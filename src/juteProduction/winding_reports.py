"""Winding production reports (jute production module).

Four read-only reports, registered under ``/api/juteProductionReports`` next
to the existing jute-production reports:

* ``winding_spell_report``  — reconciled KG per winder + spell + quality for a
  date (the per-person reconciliation rows), plus a per-shift roll-up.
* ``winding_quality_wise``  — table 1 of the legacy
  ``admin/reports/Winding_quality_wise_report``: per quality, the winder count,
  production, target and production-per-winder across the A/B/C shift buckets
  (shift = LEFT(spell_code, 1)), plus month-to-date, with a Grand Total row.
* ``winding_quality_group_wise`` — table 2 of that same legacy page: per yarn
  item GROUP, winding's figures beside the spinning department's own for the
  same day, so a mismatch between the two departments is visible.
* ``winding_weaving_difference`` — counted winding stock (daily physical
  count) reconciled against book stock (opening + winding + purchase -
  twisting - weaving - sale), day and month-to-date. See the endpoint's own
  docstring and build_reconciliation for why twisting is netted out. Purchase
  reads the Yarn Purchase document (kg) since phase 5 of the purchased-yarn
  design; the legacy Stores figure survives only as a transition warning.

Reconciled production = ``SUM(doff.production_qty) - opening_jugar + closing_jugar``
per person/spell (winding entry is person-keyed — see
docs/winding-person-keyed-entry-spec.md), computed at READ time (never
persisted). Values stay in KG.

Person-keyed entry is what makes the quality-wise report portable at all: the
legacy report's "No of Winder" column is ``COUNT(DISTINCT eb_id)``, which the
old machine-keyed schema could not answer. Targets come from the winding target
master ``jute_prod_winding_target_map`` (KG per winder per 8-hour shift). What
is still missing is the winding ATTENDANCE link, so targets are not pro-rated
by hours actually worked and efficiency / bundle figures remain uncomputed —
``winding_spell_report`` therefore keeps the older, blunter LIMITATION_NOTE.
Mirrors reports.py for conventions: get_tenant_db +
get_current_user_with_refresh, {"data": ...} responses and the try/except
(HTTPException raise / ValueError 400 / Exception 500) pattern.

Scope key for the three winding-only reports is **branch_id (required)**, not
co_id: one branch belongs to exactly one company, so the branch is the stricter
key. The quality reports do need a company for the company-scoped target master
and spinning day-slice, and resolve it FROM the branch rather than trusting a
co_id off the query string. ``winding_weaving_difference`` also reads
company-keyed weaving tables, so it requires BOTH co_id and branch_id and
verifies the pair against branch_mst.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import YARN_PURCHASE_CUTOVER_DATE
from src.juteProduction.constants import (
    CLOTH_SPLIT_HESSIAN,
    CLOTH_SPLIT_OTHER,
    CLOTH_SPLIT_SACKING,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    PURCHASE_UOM_KG,
    PURCHASE_UOM_MT,
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKETS,
)
from src.juteProduction.doff_analysis_query import get_spinning_doff_weight_query
from src.juteProduction.dprQueries import (
    get_dpr_weaving_mtd_query,
    get_dpr_winding_mtd_query,
)
from src.juteProduction.weaving_lock import fetch_weaving_day_rows
from src.juteProduction.lowprod_winding import TARGET_UNIT_HOURS, _full_hours
from src.juteProduction.lowprod_winding_query import get_spell_hours_query
from src.juteProduction.winding_query import (
    get_item_group_map_query,
    get_spinning_daily_by_group_query,
    get_winding_machine_by_winder_query,
    get_winding_mtd_by_quality_query,
    get_winding_reconciliation_query,
    resolve_winding_targets_query,
)
from src.juteProduction.winding_stock_query import (
    get_jute_issue_weight_query,
    get_sale_yarn_mt_query,
    fetch_twisted_yarn_item_ids,
    get_weaving_quality_type_map_query,
    get_weaving_unresolved_quality_count_query,
    get_winding_stock_by_date_query,
    get_winding_stock_prev_entry_date_query,
    get_yarn_purchase_document_detail_query,
    get_yarn_purchase_document_qty_query,
    get_yarn_purchase_qty_query,
)


router = APIRouter()

SHIFT_BUCKETS = ("A", "B", "C")

UNMAPPED_GROUP_NAME = "Unmapped"
GRAND_TOTAL_LABEL = "Grand Total"


def spell_report_note(no_target_qualities: int) -> str:
    """``winding_spell_report`` caption — what the rows are and how the target is derived."""
    return (
        "Production is reconciled KG per winder per spell (doff production - opening "
        "jugar + closing jugar). Machine is the one mapped to the winder on the "
        "Quality tab for that date and spell; blank when none is mapped. Target is the "
        "winding standard (jute_prod_winding_target_map, id_type 'qid', param 'prod', "
        "KG per winder per 8-hour shift, latest effective_date on or before the report "
        "date, 'target' preferred over 'standard') scaled to the spell's configured "
        "working hours (target / 8 x spell hours), the same scaling the Winding Low "
        "Producer report uses for a spell with no attendance row. It is NOT adjusted "
        "for hours the winder was actually absent or idle. "
        f"{no_target_qualities} quality(ies) on this report have no target configured "
        "and show a blank target. Efficiency and bundle figures are not computed."
    )


def _require_branch_id(request: Request) -> int:
    """branch_id is the scope key for winding reads — co_id is implied by it (one
    branch belongs to exactly one company), so it is mandatory and co_id is not
    read at all."""
    raw = request.query_params.get("branch_id")
    if not raw:
        raise HTTPException(status_code=400, detail="branch_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _require_report_date(request: Request) -> date:
    """report_date (alias tran_date), 400 on missing OR malformed.

    Parsed here rather than inside the endpoint's try/except so a bad date can
    never be reported as a 500 by a later handler.
    """
    raw = request.query_params.get("report_date") or request.query_params.get("tran_date")
    if not raw:
        raise HTTPException(status_code=400, detail="report_date is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")


def _co_id_of_branch(db: Session, branch_id: int) -> int:
    """The company owning this branch.

    The winding target master and the spinning day-slice are company-scoped while
    every winding read is branch-scoped, so the company is DERIVED from the branch
    instead of being accepted from the query string — a caller cannot pair one
    company's targets with another company's branch.
    """
    row = db.execute(
        text("SELECT co_id FROM branch_mst WHERE branch_id = :branch_id"),
        {"branch_id": branch_id},
    ).first()
    if row is None or row.co_id is None:
        raise HTTPException(status_code=400, detail="Unknown branch_id")
    return int(row.co_id)


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _shift_of(row) -> str:
    """A/B/C bucket for a row, or '' when the spell does not resolve to one."""
    return (row.get("shift") or "")[:1].upper()


def _fetch_reconciliation(db: Session, d_val, branch_id):
    """Run the reconciliation view (branch + date scoped; spell + quality open)."""
    return db.execute(
        get_winding_reconciliation_query(),
        {
            "tran_date": d_val,
            "spell_id": None,
            "branch_id": branch_id,
            "item_id": None,
        },
    ).fetchall()


def index_winder_machines(machine_rows) -> dict:
    """(spell_id, eb_id) -> {machine_id, mech_code, machine_name}.

    Rows come from get_winding_machine_by_winder_query (one per winder per spell,
    the Quality tab's map). If a duplicate ever slips in, the first row that
    actually names a machine wins, so a later blank row cannot erase it.
    """
    out: dict = {}
    for m in machine_rows:
        key = (m.get("spell_id"), m.get("eb_id"))
        if key in out and out[key]["machine_id"] is not None:
            continue
        out[key] = {
            "machine_id": m.get("machine_id"),
            "mech_code": m.get("mech_code"),
            "machine_name": m.get("machine_name"),
        }
    return out


def spell_target_kg(item_id, spell_id, shift, targets, spell_hours_map):
    """Target KG for one winder in one spell, or None when the quality has no target.

    ``targets`` maps item_id -> KG per winder per 8-HOUR shift; the figure is
    scaled to the spell's configured working hours (``_full_hours`` — the same
    spell_mst-first rule the Winding Low Producer report applies), so a 7.5-hour
    C spell carries 7.5/8 of the standard. None, not 0, for a quality with no
    target row: 0 would read as "no production expected" instead of "not configured".
    """
    if item_id not in targets:
        return None
    hours = _full_hours(spell_id, shift, spell_hours_map)
    return round(_f(targets[item_id]) / TARGET_UNIT_HOURS * hours, 3)


def build_spell_report_rows(recon_rows, machine_index, targets, spell_hours_map):
    """Spell-report rows (one per winder per spell per quality) + the count of
    distinct qualities that have no target configured.

    ``recon_rows`` are plain dicts from get_winding_reconciliation_query;
    ``machine_index`` is index_winder_machines(...); ``targets`` maps
    item_id -> KG per winder per 8-hour shift; ``spell_hours_map`` maps
    spell_id -> spell_mst.working_hours.
    """
    rows = []
    no_target = set()
    for m in recon_rows:
        item_id = m.get("item_id")
        spell_id = m.get("spell_id")
        machine = machine_index.get((spell_id, m.get("eb_id")), {})
        target = spell_target_kg(item_id, spell_id, m.get("shift"), targets, spell_hours_map)
        if target is None:
            no_target.add(item_id)
        rows.append(
            {
                "tran_date": str(m["tran_date"]) if m.get("tran_date") is not None else None,
                "spell_id": spell_id,
                "spell_code": m.get("spell_code"),
                "shift": m.get("shift"),
                "eb_id": m.get("eb_id"),
                "emp_code": m.get("emp_code"),
                "worker_name": m.get("worker_name"),
                "machine_id": machine.get("machine_id"),
                "mech_code": machine.get("mech_code"),
                "machine_name": machine.get("machine_name"),
                "item_id": item_id,
                "item_code": m.get("item_code"),
                "item_name": m.get("item_name"),
                "sum_production": _f(m.get("sum_production")),
                "opening_jugar": _f(m.get("opening_jugar")),
                "closing_jugar": _f(m.get("closing_jugar")),
                "production_kg": round(_f(m.get("production_kg")), 3),
                "target_kg": target,
            }
        )
    return rows, len(no_target)


@router.get("/winding_spell_report")
def winding_spell_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reconciled KG per winder + spell + quality for a date, plus a shift roll-up.

    Each row carries sum_production (doff net), opening/closing jugar, the
    reconciled production_kg, the machine the winder was mapped to for that spell
    (Quality tab) and the spell's target_kg (winding standard scaled to the
    spell's hours; null when the quality has no target). The roll-up sums
    production_kg per A/B/C shift.
    """
    branch_id = _require_branch_id(request)
    d = request.query_params.get("report_date") or request.query_params.get("tran_date")
    if not d:
        raise HTTPException(status_code=400, detail="report_date is required")
    try:
        d_val = date.fromisoformat(d)
        co_id = _co_id_of_branch(db, branch_id)
        recon_rows = [dict(r._mapping) for r in _fetch_reconciliation(db, d_val, branch_id)]
        machine_index = index_winder_machines(
            [
                dict(r._mapping)
                for r in db.execute(
                    get_winding_machine_by_winder_query(),
                    {"branch_id": branch_id, "tran_date": d_val},
                ).fetchall()
            ]
        )
        targets = {
            r.item_id: _f(r.value)
            for r in db.execute(
                resolve_winding_targets_query(), {"co_id": co_id, "on_date": d_val}
            ).fetchall()
        }
        spell_hours_map = {
            r.spell_id: _f(r.working_hours)
            for r in db.execute(get_spell_hours_query()).fetchall()
        }

        rows, no_target_qualities = build_spell_report_rows(
            recon_rows, machine_index, targets, spell_hours_map
        )

        shift_totals = {b: 0.0 for b in SHIFT_BUCKETS}
        total_kg = 0.0
        for row in rows:
            bucket = (row["shift"] or "")[:1].upper()
            if bucket in shift_totals:
                shift_totals[bucket] += row["production_kg"]
            total_kg += row["production_kg"]

        shift_rollup = [
            {"shift": b, "production_kg": round(shift_totals[b], 3)} for b in SHIFT_BUCKETS
        ]

        return {
            "data": {
                "rows": rows,
                "shift_rollup": shift_rollup,
                "total_production_kg": round(total_kg, 3),
                "note": spell_report_note(no_target_qualities),
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Quality-Wise / Quality-Group-Wise — pure builders
#
# Port of the legacy admin/reports/Winding_quality_wise_report (two tables on one
# page). Plain dicts in, plain dicts out, no I/O — the pivot and the arithmetic
# are the parts worth testing, and they are testable without a database.
# =============================================================================


def _ratio(numerator, denominator) -> float:
    """A per-winder figure at 2 dp, 0.0 when either side is zero.

    Zero winders means the ratio has no denominator; printing anything other than
    0.0 there (or dividing) would be inventing a number.
    """
    n, d = _f(numerator), _f(denominator)
    if n == 0 or d == 0:
        return 0.0
    return round(n / d, 2)


def _quality_columns(prod, winders, target, mtd_prod, mtd_winders) -> dict:
    """Every numeric column of table 1, derived from the component sums.

    Rows AND the Grand Total both go through this one function, which is what
    makes the total honest: it recomputes each ratio from the summed components
    instead of summing the rows' ratios.

    ``prod_per_winder_avg`` is likewise the WEIGHTED ratio (total KG / total
    winder-shifts), NOT the mean of the three shift ratios — the legacy report
    used the weighted one and a mean would over-weight a thin shift.
    """
    out = {}
    for b in SHIFT_BUCKETS:
        out[f"winders_{b}"] = int(winders[b])
        out[f"prod_{b}"] = round(_f(prod[b]), 3)
        out[f"target_{b}"] = round(_f(target[b]), 3)
        out[f"prod_per_winder_{b}"] = _ratio(prod[b], winders[b])
    winders_total = sum(int(winders[b]) for b in SHIFT_BUCKETS)
    prod_total = round(sum(_f(prod[b]) for b in SHIFT_BUCKETS), 3)
    target_total = round(sum(_f(target[b]) for b in SHIFT_BUCKETS), 3)
    out["winders_total"] = winders_total
    out["prod_total"] = prod_total
    out["target_total"] = target_total
    # Legacy sign: positive difference = shortfall against target.
    out["difference"] = round(target_total - prod_total, 3)
    out["prod_per_winder_avg"] = _ratio(prod_total, winders_total)
    out["mtd_prod"] = round(_f(mtd_prod), 3)
    out["mtd_winders"] = int(mtd_winders)
    out["mtd_prod_per_winder"] = _ratio(mtd_prod, mtd_winders)
    return out


def build_quality_wise(recon_rows, mtd_rows, targets) -> dict:
    """Table 1 — one row per winding quality, plus the Grand Total.

    ``recon_rows`` are the day's reconciliation rows (one per winder per spell);
    ``mtd_rows`` the month-to-date roll-up per quality; ``targets`` maps
    item_id -> KG per winder per 8-hour shift.

    A winder is counted DISTINCT per (quality, shift) but the three shift counts
    are then ADDED, so a winder working two shifts counts twice. That is what the
    legacy empla+emplb+emplc did and it is the correct denominator: two shifts of
    work produced two shifts of output.

    A quality present in MTD but not on the report date still gets a row (zeroed
    day columns) — otherwise its month production would vanish from the total.

    Rows land on ``item_code``; the pre-port code sorted on a "quality_code" key
    the rows never carried, so the order was arbitrary.
    """
    acc: dict = {}

    def slot(item_id, m):
        e = acc.get(item_id)
        if e is None:
            e = acc[item_id] = {
                "item_id": item_id,
                "item_code": m.get("item_code"),
                "item_name": m.get("item_name"),
                "prod": {b: 0.0 for b in SHIFT_BUCKETS},
                "winders": {b: set() for b in SHIFT_BUCKETS},
                "mtd_prod": 0.0,
                "mtd_winders": 0,
            }
        return e

    off_shift_kg = 0.0
    for m in recon_rows:
        bucket = _shift_of(m)
        if bucket not in SHIFT_BUCKETS:
            off_shift_kg += _f(m.get("production_kg"))
            continue
        e = slot(m.get("item_id"), m)
        e["prod"][bucket] += _f(m.get("production_kg"))
        e["winders"][bucket].add(m.get("eb_id"))

    for m in mtd_rows:
        e = slot(m.get("item_id"), m)
        e["mtd_prod"] += _f(m.get("mtd_prod"))
        e["mtd_winders"] += int(_f(m.get("mtd_winders")))

    rows = []
    no_target = 0
    for e in acc.values():
        if e["item_id"] not in targets:
            no_target += 1
        per_winder = _f(targets.get(e["item_id"]))
        winders = {b: len(e["winders"][b]) for b in SHIFT_BUCKETS}
        rows.append(
            {
                "item_id": e["item_id"],
                "item_code": e["item_code"],
                "item_name": e["item_name"],
                **_quality_columns(
                    e["prod"],
                    winders,
                    {b: per_winder * winders[b] for b in SHIFT_BUCKETS},
                    e["mtd_prod"],
                    e["mtd_winders"],
                ),
            }
        )
    rows.sort(key=lambda r: (r.get("item_code") or ""))

    grand_total = {
        "item_id": None,
        "item_code": None,
        "item_name": GRAND_TOTAL_LABEL,
        **_quality_columns(
            {b: sum(r[f"prod_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            {b: sum(r[f"winders_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            {b: sum(r[f"target_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            sum(r["mtd_prod"] for r in rows),
            sum(r["mtd_winders"] for r in rows),
        ),
    }
    return {
        "rows": rows,
        "grand_total": grand_total,
        "no_target_qualities": no_target,
        "off_shift_kg": round(off_shift_kg, 3),
    }


def _group_columns(wnd_prod, wnd_winders, spg_prod, spg_winding_qty) -> dict:
    """Every numeric column of table 2, derived from the component sums.

    Variance is computed for ALL FOUR columns including shift A. The legacy page
    only highlighted a mismatch on B, C and Total — shift A was left out of the
    comparison, which hid exactly the disagreements the report exists to show.
    """
    out = {}
    for b in SHIFT_BUCKETS:
        wnd = round(_f(wnd_prod[b]), 3)
        spg = round(_f(spg_prod[b]), 3)
        out[f"wnd_winders_{b}"] = int(wnd_winders[b])
        out[f"wnd_prod_{b}"] = wnd
        out[f"spg_prod_{b}"] = spg
        out[f"variance_{b}"] = round(wnd - spg, 3)
    wnd_total = round(sum(_f(wnd_prod[b]) for b in SHIFT_BUCKETS), 3)
    spg_total = round(sum(_f(spg_prod[b]) for b in SHIFT_BUCKETS), 3)
    out["wnd_winders_total"] = sum(int(wnd_winders[b]) for b in SHIFT_BUCKETS)
    out["wnd_prod_total"] = wnd_total
    out["spg_prod_total"] = spg_total
    out["spg_winding_qty"] = round(_f(spg_winding_qty), 3)
    out["variance_total"] = round(wnd_total - spg_total, 3)
    return out


def build_quality_group_wise(recon_rows, group_of_item, spg_rows) -> dict:
    """Table 2 — winding beside spinning's own figures, per yarn item group.

    ``group_of_item`` maps item_id -> {"item_grp_id", "item_grp_name"}. A quality
    with no group is NOT dropped: it lands in a single ``Unmapped`` row
    (item_grp_id None) and is counted, because a silently missing quality reads
    as a variance against spinning that does not exist.
    """
    acc: dict = {}

    def slot(grp_id, grp_name):
        e = acc.get(grp_id)
        if e is None:
            e = acc[grp_id] = {
                "item_grp_id": grp_id,
                "item_grp_name": UNMAPPED_GROUP_NAME if grp_id is None else grp_name,
                "wnd_prod": {b: 0.0 for b in SHIFT_BUCKETS},
                "wnd_winders": {b: set() for b in SHIFT_BUCKETS},
                "spg_prod": {b: 0.0 for b in SHIFT_BUCKETS},
                "spg_winding_qty": 0.0,
            }
        return e

    off_shift_kg = 0.0
    unmapped_qualities = set()
    for m in recon_rows:
        grp = group_of_item.get(m.get("item_id")) or {}
        grp_id = grp.get("item_grp_id")
        if grp_id is None:
            unmapped_qualities.add(m.get("item_id"))
        bucket = _shift_of(m)
        if bucket not in SHIFT_BUCKETS:
            off_shift_kg += _f(m.get("production_kg"))
            continue
        e = slot(grp_id, grp.get("item_grp_name"))
        e["wnd_prod"][bucket] += _f(m.get("production_kg"))
        e["wnd_winders"][bucket].add(m.get("eb_id"))

    for m in spg_rows:
        e = slot(m.get("item_grp_id"), m.get("item_grp_name"))
        # Spinning's own winding quantity is a day figure, not shift-split, so it
        # accumulates regardless of which bucket the spell falls in.
        e["spg_winding_qty"] += _f(m.get("spg_winding_qty"))
        bucket = _shift_of(m)
        if bucket not in SHIFT_BUCKETS:
            off_shift_kg += _f(m.get("spg_prod"))
            continue
        e["spg_prod"][bucket] += _f(m.get("spg_prod"))

    rows = [
        {
            "item_grp_id": e["item_grp_id"],
            "item_grp_name": e["item_grp_name"],
            **_group_columns(
                e["wnd_prod"],
                {b: len(e["wnd_winders"][b]) for b in SHIFT_BUCKETS},
                e["spg_prod"],
                e["spg_winding_qty"],
            ),
        }
        for e in acc.values()
    ]
    rows.sort(key=lambda r: (r["item_grp_id"] is None, r["item_grp_name"] or ""))

    grand_total = {
        "item_grp_id": None,
        "item_grp_name": GRAND_TOTAL_LABEL,
        **_group_columns(
            {b: sum(r[f"wnd_prod_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            {b: sum(r[f"wnd_winders_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            {b: sum(r[f"spg_prod_{b}"] for r in rows) for b in SHIFT_BUCKETS},
            sum(r["spg_winding_qty"] for r in rows),
        ),
    }
    return {
        "rows": rows,
        "grand_total": grand_total,
        "unmapped_qualities": len(unmapped_qualities),
        "off_shift_kg": round(off_shift_kg, 3),
    }


def _off_shift_clause(off_shift_kg: float) -> str:
    if not off_shift_kg:
        return ""
    return (
        f" {off_shift_kg} KG sits on spells that do not resolve to shift A, B or C "
        "(missing or inactive spell_mst row) and is excluded from every column."
    )


def quality_wise_note(no_target_qualities: int, off_shift_kg: float) -> str:
    return (
        "Production is reconciled KG (doff production - opening jugar + closing "
        "jugar), taken once per winder per spell. Winder counts are DISTINCT eb_id "
        "per quality and shift, then added across the three shifts, so a winder who "
        "works two shifts counts twice — that is the denominator the "
        "production-per-winder columns divide by, and the average is the weighted "
        "total KG / total winders, not the mean of the three shift ratios. Targets "
        "resolve from jute_prod_winding_target_map (id_type 'qid', param 'prod') as "
        "KG per winder per 8-hour SHIFT, latest effective_date on or before the "
        "report date, preferring value_role 'target' over 'standard'; "
        f"{no_target_qualities} quality(ies) on this report have NO target row "
        "configured and read 0 for target and for difference, which also pulls the "
        "Grand Total target down. There is still no winding attendance link, so "
        "targets are NOT pro-rated by hours actually worked (the legacy report "
        "pro-rated by working_hours - idle_hours) — a shift that ran short still "
        "carries a full 8-hour target. Efficiency and bundle figures are not "
        "computed." + _off_shift_clause(off_shift_kg)
    )


def quality_group_wise_note(unmapped_qualities: int, off_shift_kg: float) -> str:
    return (
        "Winding is reconciled KG (jugar-adjusted, computed at read time) for the "
        "requested branch. Spinning is the frozen planning-grid day-slice "
        "(jute_prod_spinning_daily), filtered on co_id AND branch_id: branch_id is "
        "nullable on that table but is populated on every live row in dev3 and sls, "
        "so both sides of this report are at the same branch scope. The two are "
        "independent measurements of the same yarn taken by different departments, "
        "so a nonzero variance is what this report exists to surface, not an error. "
        f"{unmapped_qualities} winding quality(ies) resolve to no item group and are "
        "reported under 'Unmapped' rather than dropped; spinning rows whose item has "
        "no group land in the same row." + _off_shift_clause(off_shift_kg)
    )


@router.get("/winding_quality_wise")
def winding_quality_wise(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Winding Quality List — table 1 of the legacy quality-wise report.

    Per winding quality for one date: winder count, production, target and
    production-per-winder across shifts A/B/C, the target-vs-production
    difference, and month-to-date production with its own per-winder average.
    Grand Total recomputes every ratio from the summed components.
    """
    branch_id = _require_branch_id(request)
    report_d = _require_report_date(request)
    try:
        month_start = report_d.replace(day=1)
        co_id = _co_id_of_branch(db, branch_id)

        recon_rows = [dict(r._mapping) for r in _fetch_reconciliation(db, report_d, branch_id)]
        mtd_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_mtd_by_quality_query(),
                {"branch_id": branch_id, "date_from": month_start, "report_date": report_d},
            ).fetchall()
        ]
        targets = {
            r.item_id: _f(r.value)
            for r in db.execute(
                resolve_winding_targets_query(), {"co_id": co_id, "on_date": report_d}
            ).fetchall()
        }

        built = build_quality_wise(recon_rows, mtd_rows, targets)
        return {
            "data": {
                "report_date": str(report_d),
                "mtd_from": str(month_start),
                "shifts": list(SHIFT_BUCKETS),
                "rows": built["rows"],
                "grand_total": built["grand_total"],
                "note": quality_wise_note(
                    built["no_target_qualities"], built["off_shift_kg"]
                ),
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/winding_quality_group_wise")
def winding_quality_group_wise(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Winding Quality Group List — table 2 of the legacy quality-wise report.

    Per yarn item GROUP for one date: winding's winder count and production per
    shift, beside the spinning department's own production and the winding
    quantity spinning booked, with the variance on every shift AND the total.
    """
    branch_id = _require_branch_id(request)
    report_d = _require_report_date(request)
    try:
        co_id = _co_id_of_branch(db, branch_id)

        recon_rows = [dict(r._mapping) for r in _fetch_reconciliation(db, report_d, branch_id)]
        item_ids = sorted({m["item_id"] for m in recon_rows if m.get("item_id") is not None})
        group_of_item = (
            {}
            if not item_ids
            else {
                m["item_id"]: {
                    "item_grp_id": m["item_grp_id"],
                    "item_grp_name": m["item_grp_name"],
                }
                for m in (
                    dict(r._mapping)
                    for r in db.execute(
                        get_item_group_map_query(), {"item_ids": item_ids}
                    ).fetchall()
                )
            }
        )
        spg_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_spinning_daily_by_group_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": report_d},
            ).fetchall()
        ]

        built = build_quality_group_wise(recon_rows, group_of_item, spg_rows)
        return {
            "data": {
                "report_date": str(report_d),
                "shifts": list(SHIFT_BUCKETS),
                "rows": built["rows"],
                "grand_total": built["grand_total"],
                "note": quality_group_wise_note(
                    built["unmapped_qualities"], built["off_shift_kg"]
                ),
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Winding & Weaving Difference — pure builders
#
# Take plain dicts and do no I/O, which is what makes the golden case testable
# without a database.
# =============================================================================


def stock_mt_of(no_of_samples, wt_per_sample) -> float:
    """MT for one counted line, at the 3 decimals the sheet prints."""
    return round(_f(no_of_samples) * _f(wt_per_sample) / 1000.0, 3)


def _prev_key(row: dict):
    """Identity of a counted line across days, for the PRVS STOCK column.

    stock_loc and dept_id joined the key with phase 5 of the purchased-yarn
    design (2026-09-25): the same package counted in the godown and on a
    department floor is two lines, and each must find ITS OWN previous figure
    rather than both printing the sum of yesterday's two. Rows saved before
    the columns existed read 'FLOOR' / NULL on both days and still match.
    Totals are untouched — only the PRVS column keys on this.
    """
    return (
        row.get("bucket"),
        row.get("item_id"),
        row.get("package_label"),
        row.get("wastage_area_id"),
        row.get("stock_loc"),
        row.get("dept_id"),
    )


def build_stock_blocks(rows, prev_rows) -> dict:
    """Group counted lines into the report's buckets, in print order.

    Returns {bucket: {"rows": [...], "subtotal_mt": float}, "grand_total_mt": float}.
    Every bucket in WINDING_STOCK_BUCKETS appears even when empty, so the printed
    report keeps a stable shape day to day.
    """
    prev_mt = {}
    for r in prev_rows:
        prev_mt[_prev_key(r)] = prev_mt.get(_prev_key(r), 0.0) + stock_mt_of(
            r.get("no_of_samples"), r.get("wt_per_sample")
        )

    blocks = {b: {"rows": [], "subtotal_mt": 0.0} for b in WINDING_STOCK_BUCKETS}
    for r in rows:
        bucket = r.get("bucket")
        if bucket not in blocks:
            # An unknown bucket must not silently disappear from the totals.
            blocks[bucket] = {"rows": [], "subtotal_mt": 0.0}
        out = dict(r)
        out["stock_mt"] = stock_mt_of(r.get("no_of_samples"), r.get("wt_per_sample"))
        out["prvs_stock_mt"] = round(prev_mt.get(_prev_key(r), 0.0), 3)
        blocks[bucket]["rows"].append(out)
        blocks[bucket]["subtotal_mt"] = round(
            blocks[bucket]["subtotal_mt"] + out["stock_mt"], 3
        )

    blocks["grand_total_mt"] = round(
        sum(b["subtotal_mt"] for b in blocks.values() if isinstance(b, dict)), 3
    )
    return blocks


def build_bucket_quality_summary(
    rows, prev_rows, bucket: str = WINDING_STOCK_BUCKET_MILL_USE
) -> dict:
    """One bucket's counted lines consolidated to ONE row per yarn quality.

    TKT-000182 asked for the consolidated position of MILL USE yarn: that block
    prints every counted line, so a quality recurs once per package and once per
    storage place (a real sls day: 17 mill-use lines over 6 qualities) and its
    whole mill-use position appears nowhere. Only the requested bucket is
    summarised -- lines from the other blocks (purchase, sale, ball, waste) are
    NOT counted here, so total_mt ties to that bucket's printed subtotal, not to
    the sheet's grand total.

    prvs_stock_mt is summed from prev_rows DIRECTLY, and filtered to the SAME
    bucket. Two reasons, both of which would overstate the opening position:
    the blocks' per-row prvs_stock_mt repeats the whole previous total on every
    current row sharing a (bucket, item, package, area) key (which is why the
    printed blocks carry no PRVS subtotal), and a quality also held in another
    block yesterday would drag that block's tonnage into a mill-use figure.

    A quality counted in this bucket yesterday but absent today still gets a
    row, with total_mt 0.0 -- a position that fell to zero is exactly what a
    consolidated view is read for, and dropping it would hide it.

    Returns {"bucket": str, "rows": [...], "total_mt": float,
    "prvs_total_mt": float}, rows ordered by the quality label the sheet prints.
    """
    groups: dict = {}

    def _group_for(r):
        key = r.get("item_id")
        g = groups.get(key)
        if g is None:
            g = {
                "item_id": r.get("item_id"),
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name"),
                "quality_name": r.get("quality_name"),
                "yarn_count": r.get("yarn_count"),
                "total_mt": 0.0,
                "prvs_stock_mt": 0.0,
                "line_count": 0,
            }
            groups[key] = g
        return g

    for r in rows:
        if r.get("bucket") != bucket:
            continue
        g = _group_for(r)
        g["total_mt"] = round(
            g["total_mt"] + stock_mt_of(r.get("no_of_samples"), r.get("wt_per_sample")), 3
        )
        g["line_count"] += 1

    for r in prev_rows:
        if r.get("bucket") != bucket:
            continue
        g = _group_for(r)
        g["prvs_stock_mt"] = round(
            g["prvs_stock_mt"]
            + stock_mt_of(r.get("no_of_samples"), r.get("wt_per_sample")),
            3,
        )

    def _sort_key(g):
        label = g["item_name"] or g["item_code"] or g["quality_name"] or ""
        return (label.upper(), g["item_id"] or 0)

    out_rows = sorted(groups.values(), key=_sort_key)
    return {
        "bucket": bucket,
        "rows": out_rows,
        "total_mt": round(sum(g["total_mt"] for g in out_rows), 3),
        "prvs_total_mt": round(sum(g["prvs_stock_mt"] for g in out_rows), 3),
    }


def split_twisting_mt(rows, twisted_item_ids):
    """Reconciliation rows -> (winding_mt, twisting_mt), KG in, MT at 3 dp.

    Ply (twisted) output is keyed into the winding doff entry because twisting
    has no module of its own, so it arrives in these rows indistinguishable from
    real winding except by the yarn master's is_twisted flag. Pulling it out here
    is what lets the reconciliation treat it as its own line instead of as
    winding production that was never physically counted.
    """
    wind_kg = twist_kg = 0.0
    for r in rows:
        kg = _f(r.get("production_kg"))
        if r.get("item_id") in twisted_item_ids:
            twist_kg += kg
        else:
            wind_kg += kg
    return round(wind_kg / 1000.0, 3), round(twist_kg / 1000.0, 3)


def build_reconciliation(
    opening_mt, winding_mt, weaving_mt, purchase_mt, sale_mt, actual_mt,
    twisting_mt=0.0,
) -> dict:
    """Book stock vs counted stock.

        should_be = opening + winding + purchase - twisting - weaving - sale
        difference = actual - should_be
        pct        = difference / winding * 100

    TWISTING is subtracted as a PROXY for the single yarn it consumed. Twisting
    output is keyed into winding (no twisting module exists), so it adds to book
    stock while the single yarn it ate is never subtracted and the ply itself is
    absent from the physical count — the difference therefore drifted by roughly
    TWICE the twisting tonnage. Subtracting the output nets both halves under an
    input ~= output assumption; twisting WASTAGE (input minus output) knowingly
    stays inside difference_mt until a real twisting-input entry exists.

    JUTE ISSUE is deliberately absent: it feeds spinning, not winding stock, and
    the mill's own sheet closes without it (18.415 + 37.875 = 56.290).

    pct is None rather than 0 when winding is zero — a percentage of nothing is
    not a percentage, and printing 0.00% would read as "no variance". The
    denominator is winding NET of twisting, matching the winding_mt line printed
    beside it.
    """
    should_be = round(
        _f(opening_mt) + _f(winding_mt) + _f(purchase_mt)
        - _f(twisting_mt) - _f(weaving_mt) - _f(sale_mt),
        3,
    )
    difference = round(_f(actual_mt) - should_be, 3)
    pct = None if _f(winding_mt) == 0 else round(difference / _f(winding_mt) * 100, 2)
    return {
        "opening_mt": round(_f(opening_mt), 3),
        "winding_mt": round(_f(winding_mt), 3),
        "twisting_mt": round(_f(twisting_mt), 3),
        "weaving_mt": round(_f(weaving_mt), 3),
        "purchase_mt": round(_f(purchase_mt), 3),
        "sale_mt": round(_f(sale_mt), 3),
        "should_be_closing_mt": should_be,
        "actual_closing_mt": round(_f(actual_mt), 3),
        "difference_mt": difference,
        "difference_pct": pct,
    }


def build_cloth_split(weaving_rows, qtype_by_qid) -> dict:
    """Weaving production split hessian / sacking / other.

    Classification comes from jute_prod_finishing_quality.quality_type resolved
    through the cloth item (1 = Hessian, 2 = Sacking). Anything unclassified
    lands in OTHER so the three buckets always sum to total weaving production.

    Known state: on sls every weaving quality points at the JCLOTH-MIG catch-all
    item, which has no finishing row, so OTHER carries 100% until those qualities
    are pointed at real cloth items in the masters.
    """
    split = {
        CLOTH_SPLIT_HESSIAN: 0.0,
        CLOTH_SPLIT_SACKING: 0.0,
        CLOTH_SPLIT_OTHER: 0.0,
    }
    for r in weaving_rows:
        qtype = qtype_by_qid.get(r.get("weaving_quality_id"))
        if qtype == FINISHING_QUALITY_TYPE_HESSIAN:
            key = CLOTH_SPLIT_HESSIAN
        elif qtype == FINISHING_QUALITY_TYPE_SACKING:
            key = CLOTH_SPLIT_SACKING
        else:
            key = CLOTH_SPLIT_OTHER
        split[key] = round(split[key] + _f(r.get("production_mt")), 3)
    return split


def doff_kg_to_mt(kg) -> float:
    """Spinning doff net weight (KG) -> MT at the 3 decimals the sheet prints.

    Source is daily_doff_tbl.net_weight via get_spinning_doff_weight_query --
    the actual weighed doffs, per the owner's "From doff wt". NOT
    jute_prod_spinning_daily.act_prod_doff: properly (co, branch, active)-scoped,
    that planning snapshot read 0.0 kg on 12 of the 18 days of September 2026 on
    sls branch 29 and matched the doffs exactly on the days it was filled in, so
    it is a data-entry artefact rather than a second measurement.

    Verified magnitudes, sls branch 29: 18-09-2026 = 597 doffs, 20,137.60 kg =
    20.138 MT against 21.160 MT of winding the same day (95%); September MTD =
    355.411 MT against 353.184 MT of winding (101%). Spinning feeds winding, so
    those are the ratios to expect -- a figure 100x off either way would stand out
    against the winding row printed beneath it.

    Spinning is CONTEXT ONLY on this report: it is the upstream department, and
    build_reconciliation never sees it, so should_be_closing_mt and difference_mt
    stay a statement about winding stock alone.
    """
    return round(_f(kg) / 1000.0, 3)


def issue_kg_to_mt(kg) -> float:
    """jute_issue.weight (KG) -> MT at the 3 decimals the sheet prints.

    This divided by 10 until it was found to be 100x high: the query aliased the
    sum `issue_qtl` and its docstring claimed QUINTAL, but the column is KG (a
    BALE row is ~146.9 per unit -- a ~150 KG bale, not a 14.7-tonne one, and
    src/juteProcurement/issuePlanner.py converts the same quantity with /1000).
    2026-09-14 on sls branch 29: 19,274 KG = 19.274 MT, where the report used to
    print 1,927.400 MT.

    Jute issue is CONTEXT ONLY on this report -- build_reconciliation never adds
    it to should_be_closing_mt or difference_mt -- so this corrects a printed
    figure without moving the winding/weaving difference.
    """
    return round(_f(kg) / 1000.0, 3)


def purchase_qty_to_mt(qty, uoms):
    """Convert a purchased-yarn quantity to MT, refusing to guess.

    Returns (mt, unknown_uom_names). MTR is METRE and MT / MTS are METRIC TONNE —
    different units that read alike, and the same sls item carries both labels.
    An unrecognised unit contributes 0 and is reported, because inventing tonnage
    out of a length is worse than a visibly missing figure.

    A non-zero quantity with NO uom at all (GROUP_CONCAT returns NULL when
    every matching line's uom_id is NULL — proc_inward_dtl.uom_id is nullable
    and the join to uom_mst is LEFT) is reported as "(no uom)" for the same
    reason: it must not silently drop out of should_be_closing_mt.

    Mixed units on one line set cannot be apportioned. When names span both the
    MT and KG families there is no safe conversion either way, so this returns
    0.0 (the quantity is excluded, not guessed) with every recognised name
    added to the warning list — never silently treating a KG quantity as MT
    and inflating it 1000x.
    """
    names = [u.strip().upper() for u in (uoms or "").split(",") if u.strip()]

    if not names:
        return (0.0, ["(no uom)"]) if _f(qty) != 0 else (0.0, [])

    unknown = [
        n for n in names if n not in PURCHASE_UOM_MT and n not in PURCHASE_UOM_KG
    ]
    has_mt = any(n in PURCHASE_UOM_MT for n in names)
    has_kg = any(n in PURCHASE_UOM_KG for n in names)
    if has_mt and has_kg:
        for n in names:
            if n not in unknown and (n in PURCHASE_UOM_MT or n in PURCHASE_UOM_KG):
                unknown.append(n)
        return 0.0, unknown

    if has_mt:
        return round(_f(qty), 3), unknown
    if has_kg:
        return round(_f(qty) / 1000.0, 3), unknown
    return 0.0, unknown


def purchase_kg_to_mt(kg) -> float:
    """Yarn Purchase document weight_kg -> MT at the 3 decimals the sheet prints.

    Phase 5 of the purchased-yarn design (§3.4): purchase_mt reads approved
    jute_yarn_purchase_li lines, which are in KILOGRAMS by construction
    (principle 1 of the design — never MTR, never a UOM to guess), so this is
    a plain /1000 with none of purchase_qty_to_mt's unit forensics.
    """
    return round(_f(kg) / 1000.0, 3)


def legacy_stores_yarn_warning(legacy_mt, document_mt, window_to, unknown_uoms=()):
    """The transition warning, or None.

    Fires when approved Stores receipts of yarn-type items exist in the
    window while they are not (or cannot be shown to be) covered by Yarn
    Purchase documents:

    * the converted legacy figure (proc_inward, > 0 MT) exists while NO
      approved Yarn Purchase document does (document figure == 0) — the sls
      state before the backfill, where the report used to read 0 without
      saying why; or
    * some of those receipts are in a unit purchase_qty_to_mt refused to
      convert (``unknown_uoms``, e.g. MTR). Their tonnage is unknown, so no
      document figure can vouch for them — flagged whether or not documents
      exist, otherwise an MTR-only month would pass the guard in silence.

    Silent for windows ending before YARN_PURCHASE_CUTOVER_DATE (owner ruling
    7: no document can be expected before the backfill start).
    """
    if window_to is not None and window_to < YARN_PURCHASE_CUTOVER_DATE:
        return None
    legacy = round(_f(legacy_mt), 3)
    tonnage_gap = legacy > 0 and round(_f(document_mt), 3) == 0
    units = sorted({u for u in (unknown_uoms or ()) if u})
    if units:
        unit_msg = (
            f"Stores receipts of yarn-type items exist in unrecognised unit(s) "
            f"{', '.join(units)} — book them as Yarn Purchase documents"
        )
        if tonnage_gap:
            return (
                f"Stores receipts of yarn-type items exist ({legacy:.3f} MT) but no "
                f"approved Yarn Purchase document in this period; further receipts are "
                f"in unrecognised unit(s) {', '.join(units)} — book them as Yarn "
                "Purchase documents"
            )
        return unit_msg
    if not tonnage_gap:
        return None
    return (
        f"Stores receipts of yarn-type items exist ({legacy:.3f} MT) but no "
        "approved Yarn Purchase document in this period"
    )


def build_purchase_detail(rows) -> list:
    """Drill-down rows behind purchase_mt, JSON-ready.

    One row per (Yarn Purchase document, yarn quality) as
    get_yarn_purchase_document_detail_query returns them; dates to ISO
    strings, kg to float at 3 dp. Order is the query's (date, purchase_no).
    """
    out = []
    for r in rows:
        td = r.get("tran_date")
        out.append(
            {
                "yarn_purchase_id": r.get("yarn_purchase_id"),
                "purchase_no": r.get("purchase_no"),
                "tran_date": None if td is None else str(td),
                "item_id": r.get("item_id"),
                "item_name": r.get("item_name"),
                "yarn_count": None if r.get("yarn_count") is None else _f(r.get("yarn_count")),
                "kg": round(_f(r.get("kg")), 3),
            }
        )
    return out


# =============================================================================
# Winding & Weaving Difference — endpoint
# =============================================================================


DIFFERENCE_NOTE = (
    "JUTE ISSUE is shown for context only and is not part of the should-be "
    "closing stock — it feeds spinning, not winding. PURCHASE yarn reads the "
    "approved Yarn Purchase document (jute_yarn_purchase, kg / 1000) at this "
    "company and branch; purchase_detail lists the documents behind the "
    "month figure. Yarn Issue and Yarn Return are internal moves (godown to "
    "department floor and back) and never enter should-be closing stock. "
    "Approved Stores receipts of yarn-type items are no longer counted as "
    "purchase: their tonnage is shown as warnings.legacy_stores_yarn_mt, and "
    "warnings.legacy_stores_yarn_warning is set when such receipts exist in the "
    "period with no Yarn Purchase document (the transition state until the "
    "Stores entries are backfilled as Yarn Purchase documents from the "
    "2026-04-01 cut-over). Sale yarn "
    "uses the item_grp_mst.item_type_id = 4 test, so a yarn item left "
    "out of a yarn item group is silently excluded from sale_mt — and "
    "because sale is subtracted, that inflates should-be closing stock and "
    "reads as shrinkage rather than a data gap. Cloth split "
    "falls to OTHER for any weaving quality whose cloth item has no finishing "
    "quality_type. Weaving rows whose weaving_quality_id does not resolve to an "
    "active weaving quality (NULL, or pointing at a missing/inactive one) are "
    "excluded from weaving production, so should-be closing stock is overstated "
    "by however much those rows would have subtracted — see the weaving_unresolved "
    "counts in warnings. "
    "TWISTING is its own line: the mill keys ply (twisted) output into the WINDING "
    "doff entry because no twisting module exists yet, so those doffs are split out "
    "of winding_mt on the yarn master's is_twisted flag and shown as twisting_mt. "
    "It is SUBTRACTED from should-be closing stock as a proxy for the single yarn "
    "twisting consumed, which is never issued out of winding stock — while the ply "
    "it produces is absent from the physical count. Netting the output covers both "
    "halves under an INPUT ~= OUTPUT assumption; twisting WASTAGE (input minus "
    "output) knowingly stays inside the difference until a real twisting-INPUT "
    "entry exists. Sales of twisted items are excluded from sale_mt for the same "
    "reason — ply yarn is item_type_id 4 like any other yarn, so counting it there "
    "as well would subtract the same tonnage twice. An unflagged ply item is "
    "reported as ordinary winding and the difference drifts by about twice its "
    "tonnage, so the flag must be set on every ply item in the yarn master."
)


def _sum_col(rows, col) -> float:
    return round(sum(_f(r.get(col)) for r in rows), 3)


def _opening_mt(db, co_id, branch_id, d_val) -> float:
    """Counted grand total MT for one date, through build_stock_blocks — the
    exact same per-line round-then-accumulate path the closing figure uses.
    A SQL-side ROUND(SUM(...)/1000, 3) regime disagrees with that path at the
    milli-tonne, and that drift lands directly in difference_mt, so opening
    and closing must never be computed two different ways."""
    rows = [
        dict(r._mapping)
        for r in db.execute(
            get_winding_stock_by_date_query(),
            {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val},
        ).fetchall()
    ]
    return build_stock_blocks(rows, [])["grand_total_mt"]


def _prev_entry_date(db, co_id, branch_id, before):
    row = db.execute(
        get_winding_stock_prev_entry_date_query(),
        {"co_id": co_id, "branch_id": branch_id, "tran_date": before},
    ).fetchone()
    return None if row is None else row.prev_entry_date


def _weaving_unresolved_count(db, co_id, branch_id, date_from, date_to) -> int:
    """How many weaving rows (cuts > 0) the quality join silently dropped — see
    get_weaving_unresolved_quality_count_query for why this matters here."""
    row = db.execute(
        get_weaving_unresolved_quality_count_query(),
        {"co_id": co_id, "branch_id": branch_id, "date_from": date_from, "date_to": date_to},
    ).fetchone()
    return 0 if row is None or row.unresolved_count is None else int(row.unresolved_count)


@router.get("/winding_weaving_difference")
def winding_weaving_difference(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Counted winding stock reconciled against book stock, day and month-to-date.

    Both co_id and branch_id are required: the winding builders are branch-keyed
    with no company predicate, while the weaving builders are company-keyed and
    NULL-branch tolerant. The pair check below stops a branch of company A from
    reporting company B's weaving under this header.
    """
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d = request.query_params.get("report_date") or request.query_params.get("tran_date")
    if not d:
        raise HTTPException(status_code=400, detail="report_date is required")
    try:
        report_d = date.fromisoformat(d)
        month_start = report_d.replace(day=1)

        pair_ok = db.execute(
            text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
            {"branch_id": branch_id, "co_id": co_id},
        ).first()
        if not pair_ok:
            raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")

        stock_binds = {"co_id": co_id, "branch_id": branch_id, "tran_date": report_d}

        # --- counted stock, this day and the previous counted day -------------
        rows = [
            dict(r._mapping)
            for r in db.execute(get_winding_stock_by_date_query(), stock_binds).fetchall()
        ]
        prev_day = _prev_entry_date(db, co_id, branch_id, report_d)
        prev_rows = (
            []
            if prev_day is None
            else [
                dict(r._mapping)
                for r in db.execute(
                    get_winding_stock_by_date_query(),
                    {**stock_binds, "tran_date": prev_day},
                ).fetchall()
            ]
        )
        stock = build_stock_blocks(rows, prev_rows)
        actual_mt = stock["grand_total_mt"]

        # --- openings -----------------------------------------------------
        # Day opening reuses prev_rows (already fetched above) through the
        # same build_stock_blocks path as the closing figure — one query
        # cheaper and never drifts from it at the milli-tonne.
        day_opening = (
            0.0 if prev_day is None else build_stock_blocks(prev_rows, [])["grand_total_mt"]
        )
        month_open_day = _prev_entry_date(db, co_id, branch_id, month_start)
        mtd_opening = (
            0.0
            if month_open_day is None
            else _opening_mt(db, co_id, branch_id, month_open_day)
        )

        # --- winding: kg -> MT ------------------------------------------------
        wind_day_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_reconciliation_query(),
                {"branch_id": branch_id, "tran_date": report_d,
                 "spell_id": None, "item_id": None},
            ).fetchall()
        ]
        wind_mtd_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_winding_mtd_query(),
                {"branch_id": branch_id, "date_from": month_start, "report_date": report_d},
            ).fetchall()
        ]
        # Ply output rides the winding doff entry (no twisting module), so it is
        # split off the winding aggregates on the yarn master's flag.
        twisted_ids = fetch_twisted_yarn_item_ids(db) or set()
        wind_day_mt, twist_day_mt = split_twisting_mt(wind_day_rows, twisted_ids)
        wind_mtd_mt, twist_mtd_mt = split_twisting_mt(wind_mtd_rows, twisted_ids)

        # --- weaving: already MT ---------------------------------------------
        # Fresh stored log rows per spell; live slice for stale/unsynced spells.
        weav_day_rows = fetch_weaving_day_rows(db, co_id, report_d, branch_id)
        weav_mtd_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_weaving_mtd_query(),
                {"co_id": co_id, "branch_id": branch_id,
                 "date_from": month_start, "report_date": report_d},
            ).fetchall()
        ]
        weav_day_mt = _sum_col(weav_day_rows, "production_mt")
        weav_mtd_mt = _sum_col(weav_mtd_rows, "production_mt")

        # --- spinning: doff net weight, KG -> MT (context only) ---------------
        # Upstream department, shown beside winding for context; one query serves
        # both grains (day = a one-day window), same shape as jute issue below.
        def _spinning_mt(date_from, date_to):
            row = db.execute(
                get_spinning_doff_weight_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "date_from": date_from,
                    "date_to": date_to,
                },
            ).fetchone()
            return doff_kg_to_mt(None if row is None else row.production_kg)

        # --- jute issue: KG -> MT (context only) ------------------------------
        def _issue_mt(date_from, date_to):
            row = db.execute(
                get_jute_issue_weight_query(),
                {"branch_id": branch_id, "date_from": date_from, "date_to": date_to},
            ).fetchone()
            return issue_kg_to_mt(None if row is None else row.issue_kg)

        # --- purchase yarn: approved Yarn Purchase document, kg -> MT ----------
        def _purchase_mt(date_from, date_to):
            row = db.execute(
                get_yarn_purchase_document_qty_query(),
                {"co_id": co_id, "branch_id": branch_id,
                 "date_from": date_from, "date_to": date_to},
            ).fetchone()
            return purchase_kg_to_mt(None if row is None else row.purchase_kg)

        # --- legacy Stores yarn receipts: cross-check ONLY, never in should-be
        # (converted per UOM, never guessed — the pre-phase-5 purchase source)
        def _legacy_stores_mt(date_from, date_to):
            row = db.execute(
                get_yarn_purchase_qty_query(),
                {"co_id": co_id, "branch_id": branch_id,
                 "date_from": date_from, "date_to": date_to},
            ).fetchone()
            if row is None:
                return 0.0, []
            return purchase_qty_to_mt(row.purchase_qty, row.uoms)

        # --- sale yarn: kg -> MT ---------------------------------------------
        def _sale_mt(date_from, date_to):
            row = db.execute(
                get_sale_yarn_mt_query(),
                {"branch_id": branch_id, "date_from": date_from, "date_to": date_to},
            ).fetchone()
            return round(_f(None if row is None else row.sale_kg) / 1000.0, 3)

        purch_day_mt = _purchase_mt(report_d, report_d)
        purch_mtd_mt = _purchase_mt(month_start, report_d)
        purchase_detail = build_purchase_detail(
            [
                dict(r._mapping)
                for r in db.execute(
                    get_yarn_purchase_document_detail_query(),
                    {"co_id": co_id, "branch_id": branch_id,
                     "date_from": month_start, "date_to": report_d},
                ).fetchall()
            ]
        )
        # Month window only: it contains the day, and the warning is about
        # the period's documents, not a single day's.
        legacy_mtd_mt, legacy_unknown = _legacy_stores_mt(month_start, report_d)

        # --- weaving rows the quality join dropped (disclosure only) ---------
        weaving_unresolved_day = _weaving_unresolved_count(db, co_id, branch_id, report_d, report_d)
        weaving_unresolved_mtd = _weaving_unresolved_count(
            db, co_id, branch_id, month_start, report_d
        )

        day = build_reconciliation(
            opening_mt=day_opening,
            winding_mt=wind_day_mt,
            twisting_mt=twist_day_mt,
            weaving_mt=weav_day_mt,
            purchase_mt=purch_day_mt,
            sale_mt=_sale_mt(report_d, report_d),
            actual_mt=actual_mt,
        )
        # Context-only figures, stamped on AFTER build_reconciliation has
        # returned: neither jute issue (upstream of spinning) nor spinning
        # production (upstream of winding) may enter should_be_closing_mt or
        # difference_mt, which are statements about winding stock alone.
        day["jute_issue_mt"] = _issue_mt(report_d, report_d)
        day["spinning_mt"] = _spinning_mt(report_d, report_d)

        mtd = build_reconciliation(
            opening_mt=mtd_opening,
            winding_mt=wind_mtd_mt,
            twisting_mt=twist_mtd_mt,
            weaving_mt=weav_mtd_mt,
            purchase_mt=purch_mtd_mt,
            sale_mt=_sale_mt(month_start, report_d),
            actual_mt=actual_mt,
        )
        mtd["jute_issue_mt"] = _issue_mt(month_start, report_d)
        mtd["spinning_mt"] = _spinning_mt(month_start, report_d)

        # --- cloth split ------------------------------------------------------
        qtype_by_qid = {
            m["weaving_quality_id"]: m["quality_type"]
            for m in (
                dict(r._mapping)
                for r in db.execute(
                    get_weaving_quality_type_map_query(), {"co_id": co_id}
                ).fetchall()
            )
        }

        # A chain break means the day opening came from an older count, so the
        # day figures are stated against that date rather than silently implying
        # yesterday.
        expected_prev = report_d - timedelta(days=1)
        chain_ok = prev_day == expected_prev

        return {
            "data": {
                "report_date": str(report_d),
                "date_from": str(month_start),
                "stock": stock,
                # Mill-use only (TKT-000182): ties to the MILL_USE subtotal.
                "stock_summary": build_bucket_quality_summary(rows, prev_rows),
                "production": {"day": day, "mtd": mtd},
                # Documents behind mtd.purchase_mt (kg per document + quality).
                "purchase_detail": purchase_detail,
                "cloth_split": {
                    "day": build_cloth_split(weav_day_rows, qtype_by_qid),
                    "mtd": build_cloth_split(weav_mtd_rows, qtype_by_qid),
                },
                "chain": {
                    "ok": chain_ok,
                    "prev_entry_date": None if prev_day is None else str(prev_day),
                    "month_opening_date": (
                        None if month_open_day is None else str(month_open_day)
                    ),
                },
                "warnings": {
                    # Both describe the LEGACY Stores cross-check (month window):
                    # unrecognised units on those receipts, and their tonnage.
                    "purchase_unknown_uoms": sorted(set(legacy_unknown)),
                    "legacy_stores_yarn_mt": legacy_mtd_mt,
                    "legacy_stores_yarn_warning": legacy_stores_yarn_warning(
                        legacy_mtd_mt, purch_mtd_mt, report_d, legacy_unknown
                    ),
                    "weaving_unresolved_day": weaving_unresolved_day,
                    "weaving_unresolved_mtd": weaving_unresolved_mtd,
                },
                "note": DIFFERENCE_NOTE,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
