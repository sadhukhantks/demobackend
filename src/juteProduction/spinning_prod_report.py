"""Spinning Production Report — GET /api/juteProductionReports/spinning_production_datewise

Five date-wise pivots over a period, picked by ``report_type``:

  quality  Quality Type × Quality        Prod, Eff, Avg Prod/Frame
  worker   EB No × Name                  Prod, Eff
  total    whole branch                  Prod, Eff, Frames Run, Avg Prod/Frame
  side     Fine / Coarse                 Prod, Eff, Avg Prod/Frame
  machine  Mc No × Quality Type          Prod, Eff

Rows come from the Spinning Low Producer fetch (worker grain, already carrying
kg and efficiency against the standard), so every figure here agrees with that
report. Efficiency is the plain mean of the row efficiencies, zeros excluded
(its "shed average" rule); Average is over the days that have a value.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.lowprod_spinning import (
    _f,
    _verify_branch,
    fetch_spinning_lowprod_rows,
)
from src.juteProduction.reportQueries import _expanding
from src.juteProduction.reports import _checklist_scope

spinning_prod_report_router = APIRouter()

MAX_DAYS = 31

PROD = "Prod (kg)"
EFF = "Eff %"
FRAMES = "Frames Run"
PPF = "Avg Prod/Frame"

# report_type -> label column headers, row key, lines, subtotal per first label.
REPORTS = {
    "quality": {
        "labels": ["Quality Type", "Quality"],
        "key": lambda r: (r["quality_type"], r.get("quality_name") or "—"),
        "lines": (PROD, EFF, PPF),
        "subtotal": True,
    },
    "worker": {
        "labels": ["EB No", "Name"],
        "key": lambda r: (r.get("eb_label") or "—", r.get("worker_name") or "—"),
        "lines": (PROD, EFF),
        "subtotal": False,
    },
    "total": {
        "labels": [],
        "key": lambda r: (),
        "lines": (PROD, EFF, FRAMES, PPF),
        "subtotal": False,
    },
    "side": {
        "labels": ["Side"],
        "key": lambda r: (r["side"],),
        "lines": (PROD, EFF, PPF),
        "subtotal": False,
    },
    "machine": {
        "labels": ["Mc No", "Quality Type"],
        "key": lambda r: (r.get("frame") or "—", r["quality_type"]),
        "lines": (PROD, EFF),
        "subtotal": True,
    },
}


def frame_side(machine_name) -> str:
    """Fine / Coarse from the frame's pitch in its name.

    ponytail: name match, because no side is stored anywhere (the spinning
    machine attr master is empty). Covers EJM's 'SPG 4 1/4' / 'SPG 4 3/4';
    add a side column to that master if other mills need it."""
    name = str(machine_name or "")
    if "4 1/4" in name:
        return "Fine"
    if "4 3/4" in name:
        return "Coarse"
    return "Unassigned"


def get_item_groups_query():
    """Yarn item -> its item group name (the report's Quality Type)."""
    return _expanding(
        """
        SELECT i.item_id, g.item_grp_name
        FROM item_mst i
        LEFT JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id
        WHERE i.item_id IN :item_ids
        """,
        "item_ids",
    )


def get_machine_names_query():
    return _expanding(
        "SELECT machine_id, machine_name FROM machine_mst WHERE machine_id IN :machine_ids",
        "machine_ids",
    )


def _mean(values):
    return round(sum(values) / len(values), 2) if values else None


def _line(labels: tuple, line: str, cells: dict) -> dict:
    """One output row. ``cells`` maps ISO date -> {kg, effs, mcs}."""
    kg = {d: c["kg"] for d, c in cells.items() if c["kg"]}
    frames = {d: len(cells[d]["mcs"]) for d in kg}
    total_kg = sum(kg.values())
    frame_days = sum(frames.values())
    total = None
    if line == PROD:
        days = {d: round(v, 2) for d, v in kg.items()}
        total = round(total_kg, 2)
        average = round(total_kg / len(kg), 2) if kg else None
    elif line == EFF:
        days = {d: _mean(c["effs"]) for d, c in cells.items() if c["effs"]}
        average = _mean([e for c in cells.values() for e in c["effs"]])
    elif line == FRAMES:
        days = frames
        total = frame_days
        average = round(frame_days / len(kg), 2) if kg else None
    else:  # PPF
        days = {d: round(kg[d] / frames[d], 2) for d in kg if frames[d]}
        average = round(total_kg / frame_days, 2) if frame_days else None
    return {
        "label1": labels[0] if labels else None,
        "label2": labels[1] if len(labels) > 1 else None,
        "line": line,
        "days": days,
        "total": total,
        "average": average,
    }


def build_spinning_pivot(rows: list, report_type: str) -> list:
    """Worker-grain rows -> pivot rows for ``report_type``: the report's lines
    for each group, a "Total" block after each first-label group where the
    report subtotals, and a "Grand Total" block last.

    Totals are re-accumulated from the raw rows, never summed from the lines —
    frames run is a DISTINCT machine count and efficiency a mean."""
    spec = REPORTS[report_type]
    groups: dict = {}
    subtotals: dict = {}
    grand: dict = {}

    def _add(cells: dict, r):
        cell = cells.setdefault(r["doff_date"], {"kg": 0.0, "effs": [], "mcs": set()})
        cell["kg"] += _f(r.get("net_wt"))
        cell["mcs"].add(r.get("machine_id"))
        eff = _f(r.get("eff_pct"))
        if eff > 0:
            cell["effs"].append(eff)

    for r in rows:
        key = spec["key"](r)
        _add(groups.setdefault(key, {}), r)
        if spec["subtotal"]:
            _add(subtotals.setdefault(key[0], {}), r)
        _add(grand, r)

    out = []
    keys = sorted(groups, key=lambda k: tuple(str(x) for x in k))
    for n, key in enumerate(keys):
        out.extend(_line(key, line, groups[key]) for line in spec["lines"])
        if spec["subtotal"] and (n + 1 == len(keys) or keys[n + 1][0] != key[0]):
            out.extend(
                _line((key[0], "Total"), line, subtotals[key[0]]) for line in spec["lines"]
            )
    if out and spec["labels"]:
        out.extend(_line(("Grand Total",), line, grand) for line in spec["lines"])
    return out


@spinning_prod_report_router.get("/spinning_production_datewise")
def spinning_production_datewise(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _checklist_scope(request, MAX_DAYS)
    if branch_id is None:
        raise HTTPException(status_code=400, detail="branch_id is required")
    report_type = (request.query_params.get("report_type") or "quality").strip().lower()
    if report_type not in REPORTS:
        raise HTTPException(
            status_code=400, detail=f"report_type must be one of {', '.join(REPORTS)}"
        )

    try:
        _verify_branch(db, co_id, branch_id)
        rows = fetch_spinning_lowprod_rows(db, co_id, branch_id, d_from, d_to)

        item_ids = sorted({r["item_id"] for r in rows if r.get("item_id") is not None})
        item_grp = {}
        if item_ids:
            item_grp = {
                r.item_id: r.item_grp_name
                for r in db.execute(get_item_groups_query(), {"item_ids": item_ids}).fetchall()
            }
        machine_ids = sorted({r["machine_id"] for r in rows if r.get("machine_id") is not None})
        machine_name = {}
        if machine_ids:
            machine_name = {
                r.machine_id: r.machine_name
                for r in db.execute(
                    get_machine_names_query(), {"machine_ids": machine_ids}
                ).fetchall()
            }
        for r in rows:
            r["quality_type"] = item_grp.get(r.get("item_id")) or "—"
            r["side"] = frame_side(machine_name.get(r.get("machine_id")))

        dates = []
        d = d_from
        while d <= d_to:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        return {
            "data": {
                "dates": dates,
                "labels": REPORTS[report_type]["labels"],
                "rows": build_spinning_pivot(rows, report_type),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
