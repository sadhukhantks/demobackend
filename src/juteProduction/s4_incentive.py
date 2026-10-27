"""S4 Incentive Report — GET /api/juteProductionReports/s4_incentive

Port of the legacy code3i report (S4_incentive_report / S4_incentive_report_model,
which read vowsls attendance + EMPMILL12.DAILY_LOOM_DATA): per S4 weaver or S4
helper, the efficiency of every day in the range, average efficiency, days
worked, incentive days and the incentive earned.

``worker_type`` picks the legacy "Loom Type" option:
  * weaver (legacy 41) — the production of the S4 looms the weaver ran;
  * helper (legacy 42) — the production of every S4 loom on the helper line the
    helper attended, for that date and spell (whoever wove on them).

Legacy rules kept as-is:
  * DAY efficiency = ROUND(sum(production) / sum(std production) * 100) over
    that day's loom-spells (whole number, MySQL half-up rounding);
  * the day's incentive = tbl_incentive_data rate + rate_inc (= total_inc) for
    that EXACT whole efficiency (the slab runs 60..100; outside it nothing);
  * average efficiency = ROUND(sum(production) / sum(std production) * 100)
    over the whole range;
  * every day worked counts in "no of days";

Deliberate difference from legacy: loom-spells without a standard production
are left out of both sums (see ``_has_standard``). A helper who attended a line
with no such loom-spell still gets the day, at efficiency 0.
  * rows ordered worst average efficiency first, then emp code.

Production / standard production come from the weaving day-slice
(``fetch_weaving_day_rows``) exactly like the Weaving Low Producer report — no
production formula here.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.dpr_reports import _f
from src.juteProduction.lowprod_weaving_query import (
    get_branch_in_company_query,
    get_weaving_worker_names_query,
)
from src.juteProduction.reportQueries import _expanding
from src.juteProduction.reports import _checklist_scope
from src.juteProduction.weaving_lock import fetch_weaving_day_rows

s4_incentive_router = APIRouter()

MAX_DAYS = 31
# tbl_loom_type 1 = "S4 Loom" (tbl_loom_master_other_details, copied from
# EMPMILL12 — machine ids are sls machine_mst ids).
S4_LOOM_TYPE_ID = 1
WORKER_TYPES = ("weaver", "helper")


def _round0(v: float) -> int:
    """MySQL ROUND(x, 0) — half away from zero, not Python's banker's rounding."""
    return int(Decimal(str(v)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def get_s4_looms_query():
    """S4 looms (the legacy report's loom_type_id = 1) with their helper line."""
    return text(
        "SELECT mechine_id, helper_line_id FROM tbl_loom_master_other_details "
        "WHERE loom_type_id = :loom_type_id"
    )


def get_helper_attendance_query():
    """Who attended which S4 helper line, per date and spell.

    The helper lines are pseudo-machines (machine_mst "S4 LOOM HELPER LINE NO n")
    that only helpers are marked on, so no designation filter is needed — the
    legacy worked_designation_id = 79 selects the same rows."""
    return _expanding(
        """
        SELECT DISTINCT da.eb_id, da.attendance_date, da.spell_id, dea.mc_id
        FROM daily_attendance da
        JOIN daily_ebmc_attendance dea
          ON dea.daily_atten_id = da.daily_atten_id AND dea.is_active = 1
        WHERE da.branch_id = :branch_id AND da.is_active = 1
          AND da.attendance_date BETWEEN :date_from AND :date_to
          AND dea.mc_id IN :line_ids
        """,
        "line_ids",
    )


def _has_standard(r: dict) -> bool:
    """A loom-spell with no standard production (the day-slice gives 0 loom
    hours when no weaver is marked on the loom, or no SQC picks that day) has no
    efficiency. Its production must not count either: production with nothing
    in the denominator pushed a helper line to 142% on 2026-09-05."""
    return _f(r.get("std_prod_yds")) > 0


def weaver_entries(slice_rows: list) -> list:
    """Day-slice rows (already S4-only) -> (eb_id, iso date, production, std)."""
    return [
        (r["eb_id"], _iso(r.get("tran_date")), _f(r.get("production_yds")), _f(r.get("std_prod_yds")))
        for r in slice_rows
        if r.get("eb_id") is not None and _has_standard(r)
    ]


def helper_entries(slice_rows: list, attendance: list, loom_line: dict) -> list:
    """One entry per (helper, date, spell, line) attended: the production of the
    whole line in that spell. ``loom_line`` maps loom machine_id -> helper line."""
    line_tot: dict = {}  # (iso date, spell_id, line) -> [production, std]
    for r in slice_rows:
        if not _has_standard(r):
            continue
        key = (_iso(r.get("tran_date")), r.get("spell_id"), loom_line.get(r.get("machine_id")))
        acc = line_tot.setdefault(key, [0.0, 0.0])
        acc[0] += _f(r.get("production_yds"))
        acc[1] += _f(r.get("std_prod_yds"))
    out = []
    for a in attendance:
        d = _iso(a["attendance_date"])
        prod, std = line_tot.get((d, a["spell_id"], a["mc_id"]), (0.0, 0.0))
        out.append((a["eb_id"], d, prod, std))
    return out


def build_incentive_rows(entries: list, slab: dict, names: dict) -> list:
    """(eb_id, iso date, production, std production) entries -> one row per worker.

    ``slab`` maps whole efficiency -> incentive amount; ``names`` maps eb_id ->
    {eb_no, worker_name}. ``days`` maps ISO date -> that day's efficiency."""
    day_acc: dict = {}  # (eb_id, date) -> [production, std]
    for eb, d, prod, std in entries:
        acc = day_acc.setdefault((eb, d), [0.0, 0.0])
        acc[0] += prod
        acc[1] += std

    workers: dict = {}
    for (eb, d), (prod, std) in day_acc.items():
        eff = _round0(prod / std * 100) if std > 0 else 0
        inc = slab.get(eff, 0.0)
        info = names.get(eb) or {}
        w = workers.setdefault(
            eb,
            {
                "eb_id": eb,
                "eb_no": info.get("eb_no"),
                "worker_name": info.get("worker_name"),
                "days": {},
                "total_days": 0,
                "inc_days": 0,
                "total_inc_amt": 0.0,
                "_prod": 0.0,
                "_std": 0.0,
            },
        )
        w["days"][d] = eff
        w["total_days"] += 1
        if inc > 0:
            w["inc_days"] += 1
        w["total_inc_amt"] += inc
        w["_prod"] += prod
        w["_std"] += std

    out = []
    for w in workers.values():
        prod, std = w.pop("_prod"), w.pop("_std")
        w["avg_eff"] = _round0(prod / std * 100) if std > 0 else 0
        w["total_inc_amt"] = round(w["total_inc_amt"], 2)
        out.append(w)
    out.sort(key=lambda x: (x["avg_eff"], str(x["eb_no"] or x["eb_id"])))
    return out


def get_incentive_slab_query():
    """Whole efficiency -> incentive for the S4 looms' department(s)."""
    return _expanding(
        """
        SELECT i.eff_inc, i.total_inc
        FROM tbl_incentive_data i
        WHERE i.company_id = :co_id AND i.branch_id = :branch_id AND i.is_active = 1
          AND i.department_id IN (
              SELECT m.dept_id FROM machine_mst m WHERE m.machine_id IN :machine_ids)
        """,
        "machine_ids",
    )


@s4_incentive_router.get("/s4_incentive")
def s4_incentive(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _checklist_scope(request, MAX_DAYS)
    if branch_id is None:
        raise HTTPException(status_code=400, detail="branch_id is required")
    worker_type = (request.query_params.get("worker_type") or "weaver").strip().lower()
    if worker_type not in WORKER_TYPES:
        raise HTTPException(status_code=400, detail="worker_type must be weaver or helper")
    eb_no = (request.query_params.get("eb_no") or "").strip().lower()

    try:
        if not db.execute(
            get_branch_in_company_query(), {"branch_id": branch_id, "co_id": co_id}
        ).first():
            raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")

        loom_line = {
            int(r.mechine_id): r.helper_line_id
            for r in db.execute(
                get_s4_looms_query(), {"loom_type_id": S4_LOOM_TYPE_ID}
            ).fetchall()
        }
        slice_rows = []
        d = d_from
        while d <= d_to:
            slice_rows.extend(
                r for r in fetch_weaving_day_rows(db, co_id, d, branch_id)
                if r.get("machine_id") in loom_line
            )
            d += timedelta(days=1)

        if worker_type == "helper":
            line_ids = sorted({v for v in loom_line.values() if v is not None})
            attendance = []
            if line_ids:
                attendance = [
                    dict(r._mapping)
                    for r in db.execute(
                        get_helper_attendance_query(),
                        {"branch_id": branch_id, "date_from": d_from, "date_to": d_to,
                         "line_ids": line_ids},
                    ).fetchall()
                ]
            entries = helper_entries(slice_rows, attendance, loom_line)
        else:
            entries = weaver_entries(slice_rows)

        machine_ids = sorted({r["machine_id"] for r in slice_rows if r.get("machine_id")})
        slab = {}
        if machine_ids:
            slab = {
                _round0(_f(r.eff_inc)): _f(r.total_inc)
                for r in db.execute(
                    get_incentive_slab_query(),
                    {"co_id": co_id, "branch_id": branch_id, "machine_ids": machine_ids},
                ).fetchall()
            }

        eb_ids = sorted({e[0] for e in entries})
        names = {}
        if eb_ids:
            names = {
                r._mapping["eb_id"]: dict(r._mapping)
                for r in db.execute(get_weaving_worker_names_query(), {"eb_ids": eb_ids}).fetchall()
            }

        rows = build_incentive_rows(entries, slab, names)
        if eb_no:
            rows = [
                w for w in rows
                if eb_no in (str(w["eb_no"] or "").lower(), str(w["eb_id"]))
            ]

        dates = []
        d = d_from
        while d <= d_to:
            dates.append(d.isoformat())
            d += timedelta(days=1)

        notes = []
        if not machine_ids:
            notes.append("No S4 loom production found for this branch and range.")
        elif not slab:
            notes.append("No incentive slab (tbl_incentive_data) is configured for this branch.")
        return {
            "data": {
                "dates": dates,
                "rows": rows,
                "total_inc_amt": round(sum(w["total_inc_amt"] for w in rows), 2),
                "notes": notes,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
