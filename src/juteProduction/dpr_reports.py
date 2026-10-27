"""Daily Production Report (DPR) — one-shot mill summary endpoint.

GET /api/juteProductionReports/daily_production_report
    ?co_id=..&branch_id=..&report_date=YYYY-MM-DD

Aggregates the whole mill day into one payload: spinning (shift-wise +
quality-wise), winding, weaving (quality + shift pivots), finishing/bale press,
yarn heavy-light SQC, jute stock position, worker hands, and an extra-machine-run
analysis — each with month-to-date context. Sections whose data the ERP does not
capture yet (twisting, bale stock, PW/ply sale yarn) are declared in
`unavailable` so the page can surface them as data-capture gaps instead of
silently showing zeros.

Day-level spinning and weaving reuse the planning-grid day slices verbatim;
see dprQueries.py for the MTD/aux builders.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    SPINNING_MACHINE_TYPE_NAME,
    WEAVING_MACHINE_TYPE_NAME,
)
from src.juteProduction.dprQueries import (
    get_dpr_emulsion_query,
    get_dpr_finishing_query,
    get_dpr_hands_by_dept_query,
    get_dpr_hands_query,
    get_dpr_machine_count_query,
    get_dpr_power_query,
    get_dpr_purchased_yarn_issue_query,
    get_dpr_shifts_worked_query,
    get_dpr_spinning_daily_production_query,
    get_dpr_spinning_mtd_query,
    get_dpr_weaving_mtd_query,
    get_dpr_winding_mtd_query,
    get_dpr_yarn_heavy_light_query,
)
from src.juteProduction.dpr_hands_stages import (
    FALLBACK_BASIS_LABEL,
    STAGE_BASIS_LABEL,
    build_hands_stages,
    build_yarn_balance,
    ratio_or_none,
)
from src.juteProduction.spinning_query import get_spinning_planning_slice_query
from src.juteProduction.weaving_lock import fetch_weaving_day_rows
from src.juteProduction.winding_query import get_winding_reconciliation_query
from src.juteProduction.winding_stock_query import fetch_twisted_yarn_item_ids
from src.juteProcurement.reportQueries import get_jute_stock_report_query


router = APIRouter()

SHIFT_ORDER = ("A", "B", "C")

# Order the shifts-worked table reads in (production flow order).
SHIFTS_WORKED_SECTION_ORDER = ("Spinning", "Winding", "Weaving", "Finishing")

# Sections the ERP has no capture for yet — surfaced to the page so the gaps
# are visible (and actionable) instead of silently zero.
UNAVAILABLE_SECTIONS = [
    {
        "section": "Twisting Production",
        "reason": "No twisting entry screen exists yet — ply output is keyed as "
        "winding, so it is split off the winding total on the yarn master's "
        "twisted flag and listed in its own block under the winder table. The "
        "single yarn CONSUMED by twisting is still not captured anywhere.",
    },
    {
        "section": "Bale / Loose Stock",
        "reason": "Press production is captured (finishing), but bale stock "
        "movements (issue/dispatch) are not, so stock cannot be derived.",
    },
    {
        "section": "PW / Sale Yarn Production",
        "reason": "Plied sale-yarn production has no entry screen yet.",
    },
    {
        "section": "Jute Arrival Break-up",
        "reason": "Arrivals are captured at MR level; bale-cut vs bale-uncut "
        "re-selection split is not recorded.",
    },
]


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _optional_branch_id(request: Request) -> Optional[int]:
    raw = request.query_params.get("branch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id format")


def fetch_purchased_yarn_issue(db: Session, binds: dict) -> Optional[dict]:
    """Approved purchased-yarn issues net of returns (kg, day + MTD), or None
    on a tenant that has no yarn purchase tables (TKT-000185).

    Same swallow-and-degrade as fetch_twisted_yarn_item_ids: the Yarn Purchase
    migration (20260925_yarn_purchase_issue_tables.sql) is live on dev3 and sls
    only, and a missing table raises 1146 — the sheet must print "not tracked",
    never 500. None and {0, 0} mean different things (cannot tell vs nothing
    issued) and the yarn balance says which.
    """
    try:
        row = db.execute(get_dpr_purchased_yarn_issue_query(), binds).first()
    except (OperationalError, ProgrammingError):
        # The failed statement would otherwise poison every later query.
        db.rollback()
        return None
    m = dict(row._mapping) if row is not None else {}
    return {"day_kg": _f(m.get("day_kg")), "mtd_kg": _f(m.get("mtd_kg"))}


def fetch_power_readings(db: Session, binds: dict) -> Optional[dict]:
    """The DPR Power line's one aggregate row (ITM-004), or None on a tenant that
    has not run 20261018_jute_power_reading.sql.

    Same swallow-and-degrade as fetch_purchased_yarn_issue: a missing table
    raises 1146 and the failed statement must be rolled back so the queries
    after it are not poisoned. None means "cannot tell" (section omitted); an
    empty dict means the table exists but nothing is keyed (section printed as
    "not keyed").
    """
    try:
        row = db.execute(get_dpr_power_query(), binds).first()
    except (OperationalError, ProgrammingError):
        db.rollback()
        return None
    return dict(row._mapping) if row is not None else {}


def build_power_section(
    row: Optional[dict],
    day_yarn_t: float,
    day_cloth_t: float,
) -> Optional[dict]:
    """The DPR "power" section from the aggregate row (pure).

    None when the row is None (table absent on this tenant). keyed = at least
    one reading for the report date; the day block is all None when not keyed
    and the MTD block is None-valued when no day of the month was keyed, so a
    missing entry never prints as 0 kWh. Day ratios are kWh per tonne of the
    DPR's own day totals (cloth = weaving, yarn = spinning). MTD ratios divide
    the kWh of the KEYED days by the tonnes of those same days
    (``mtd_keyed_cloth_mt`` / ``mtd_keyed_yarn_kg`` on the row, see
    get_dpr_power_query) -- the rule /power_report's MTD block applies, so the
    two screens print the same MTD kWh/t for a month; the full-month tonnes
    would understate it by keyed/total days. Ratios are None (not 0) when
    there is no tonnage to divide by.
    """
    if row is None:
        return None
    day_rows = int(_f(row.get("day_rows")))
    mtd_days = int(_f(row.get("mtd_days")))
    keyed = day_rows > 0
    mtd_cloth_t = _f(row.get("mtd_keyed_cloth_mt"))
    mtd_yarn_t = _f(row.get("mtd_keyed_yarn_kg")) / 1000.0

    def block(kwh, dg_kwh, dg_hours, dg_litres, cloth_t, yarn_t, present: bool) -> dict:
        if not present:
            return {
                "kwh": None,
                "grid_kwh": None,
                "dg_kwh": None,
                "dg_hours": None,
                "dg_litres": None,
                "kwh_per_t_cloth": None,
                "kwh_per_t_yarn": None,
                "dg_share_pct": None,
            }
        kwh = _f(kwh)
        dg_kwh = _f(dg_kwh)
        return {
            "kwh": _round(kwh),
            "grid_kwh": _round(kwh - dg_kwh),
            "dg_kwh": _round(dg_kwh),
            "dg_hours": _round(_f(dg_hours)),
            "dg_litres": _round(_f(dg_litres)),
            "kwh_per_t_cloth": ratio_or_none(kwh, _f(cloth_t)),
            "kwh_per_t_yarn": ratio_or_none(kwh, _f(yarn_t)),
            "dg_share_pct": _round(dg_kwh * 100.0 / kwh) if kwh > 0 else None,
        }

    mtd = block(
        row.get("mtd_kwh"),
        row.get("mtd_dg_kwh"),
        row.get("mtd_dg_hours"),
        row.get("mtd_dg_litres"),
        mtd_cloth_t,
        mtd_yarn_t,
        mtd_days > 0,
    )
    mtd["days"] = mtd_days
    mtd["cloth_t"] = _round(mtd_cloth_t, 3)
    mtd["yarn_t"] = _round(mtd_yarn_t, 3)
    mtd["tonnes_basis"] = "keyed days"
    return {
        "keyed": keyed,
        "day": block(
            row.get("day_kwh"),
            row.get("day_dg_kwh"),
            row.get("day_dg_hours"),
            row.get("day_dg_litres"),
            day_cloth_t,
            day_yarn_t,
            keyed,
        ),
        "mtd": mtd,
    }


def _f(v, default=0.0) -> float:
    """Decimal/None → float for JSON."""
    if v is None:
        return default
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _round(v: float, nd: int = 2) -> float:
    return round(v + 0.0, nd)


def _pct(num: float, den: float, nd: int = 2) -> float:
    return _round(num * 100.0 / den, nd) if den else 0.0


def _ratio(num: float, den: float, nd: int = 2) -> float:
    return _round(num / den, nd) if den else 0.0


def _eff_pct(num: float, den: float, nd: int = 2):
    """Efficiency % — None (not 0.0) when there is no standard to divide by.

    TKT-000116: sls co 2 has no quality-level ('qid') tpi/eff rows in
    jute_prod_spng_target_map, so std_prod collapses to 0 for every frame and
    the page showed a confident "0.0%" for a mill running near normal. A blank
    reads as "not configured", which is what it is; only _pct's zero-den default
    made the missing standard look like measured failure.
    """
    return _round(num * 100.0 / den, nd) if den else None


# ---------------------------------------------------------------------------
# Section builders (pure aggregation over already-fetched rows)
# ---------------------------------------------------------------------------


# TKT-000201 (owner ruling 2026-10-13): the DPR's "SQC actual count" is the
# MOISTURE-CORRECTED count (jute_sqc_spinning_count.corrected_count — the
# observed count brought to the quality's standard moisture regain) from
# October 2026 on. Earlier months keep the observed count so reports already
# circulated do not change. The slice's std production stays on the observed
# count (what the Spinning Target Map's 100%-production formula uses).
SQC_CORRECTED_COUNT_FROM = date(2026, 10, 1)


def sqc_count_basis(report_d: date) -> str:
    """Which SQC count the DPR reports for `report_d`: "corrected" from
    SQC_CORRECTED_COUNT_FROM on, "observed" before (see the note above)."""
    return "corrected" if report_d >= SQC_CORRECTED_COUNT_FROM else "observed"


def _sqc_count(row: dict, count_basis: str) -> float:
    """The slice row's SQC count on the requested basis. "corrected" reads
    act_corr_count (the day's AVG corrected_count) and falls back to the
    observed act_count when no corrected figure exists, so a quality never
    drops to 0 just because the moisture correction is missing."""
    if count_basis == "corrected":
        corr = _f(row.get("act_corr_count"))
        if corr > 0:
            return corr
    return _f(row.get("act_count"))


def build_spinning_avg_count(quality: dict) -> dict:
    """Mill average yarn count on three weightings, for two counts side by side
    (TKT-000173, TKT-000201).

    The three weightings (unchanged from TKT-000173):

      by_frames          sum(count x frame-shifts run)   / sum(frame-shifts run)
      by_production      sum(count x actual kg doffed)   / sum(actual kg doffed)
      by_std_production  sum(count x std expected kg)    / sum(std expected kg)

    frame-shifts = distinct (frame, shift) that produced; actual kg =
    daily_doff_tbl.net_weight; std expected kg = the slice's std_prod
    (100%-production from the Spinning Target Map's std speed / spindles / TPI
    and the day's SQC count, times the std efficiency) on the frames that ran.

    The two counts averaged:

      by_*      the quality's STANDARD count (jute_yarn_mst.jute_yarn_count,
                lb/spyndle — the slice's std_count). A quality with no count on
                the yarn master is left out and named in count_missing.
      act_by_*  the quality's ACTUAL count from Spinning SQC (R-08-16): the day's
                AVG of jute_sqc_spinning_count.corrected_count (moisture-corrected;
                observed_count for days before SQC_CORRECTED_COUNT_FROM — see
                sqc_count_basis / _sqc_count), the same figure as the quality
                table's "Act Count" column. A quality with no SQC reading for the
                day falls back to its standard count — the best available estimate,
                and dropping it would skew the mix towards the measured qualities —
                and is named in act_fallback; act_measured_kg says how much of the
                day's kg had a reading. A quality with neither count is left out
                of the actual figures (it is already in count_missing).

    A quality with no std production (std_prod = 0: no std TPI / efficiency
    entered, or no SQC count for the day) is LEFT OUT of the std-weighted
    figures, not counted as zero, and those are None when no quality has one —
    a blank means a master-data / SQC-entry gap, not a fault.

    `quality`: build_spinning_section's per-item accumulator (production_kg,
    std_prod_kg, std_count, _count_wt, _frame_shifts).
    """
    std = {"frames_w": 0.0, "frames_n": 0.0, "prod_w": 0.0, "prod_n": 0.0,
           "std_w": 0.0, "std_n": 0.0}
    act = dict(std)
    std_qualities = 0
    counted = 0
    count_missing = []
    act_fallback = []
    act_measured_kg = 0.0

    def _add(acc: dict, count: float, frame_shifts: int, q: dict) -> None:
        acc["frames_w"] += count * frame_shifts
        acc["frames_n"] += frame_shifts
        acc["prod_w"] += count * q["production_kg"]
        acc["prod_n"] += q["production_kg"]
        if q["std_prod_kg"] > 0:
            acc["std_w"] += count * q["std_prod_kg"]
            acc["std_n"] += q["std_prod_kg"]

    for q in quality.values():
        name = q["item_name"] or q["item_code"] or str(q["item_id"])
        frame_shifts = len(q["_frame_shifts"])
        std_count = q["std_count"]
        # The day's SQC count, production-weighted over the slice rows (one
        # value per quality-day, so this is just that value).
        sqc_count = (
            q.get("_count_wt", 0.0) / q["production_kg"] if q["production_kg"] > 0 else 0.0
        )
        if std_count:
            counted += 1
            _add(std, std_count, frame_shifts, q)
            if q["std_prod_kg"] > 0:
                std_qualities += 1
        else:
            count_missing.append(name)
        if sqc_count > 0:
            act_measured_kg += q["production_kg"]
            _add(act, sqc_count, frame_shifts, q)
        elif std_count:
            act_fallback.append(name)
            _add(act, std_count, frame_shifts, q)

    def _avg(w: float, n: float):
        return _round(w / n, 2) if n else None

    return {
        "by_frames": _avg(std["frames_w"], std["frames_n"]),
        "frame_shifts": int(std["frames_n"]),
        "by_production": _avg(std["prod_w"], std["prod_n"]),
        "production_kg": _round(std["prod_n"], 3),
        "by_std_production": _avg(std["std_w"], std["std_n"]),
        "std_production_kg": _round(std["std_n"], 3),
        # How much of the mix the std-weighted figure actually covers.
        "std_qualities": std_qualities,
        "qualities": counted,
        "count_missing": sorted(count_missing),
        # TKT-000201: the same three weightings on the SQC actual count.
        "act_by_frames": _avg(act["frames_w"], act["frames_n"]),
        "act_by_production": _avg(act["prod_w"], act["prod_n"]),
        "act_by_std_production": _avg(act["std_w"], act["std_n"]),
        "act_measured_kg": _round(act_measured_kg, 3),
        "act_fallback": sorted(act_fallback),
    }


# TKT-000234: why a quality's Target/Frame-Shift or Eff % cannot be computed.
# The slice derives both from p100prod = std speed x minutes x SQC count x
# spindles / std TPI (machine std speed + spindles, quality std TPI, the day's
# SQC observed count), then std eff % (-> Eff %) and target eff % (-> Target).
# Any of those inputs absent collapses the figure to 0, which the page used to
# print as a confident "0.0". Each code names one missing input and where the
# mill enters it; order = display order.
SPINNING_MISSING_REASONS = {
    "machine_std": (
        "Frame standard speed / spindles not set",
        "Spinning Target Map > Machine > Standard",
    ),
    "std_tpi": (
        "Quality standard TPI not set",
        "Spinning Target Map > Quality > Standard",
    ),
    "std_eff": (
        "Quality standard Eff % not set",
        "Spinning Target Map > Quality > Standard",
    ),
    "target_eff": (
        "Quality target Eff % not set",
        "Spinning Target Map > Quality > Target",
    ),
    # Owner ruling 2026-10-24 (TKT-000234): no target Eff % but a standard ->
    # the target falls back to the standard (target_prod = std_prod), flagged.
    "target_eff_fallback": (
        "Target uses Standard Eff % (Target Eff % not set)",
        "Spinning Target Map > Quality > Target",
    ),
    "sqc_count": (
        "No Spinning SQC count entered for the day",
        "Spinning SQC (count entry)",
    ),
    "no_capacity": (
        "100% production worked out as 0 (check spell working hours)",
        "Spell master",
    ),
}

# Slice inputs checked per producing row: code -> slice column. A column absent
# from the row (older callers / test stubs) is never reported as missing.
_QUALITY_INPUTS = (
    ("std_tpi", "std_tpi"),
    ("std_eff", "std_eff"),
    ("target_eff", "target_eff"),
    # p100prod runs on the OBSERVED count (act_count), whatever count_basis the
    # table displays, so that is the reading whose absence blanks the figures.
    ("sqc_count", "act_count"),
)


def _missing_input(row: dict, col: str) -> bool:
    return col in row and _f(row.get(col)) <= 0


def _target_falls_back(row: dict) -> bool:
    """No target production (no target Eff %) but a standard one exists — the
    row's target falls back to its standard (owner ruling 2026-10-24)."""
    return _f(row.get("target_prod")) <= 0 and _f(row.get("std_prod")) > 0


def _row_target(row: dict) -> float:
    """Target kg of one slice row, with the standard fallback applied — used by
    the quality table AND the shift/total aggregates so they agree."""
    return _f(row.get("std_prod")) if _target_falls_back(row) else _f(row.get("target_prod"))


def _spinning_missing_summary(quality_rows: list) -> list:
    """Section-level roll-up of the per-quality `missing` codes, in display
    order: one entry per reason with the quality names it applies to."""
    out = []
    for code, (label, where) in SPINNING_MISSING_REASONS.items():
        names = [
            (q["item_name"] or q["item_code"] or str(q["item_id"]))
            for q in quality_rows
            if code in q["missing"]
        ]
        if names:
            out.append({"code": code, "label": label, "where": where, "qualities": names})
    return out


def build_spinning_section(
    slice_rows: list, mtd_rows: list, total_frames: int, count_basis: str = "observed"
) -> dict:
    """Shift-wise + quality-wise spinning aggregates from the day-slice rows.

    `count_basis` ("observed" | "corrected") picks which SQC count the quality
    rows' act_count and the avg_count act_* figures are on — see
    sqc_count_basis (TKT-000201). It does not touch std production / eff%.
    """
    shifts: dict = {}
    quality: dict = {}
    frames_by_shift: dict = {}
    for r in slice_rows:
        bucket = r.get("shift_bucket") or "?"
        prod = _f(r.get("act_prod_doff"))
        s = shifts.setdefault(
            bucket,
            {
                "shift": bucket,
                "production_kg": 0.0,
                "std_prod_kg": 0.0,
                "target_prod_kg": 0.0,
                "measured_kg": 0.0,
            },
        )
        s["production_kg"] += prod
        # std/target only for rows that produced: the slice fans today-only
        # idle helper rows (act_prod_doff = 0) across spells, and counting
        # their standards would dilute eff% / avg-per-frame for report_date =
        # today vs the same date re-run tomorrow (helper rows gone).
        if prod > 0:
            std = _f(r.get("std_prod"))
            s["std_prod_kg"] += std
            s["target_prod_kg"] += _row_target(r)
            # Efficiency numerator counts ONLY output that has a standard behind
            # it. Dividing all production by the standards of the configured
            # subset reads as a spectacular 190%+ when half the qualities are
            # unconfigured — a worse lie than the 0% this ticket started from.
            if std > 0:
                s["measured_kg"] += prod
            frames_by_shift.setdefault(bucket, set()).add(r.get("machine_id"))

        item_key = r.get("item_id")
        if item_key is not None and prod > 0:
            q = quality.setdefault(
                item_key,
                {
                    "item_id": item_key,
                    "item_code": r.get("item_code"),
                    "item_name": r.get("item_name"),
                    "production_kg": 0.0,
                    "std_prod_kg": 0.0,
                    "target_prod_kg": 0.0,
                    "winding_kg": 0.0,
                    "_count_wt": 0.0,
                    "std_count": _f(r.get("std_count")),
                    # (machine, shift_bucket) set: the slice is at (frame, spell,
                    # item) grain and A1/A2 collapse into one bucket, so a plain
                    # row counter would count that frame-shift twice.
                    "_frame_shifts": set(),
                    # TKT-000234: missing slice inputs seen on producing rows.
                    "_missing": set(),
                    "_frames_no_std": set(),
                    "_has_p100": "p100prod" in r,
                    "_target_fallback": False,
                    "_std_eff": 0.0,
                },
            )
            if _target_falls_back(r):
                q["_target_fallback"] = True
                q["_std_eff"] = max(q["_std_eff"], _f(r.get("std_eff")))
            for code, col in _QUALITY_INPUTS:
                if _missing_input(r, col):
                    q["_missing"].add(code)
            if _missing_input(r, "std_speed") or _missing_input(r, "spindles"):
                q["_frames_no_std"].add(r.get("mech_code") or str(r.get("machine_id")))
            q["production_kg"] += prod
            q["std_prod_kg"] += _f(r.get("std_prod"))
            q["target_prod_kg"] += _row_target(r)
            q["winding_kg"] += _f(r.get("act_prod_wind"))
            q["_count_wt"] += _sqc_count(r, count_basis) * prod
            q["_frame_shifts"].add((r.get("machine_id"), bucket))

    shift_rows = []
    total = {
        "production_kg": 0.0,
        "std_prod_kg": 0.0,
        "target_prod_kg": 0.0,
        "measured_kg": 0.0,
        "frame_runs": 0,
    }
    # A/B/C first, then any unexpected bucket (e.g. a spell missing from
    # spell_mst) so nothing silently drops out of the totals.
    extra_buckets = sorted(b for b in shifts if b not in SHIFT_ORDER)
    for bucket in (*SHIFT_ORDER, *extra_buckets):
        s = shifts.get(bucket)
        if not s:
            continue
        frames = len(frames_by_shift.get(bucket, ()))
        row = {
            "shift": bucket,
            "production_kg": _round(s["production_kg"], 3),
            "production_mt": _round(s["production_kg"] / 1000.0, 3),
            "frame_runs": frames,
            "kg_per_frame": _ratio(s["production_kg"], frames, 1),
            "utilization_pct": _pct(frames, total_frames),
            "eff_pct": _eff_pct(s["measured_kg"], s["std_prod_kg"]),
        }
        shift_rows.append(row)
        total["production_kg"] += s["production_kg"]
        total["std_prod_kg"] += s["std_prod_kg"]
        total["target_prod_kg"] += s["target_prod_kg"]
        total["measured_kg"] += s["measured_kg"]
        total["frame_runs"] += frames

    quality_rows = []
    for q in quality.values():
        prod = q["production_kg"]
        frame_shifts = len(q["_frame_shifts"])
        eff_pct = _eff_pct(prod, q["std_prod_kg"])
        # Blank (None), not 0.0, when no target could be computed (TKT-000234).
        target_pfs = (
            _ratio(q["target_prod_kg"], frame_shifts, 1) if q["target_prod_kg"] > 0 else None
        )
        missing = set(q["_missing"])
        if q["_target_fallback"]:
            # The target is shown (on the standard) — say so instead of "missing".
            missing.discard("target_eff")
            missing.add("target_eff_fallback")
        if q["_frames_no_std"]:
            missing.add("machine_std")
        if (eff_pct is None or target_pfs is None) and not missing and q["_has_p100"]:
            missing.add("no_capacity")
        quality_rows.append(
            {
                "item_id": q["item_id"],
                "item_code": q["item_code"],
                "item_name": q["item_name"],
                "act_count": _ratio(q["_count_wt"], prod),
                "std_count": _round(q["std_count"], 2),
                "production_kg": _round(prod, 3),
                "frame_shifts": frame_shifts,
                "kg_per_frame_shift": _ratio(prod, frame_shifts, 1),
                "target_per_frame_shift": target_pfs,
                "winding_kg": _round(q["winding_kg"], 3),
                "eff_pct": eff_pct,
                # Codes from SPINNING_MISSING_REASONS, display order; the page
                # and PDF explain a blank cell with them.
                "missing": [c for c in SPINNING_MISSING_REASONS if c in missing],
                "missing_frames": sorted(q["_frames_no_std"]),
                # "target" | "standard" (fallback, TKT-000234) | None (blank).
                "target_basis": (
                    None if target_pfs is None
                    else "standard" if q["_target_fallback"] else "target"
                ),
                # The std Eff % the fallback target used (None unless fallback).
                "target_std_eff_pct": (
                    _round(q["_std_eff"], 2)
                    if q["_target_fallback"] and q["_std_eff"] > 0 else None
                ),
            }
        )
    quality_rows.sort(key=lambda x: (x["item_code"] or ""))

    # Qualities that produced but have no standard to measure against — the page
    # names them so the mill can go and configure them instead of reading a blank
    # as a report failure (TKT-000116).
    standards_missing = [
        (q["item_name"] or q["item_code"] or str(q["item_id"]))
        for q in quality_rows
        if q["eff_pct"] is None
    ]
    # Same for the Target/Frame-Shift column (TKT-000234).
    targets_missing = [
        (q["item_name"] or q["item_code"] or str(q["item_id"]))
        for q in quality_rows
        if q["target_per_frame_shift"] is None
    ]

    mtd = {"production_kg": 0.0, "frame_runs": 0}
    for r in mtd_rows:
        mtd["production_kg"] += _f(r.get("production_kg"))
        mtd["frame_runs"] += int(r.get("frame_runs") or 0)

    return {
        "shifts": shift_rows,
        "total": {
            "production_kg": _round(total["production_kg"], 3),
            "production_mt": _round(total["production_kg"] / 1000.0, 3),
            "frame_runs": total["frame_runs"],
            "kg_per_frame": _ratio(total["production_kg"], total["frame_runs"], 1),
            "utilization_pct": _pct(total["frame_runs"], total_frames * len(SHIFT_ORDER)),
            "eff_pct": _eff_pct(total["measured_kg"], total["std_prod_kg"]),
            "avg_target_per_frame": _ratio(total["target_prod_kg"], total["frame_runs"], 1),
            "avg_std_per_frame": _ratio(total["std_prod_kg"], total["frame_runs"], 1),
        },
        "mtd": {
            "production_kg": _round(mtd["production_kg"], 3),
            "production_mt": _round(mtd["production_kg"] / 1000.0, 3),
            "frame_runs": mtd["frame_runs"],
            "kg_per_frame": _ratio(mtd["production_kg"], mtd["frame_runs"], 1),
        },
        "total_frames": total_frames,
        "quality": quality_rows,
        "standards_missing": standards_missing,
        "targets_missing": targets_missing,
        "missing_summary": _spinning_missing_summary(quality_rows),
        "avg_count": {**build_spinning_avg_count(quality), "count_basis": count_basis},
    }


def build_winding_section(
    day_rows: list, mtd_rows: list, twisted_item_ids: set, twisting_available: bool = True
) -> dict:
    """Quality-wise reconciled winding production (kg) + person productivity.

    Twisting is netted out of the totals. Twisting has no module of its own, so
    the mill keys ply output into the SAME winding doff entry — meaning twisted
    yarn is wound once as single yarn and booked a second time as ply, and a
    plain SUM of the doffs counts that tonnage twice. Same discriminator and the
    same treatment the winding↔weaving difference report already uses
    (`split_twisting_mt`, TKT-000127): the yarn master's is_twisted flag.

    Per-quality rows are left whole and merely tagged — the twisted quality's own
    output is real and the winder who made it earned it. Only the TOTAL, which is
    the figure that gets double counted, is reported net (with the gross and the
    twisting split beside it so nothing is hidden).

    TKT-000174: twisting gets its own block on the page, after the winding
    Total, so `total` now describes winding ALONE on every column — kg, winders
    and kg/winder — and `total.twisting` / `total.gross` carry the same three
    columns for the twisting block and the gross line under it. Before, the
    Total row mixed bases (net kg beside gross winders and a gross kg/winder),
    which is what made the twisted row hard to place. Twisting stays OUTSIDE
    the winding total; the gross line is the only place the two are added, and
    it is labelled as winder output, not production.
    """
    by_item: dict = {}
    winders_by_item: dict = {}
    for r in day_rows:
        item_key = r.get("item_id")
        q = by_item.setdefault(
            item_key,
            {
                "item_id": item_key,
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name"),
                "production_kg": 0.0,
            },
        )
        q["production_kg"] += _f(r.get("production_kg"))
        winders_by_item.setdefault(item_key, set()).add(r.get("eb_id"))

    # Qualities with MTD output but nothing today still get a (zero) row so the
    # MTD column adds up to the total's MTD.
    for r in mtd_rows:
        by_item.setdefault(
            r.get("item_id"),
            {
                "item_id": r.get("item_id"),
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name"),
                "production_kg": 0.0,
            },
        )

    mtd_by_item = {r.get("item_id"): r for r in mtd_rows}
    rows = []
    total_kg = 0.0
    twisting_kg = 0.0
    mtd_twisting_kg = 0.0
    winding_winders = 0
    twisting_winders = 0
    for item_key, q in by_item.items():
        winders = len(winders_by_item.get(item_key, ()))
        mtd = mtd_by_item.get(item_key, {})
        mtd_kg = _f(mtd.get("production_kg"))
        is_twisted = 1 if item_key in twisted_item_ids else 0
        rows.append(
            {
                **q,
                "production_kg": _round(q["production_kg"], 3),
                "winders": winders,
                "kg_per_winder": _ratio(q["production_kg"], winders, 1),
                "mtd_production_kg": mtd_kg,
                "mtd_kg_per_winder_shift": _ratio(
                    mtd_kg, int(mtd.get("winder_shifts") or 0), 1
                ),
                "is_twisted": is_twisted,
            }
        )
        # Each block divides its own kg by its own winders. A winder on a
        # twisting frame did work the shift — they are counted, in the twisting
        # block — so neither kg/winder is inflated by dropping them, and the
        # winding figure is no longer net kg over a gross head count.
        if is_twisted:
            twisting_kg += q["production_kg"]
            mtd_twisting_kg += mtd_kg
            twisting_winders += winders
        else:
            total_kg += q["production_kg"]
            winding_winders += winders
    rows.sort(key=lambda x: (x["item_code"] or ""))
    mtd_gross = sum(_f(r.get("production_kg")) for r in mtd_rows)
    mtd_net = mtd_gross - mtd_twisting_kg
    gross_kg = total_kg + twisting_kg
    gross_winders = winding_winders + twisting_winders
    return {
        "qualities": rows,
        "total": {
            "production_kg": _round(total_kg, 3),
            "winders": winding_winders,
            "kg_per_winder": _ratio(total_kg, winding_winders, 1),
            "mtd_production_kg": _round(mtd_net, 3),
            "twisting_kg": _round(twisting_kg, 3),
            "mtd_twisting_kg": _round(mtd_twisting_kg, 3),
            "gross_production_kg": _round(gross_kg, 3),
            "mtd_gross_production_kg": _round(mtd_gross, 3),
            "twisting": {
                "production_kg": _round(twisting_kg, 3),
                "winders": twisting_winders,
                "kg_per_winder": _ratio(twisting_kg, twisting_winders, 1),
                "mtd_production_kg": _round(mtd_twisting_kg, 3),
            },
            "gross": {
                "production_kg": _round(gross_kg, 3),
                "winders": gross_winders,
                "kg_per_winder": _ratio(gross_kg, gross_winders, 1),
                "mtd_production_kg": _round(mtd_gross, 3),
            },
        },
        # False = this tenant has not run the is_twisted migration, so nothing
        # could be split and the total is still gross. Say so rather than let a
        # zero read as "no twisting happened".
        "twisting_available": twisting_available,
    }


def build_weaving_section(slice_rows: list, mtd_rows: list, total_looms: int) -> dict:
    """Quality-wise + shift-wise weaving aggregates from the day-slice rows."""
    by_quality: dict = {}
    looms_by_quality: dict = {}
    looms_by_shift: dict = {}
    shift_pivot_total = {b: 0.0 for b in SHIFT_ORDER}
    std_capacity_kg = 0.0
    for r in slice_rows:
        qid = r.get("weaving_quality_id")
        if qid is None:
            # Loom not mapped to a quality yet: the slice LEFT JOINs the quality
            # table, so such rows carry NULL code/name and always 0 kg
            # (ozs_yds coalesced to 0). Skip rather than emit a blank row.
            continue
        # get_weaving_entries_by_date_query projects spell_code but NOT the
        # slice-internal shift_bucket — derive it here (A1/A2 -> A, etc.).
        spell_code = str(r.get("spell_code") or "")
        bucket = r.get("shift_bucket") or (spell_code[:1] if spell_code else "?")
        kg = _f(r.get("production_kg"))
        q = by_quality.setdefault(
            qid,
            {
                "weaving_quality_id": qid,
                "quality_code": r.get("weaving_quality_code"),
                "quality_name": r.get("weaving_quality_name"),
                "production_kg": 0.0,
                "std_prod_yds": 0.0,
                "production_yds": 0.0,
                **{f"shift_{b}_kg": 0.0 for b in SHIFT_ORDER},
            },
        )
        q["production_kg"] += kg
        q["production_yds"] += _f(r.get("production_yds"))
        q["std_prod_yds"] += _f(r.get("std_prod_yds"))
        # 100%-efficiency capacity in kg for this row — the std basis of the
        # machine-run analysis (exact per row, not backed out of rounded aggregates).
        std_capacity_kg += _f(r.get("std_prod_yds")) * _f(r.get("ozs_yds")) * 28.35 / 1000.0
        if bucket in SHIFT_ORDER:
            q[f"shift_{bucket}_kg"] += kg
            shift_pivot_total[bucket] += kg
        if kg > 0:
            looms_by_quality.setdefault(qid, set()).add(r.get("machine_id"))
            looms_by_shift.setdefault(bucket, set()).add(r.get("machine_id"))

    # Qualities with MTD output but nothing today still get a (zero) row so the
    # MTD column adds up to the total's MTD.
    for r in mtd_rows:
        by_quality.setdefault(
            r.get("weaving_quality_id"),
            {
                "weaving_quality_id": r.get("weaving_quality_id"),
                "quality_code": r.get("weaving_quality_code"),
                "quality_name": r.get("weaving_quality_name"),
                "production_kg": 0.0,
                "std_prod_yds": 0.0,
                "production_yds": 0.0,
                **{f"shift_{b}_kg": 0.0 for b in SHIFT_ORDER},
            },
        )

    mtd_by_qid = {r.get("weaving_quality_id"): r for r in mtd_rows}
    rows = []
    total_kg = 0.0
    total_yds = 0.0
    total_std_yds = 0.0
    total_measured_yds = 0.0
    total_loom_runs = 0
    for qid, q in by_quality.items():
        looms = len(looms_by_quality.get(qid, ()))
        mtd = mtd_by_qid.get(qid, {})
        rows.append(
            {
                "weaving_quality_id": qid,
                "quality_code": q["quality_code"],
                "quality_name": q["quality_name"],
                **{
                    f"shift_{b}_mt": _round(q[f"shift_{b}_kg"] / 1000.0, 3)
                    for b in SHIFT_ORDER
                },
                "production_mt": _round(q["production_kg"] / 1000.0, 3),
                "looms_run": looms,
                "kg_per_loom": _ratio(q["production_kg"], looms, 1),
                "eff_pct": _eff_pct(q["production_yds"], q["std_prod_yds"]),
                "mtd_production_mt": _f(mtd.get("production_mt")),
            }
        )
        total_kg += q["production_kg"]
        total_yds += q["production_yds"]
        total_std_yds += q["std_prod_yds"]
        # Same measured-subset rule as spinning: only cloth with a standard
        # behind it counts toward the efficiency numerator.
        if q["std_prod_yds"] > 0:
            total_measured_yds += q["production_yds"]
        total_loom_runs += looms
    rows.sort(key=lambda x: (x["quality_code"] or ""))

    # Qualities that wove today with no standard to measure against (TKT-000116).
    standards_missing = [
        (r["quality_name"] or r["quality_code"] or str(r["weaving_quality_id"]))
        for r in rows
        if r["eff_pct"] is None and r["production_mt"]
    ]

    looms_run_day = len(set().union(*looms_by_shift.values())) if looms_by_shift else 0
    mtd_total_mt = sum(_f(r.get("production_mt")) for r in mtd_rows)
    return {
        "qualities": rows,
        "shift_totals": {
            b: _round(shift_pivot_total[b] / 1000.0, 3) for b in SHIFT_ORDER
        },
        "total": {
            "production_mt": _round(total_kg / 1000.0, 3),
            "loom_runs": total_loom_runs,
            "looms_run": looms_run_day,
            "kg_per_loom": _ratio(total_kg, total_loom_runs, 1),
            "eff_pct": _eff_pct(total_measured_yds, total_std_yds),
            "utilization_pct": _pct(looms_run_day, total_looms),
            "mtd_production_mt": _round(mtd_total_mt, 3),
            "std_capacity_kg": _round(std_capacity_kg, 1),
        },
        "total_looms": total_looms,
        "standards_missing": standards_missing,
    }


# R-08-16 dead band: within ±0.2 lb of standard the yarn is on-count (the
# printed sheet's `$$`/`$` flags fire only beyond this absolute band).
YARN_COUNT_DEAD_BAND_LB = 0.2


def build_yarn_heavy_light(rows: list) -> list:
    """Attach the heavy/light verdict: (corr - std) / std * 100.

    Positive → yarn heavier than standard, negative → lighter. The HEAVY/LIGHT
    remark applies only beyond the ±0.2 lb absolute dead band (R-08-16); the
    percentage is always reported. Falls back to the observed count when no
    corrected value was stored for the day.
    """
    out = []
    for r in rows:
        std = _f(r.get("std_count"))
        corr = r.get("corr_count")
        obs = r.get("obs_count")
        eff_corr = _f(corr) if corr is not None else (_f(obs) if obs is not None else None)
        hy_lt = (
            _round((eff_corr - std) * 100.0 / std, 2)
            if (eff_corr is not None and std)
            else None
        )
        remark = None
        if hy_lt is not None:
            dev_lb = eff_corr - std
            if abs(dev_lb) <= YARN_COUNT_DEAD_BAND_LB:
                remark = "OK"
            else:
                remark = "HEAVY" if dev_lb > 0 else "LIGHT"
        out.append(
            {
                "item_id": r.get("item_id"),
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name"),
                "std_count": std or None,
                "obs_count": _f(obs) if obs is not None else None,
                "corr_count": _f(corr) if corr is not None else None,
                "day_tests": int(r.get("day_tests") or 0),
                "hy_lt_pct": hy_lt,
                "remark": remark,
                "mtd_obs_count": _f(r.get("mtd_obs_count")) if r.get("mtd_obs_count") is not None else None,
                "mtd_corr_count": _f(r.get("mtd_corr_count")) if r.get("mtd_corr_count") is not None else None,
            }
        )
    return out


# TKT-000172: look-back for the jute cover divisor. Wide enough to hold seven
# producing days across a festival shutdown and to tell a full day from a part
# day; short enough that a stale run rate never stands in.
COVER_LOOKBACK_DAYS = 14
# Producing days averaged when the report date was a holiday / part day.
COVER_AVG_DAYS = 7


def pick_cover_basis(daily_rows: list, report_d) -> Optional[dict]:
    """The spinning production the jute days-of-cover divides by.

    TKT-000172 asked for "total stock divided by the production of the previous
    day". The report is read the next morning FOR the previous day, so that day
    is the report date itself (owner-confirmed).

    When the report date was a holiday or a part day, dividing by it would
    inflate the cover (sls runs Sundays on shift A alone: ~4-6 MT against ~22
    MT, so a Sunday divisor reads as ~4x the real cover). Owner's rule for that
    case: divide by the AVERAGE daily spinning production over the last
    COVER_AVG_DAYS days that had production, ending at the report date — part
    days counted as they are, because that is the mill's real weekly run rate.

    Full day = the report date produced AND ran at least the usual number of
    shifts. "Usual" is the look-back window's most common shift count (not its
    maximum), so a mill that steps down from three shifts to two is not treated
    as running part days for the whole window; a tie takes the lower count.

    `daily_rows`: {tran_date, production_kg, shifts} per day, from
    get_dpr_spinning_daily_production_query(). Returns None when nothing was
    produced in the window — the caller shows "—", never a made-up cover.

    Result: mode "report_date" (production_kg = that day's kg, days = 1) or
    "average" (production_kg = kg PER DAY over `days` producing days spanning
    date_from..date_to). `date` is always date_to.
    """
    days = sorted(
        (
            r
            for r in daily_rows
            if r.get("tran_date") is not None
            and r.get("tran_date") <= report_d
            and _f(r.get("production_kg")) > 0
        ),
        key=lambda r: r.get("tran_date"),
    )
    if not days:
        return None
    counts = Counter(int(r.get("shifts") or 0) for r in days)
    top = max(counts.values())
    usual_shifts = min(c for c, n in counts.items() if n == top)

    last = days[-1]
    if last.get("tran_date") == report_d and int(last.get("shifts") or 0) >= usual_shifts:
        return {
            "mode": "report_date",
            "date": report_d.isoformat(),
            "date_from": report_d.isoformat(),
            "date_to": report_d.isoformat(),
            "days": 1,
            "production_kg": _round(_f(last.get("production_kg")), 3),
            "shifts": int(last.get("shifts") or 0),
            "is_report_date": True,
            "basis_label": "spinning production",
        }

    span = days[-COVER_AVG_DAYS:]
    total_kg = sum(_f(r.get("production_kg")) for r in span)
    date_to = span[-1].get("tran_date")
    return {
        "mode": "average",
        "date": date_to.isoformat(),
        "date_from": span[0].get("tran_date").isoformat(),
        "date_to": date_to.isoformat(),
        "days": len(span),
        "production_kg": _round(total_kg / len(span), 3),
        "shifts": None,
        "is_report_date": False,
        "basis_label": "average daily spinning production",
    }


def build_jute_section(rows: list, num_days: int, cover_basis: Optional[dict] = None) -> dict:
    """Roll the itemwise jute stock report up to a stock position summary.

    days_stock (TKT-000172) = total closing stock / `cover_basis` production —
    the report date's own spinning production, or the 7-producing-day average
    when the report date was a holiday / part day (see pick_cover_basis). Both
    sides are kg: the stock report's weights and daily_doff_tbl.net_weight.

    It used to be closing / (MTD jute issue / calendar days). That divisor only
    moves when someone keys a jute issue, and issue entry lags production — on
    sls br 29 nothing was issued after 14-09-2026, so the same 193,779 kg was
    spread over one more day each morning and the cover crept 8.0 -> 10.0 ->
    11.7 days while the mill was spinning ~24 MT a day. The old figure still
    ships as days_stock_mtd_issue so the two can be compared; the page does not
    lead with it.
    """
    totals = {
        "opening_weight": 0.0,
        "receipt_weight": 0.0,
        "issue_weight": 0.0,
        "sold_weight": 0.0,
        "closing_weight": 0.0,
        "mtd_receipt_weight": 0.0,
        "mtd_issue_weight": 0.0,
    }
    groups: dict = {}
    # Owner on TKT-000172: waste is batched with the jute, so it stays in the
    # total, but jute stock and waste stock are stated separately. is_waste is
    # a property of the item group, so a group is wholly one or the other.
    jute_totals = {k: 0.0 for k in totals}
    waste_totals = {k: 0.0 for k in totals}
    for r in rows:
        is_waste = bool(r.get("is_waste"))
        split = waste_totals if is_waste else jute_totals
        for k in totals:
            totals[k] += _f(r.get(k))
            split[k] += _f(r.get(k))
        g = groups.setdefault(
            r.get("item_group_name") or "Other",
            {
                "item_group_name": r.get("item_group_name") or "Other",
                "is_waste": is_waste,
                **{k: 0.0 for k in totals},
            },
        )
        for k in totals:
            g[k] += _f(r.get(k))
    for g in groups.values():
        for k in totals:
            g[k] = _round(g[k], 3)
    waste_closing = waste_totals["closing_weight"]
    avg_daily_issue = totals["mtd_issue_weight"] / num_days if num_days else 0.0
    basis_kg = _f(cover_basis.get("production_kg")) if cover_basis else 0.0
    raw_closing = totals["closing_weight"] - waste_closing
    return {
        # Jute groups first, waste groups after, so the page can subtotal each.
        "groups": sorted(groups.values(), key=lambda x: (x["is_waste"], x["item_group_name"])),
        "totals": {k: _round(v, 3) for k, v in totals.items()},
        "jute_totals": {k: _round(v, 3) for k, v in jute_totals.items()},
        "waste_totals": {k: _round(v, 3) for k, v in waste_totals.items()},
        # None (not 0) when there is no production day to divide by — 0 would
        # read as "no cover".
        "days_stock": _ratio(totals["closing_weight"], basis_kg, 1) if basis_kg else None,
        # Process waste (dust, caddies, thread waste ...) sits in the same stock
        # and the ticket said TOTAL stock, so the headline keeps it; this is the
        # cover on purchased raw jute alone, shown beside it.
        "days_stock_raw_jute": _ratio(raw_closing, basis_kg, 1) if basis_kg else None,
        "days_stock_waste": _ratio(waste_closing, basis_kg, 1) if basis_kg else None,
        "raw_jute_closing_weight": _round(raw_closing, 3),
        "waste_closing_weight": _round(waste_closing, 3),
        "cover_basis": cover_basis,
        "days_stock_mtd_issue": _ratio(totals["closing_weight"], avg_daily_issue, 1)
        if avg_daily_issue
        else None,
    }


def build_hands_section(rows: list) -> dict:
    shift_rows = []
    total = {"day_hours": 0.0, "day_hands": 0.0, "mtd_hours": 0.0, "mtd_hands": 0.0}
    by_bucket = {r.get("shift_bucket"): r for r in rows}
    for bucket in SHIFT_ORDER:
        r = by_bucket.get(bucket)
        if not r:
            continue
        row = {
            "shift": bucket,
            "day_hours": _f(r.get("day_hours")),
            "day_hands": _f(r.get("day_hands")),
            "mtd_hours": _f(r.get("mtd_hours")),
            "mtd_hands": _f(r.get("mtd_hands")),
        }
        shift_rows.append(row)
        for k in total:
            total[k] += row[k]
    # Any rows with unexpected buckets still count toward totals.
    for bucket, r in by_bucket.items():
        if bucket not in SHIFT_ORDER and r is not None:
            for k in total:
                total[k] += _f(r.get(k))
    return {"shifts": shift_rows, "total": {k: _round(v, 2) for k, v in total.items()}}


def build_shifts_worked(rows: list, report_d) -> dict:
    """How many shifts the mill worked on the day and month-to-date (TKT-000152).

    `rows` are distinct (section, tran_date, shift_bucket) triples from
    get_dpr_shifts_worked_query(). The mill figure is the UNION over sections —
    a shift in which spinning AND weaving both ran is ONE worked shift, not two
    — while each section keeps its own count, because a mill routinely runs
    three spinning shifts against two weaving ones.
    """
    day_buckets: set = set()
    mtd_pairs: set = set()
    per: dict = {}
    for r in rows:
        section = r.get("section") or "?"
        d = r.get("tran_date")
        bucket = r.get("shift_bucket") or "?"
        p = per.setdefault(section, {"day": set(), "mtd": set()})
        mtd_pairs.add((d, bucket))
        p["mtd"].add((d, bucket))
        if d == report_d:
            day_buckets.add(bucket)
            p["day"].add(bucket)

    def _sorted(buckets):
        # Known shifts in A/B/C order first, anything unexpected after.
        return [b for b in SHIFT_ORDER if b in buckets] + sorted(
            b for b in buckets if b not in SHIFT_ORDER
        )

    sections = [
        {
            "section": name,
            "day": len(per[name]["day"]),
            "mtd": len(per[name]["mtd"]),
            "day_shifts": _sorted(per[name]["day"]),
        }
        for name in list(SHIFTS_WORKED_SECTION_ORDER)
        + sorted(n for n in per if n not in SHIFTS_WORKED_SECTION_ORDER)
        if name in per
    ]
    return {
        "day": len(day_buckets),
        "mtd": len(mtd_pairs),
        "day_shifts": _sorted(day_buckets),
        "sections": sections,
    }


def build_emulsion_section(row, report_date=None) -> Optional[dict]:
    """Batching-oil usage + the OK/LOW/HIGH verdict against the logged std band.

    TKT-000116 asked for "JBO / Ecofin %" on the DPR. The only place the ERP
    holds it is the R-08-02 emulsion log, whose oil_pct_in_emulsion IS that
    percentage. Returns None when the window has no log at all, so the page can
    drop the section rather than show a table of dashes.
    """
    if row is None:
        return None
    r = dict(row._mapping)
    mtd_days = int(r.get("mtd_days") or 0)
    if not mtd_days:
        return None

    day = None
    if int(r.get("day_rows") or 0):
        oil_pct = r.get("day_oil_pct")
        low, high = r.get("day_std_low"), r.get("day_std_high")
        status = None
        if oil_pct is not None and low is not None and high is not None:
            status = (
                "LOW"
                if _f(oil_pct) < _f(low)
                else "HIGH"
                if _f(oil_pct) > _f(high)
                else "OK"
            )
        tank = _f(r.get("day_tank_capacity_ltr"))
        day = {
            "entry_date": report_date.isoformat() if report_date else None,
            "jbo_ltr": _round(_f(r.get("day_jbo_ltr")), 2),
            "eco_fin_ltr": _round(_f(r.get("day_eco_fin_ltr")), 2),
            "oil_used_ltr": _round(_f(r.get("day_oil_used_ltr")), 2),
            "tank_capacity_ltr": _round(tank, 2),
            "oil_pct": _round(_f(oil_pct), 2) if oil_pct is not None else None,
            # Shown beside the measured value on purpose: the two disagree in
            # the live data (150/1000 = 15.0 vs a measured 15.5), and silently
            # picking one would hide a recipe-vs-tank discrepancy.
            "derived_oil_pct": _pct(_f(r.get("day_oil_used_ltr")), tank) if tank else None,
            "std_low": _round(_f(low), 2) if low is not None else None,
            "std_high": _round(_f(high), 2) if high is not None else None,
            "status": status,
        }

    mtd_pct = r.get("mtd_avg_oil_pct")
    return {
        "day": day,
        "mtd": {
            "jbo_ltr": _round(_f(r.get("mtd_jbo_ltr")), 2),
            "eco_fin_ltr": _round(_f(r.get("mtd_eco_fin_ltr")), 2),
            "avg_oil_pct": _round(_f(mtd_pct), 2) if mtd_pct is not None else None,
            "days": mtd_days,
        },
    }


def build_machine_run(spinning: dict, weaving: dict) -> list:
    """Extra-machine-run analysis: how many machines the day's output justified
    vs how many actually ran — on both std and target productivity bases."""
    out = []
    sp_total = spinning["total"]
    for basis, avg_key in (("std", "avg_std_per_frame"), ("target", "avg_target_per_frame")):
        avg = sp_total.get(avg_key) or 0.0
        should = _ratio(sp_total["production_kg"], avg) if avg else 0.0
        out.append(
            {
                "section": "Spinning",
                "basis": basis,
                "avg_per_machine": avg,
                "avg_act_per_machine": sp_total["kg_per_frame"],
                "total_prod_kg": sp_total["production_kg"],
                "should_run": should,
                "act_run": sp_total["frame_runs"],
                "excess_machines": _round(sp_total["frame_runs"] - should, 2) if avg else None,
            }
        )
    wv_total = weaving["total"]
    total_kg = wv_total["production_mt"] * 1000.0
    runs = wv_total["loom_runs"]
    # Weaving std basis: per-row 100%-efficiency capacity summed exactly in
    # build_weaving_section (std_prod_yds * ozs_yds), so mixed hessian/sacking
    # days don't skew the standard toward the heavier cloth.
    capacity = wv_total.get("std_capacity_kg") or 0.0
    if runs and capacity:
        avg_std = _ratio(capacity, runs, 1)
        should = _ratio(total_kg, avg_std) if avg_std else 0.0
        out.append(
            {
                "section": "Weaving",
                "basis": "std",
                "avg_per_machine": avg_std,
                "avg_act_per_machine": wv_total["kg_per_loom"],
                "total_prod_kg": _round(total_kg, 1),
                "should_run": should,
                "act_run": runs,
                "excess_machines": _round(runs - should, 2),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def build_dpr_data(db: Session, co_id: int, branch_id: Optional[int], report_d: date) -> dict:
    """Build the whole DPR payload (the dict the endpoint returns under "data").

    Request-free so it can be reused outside the HTTP path (e.g. the server-side
    PDF in dpr_pdf.py). Raises HTTPException(400) when branch_id does not belong
    to co_id; any other failure propagates to the caller.
    """
    date_from = report_d.replace(day=1)
    num_days = (report_d - date_from).days + 1

    # The winding and jute-stock sections are branch-keyed with no co
    # predicate, so a branch belonging to another company of the tenant
    # would silently report that company's figures under this co_id's
    # header — reject a mismatched pair up front.
    if branch_id is not None:
        pair_ok = db.execute(
            text(
                "SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"
            ),
            {"branch_id": branch_id, "co_id": co_id},
        ).first()
        if not pair_ok:
            raise HTTPException(
                status_code=400,
                detail="branch_id does not belong to co_id",
            )

    scope = {"co_id": co_id, "branch_id": branch_id}
    range_binds = {"date_from": date_from, "report_date": report_d, **scope}

    # Installed machine counts (utilization denominators).
    mc_rows = db.execute(
        get_dpr_machine_count_query(),
        {
            **scope,
            "spinning_type": SPINNING_MACHINE_TYPE_NAME,
            "weaving_type": WEAVING_MACHINE_TYPE_NAME,
        },
    ).fetchall()
    totals_by_type = {
        (r._mapping["machine_type_name"] or "").strip().lower(): int(
            r._mapping["total_machines"]
        )
        for r in mc_rows
    }
    total_frames = totals_by_type.get(SPINNING_MACHINE_TYPE_NAME.lower(), 0)
    total_looms = totals_by_type.get(WEAVING_MACHINE_TYPE_NAME.lower(), 0)

    # Spinning: day slice (reused planning-grid compute) + raw MTD.
    sp_slice = [
        dict(r._mapping)
        for r in db.execute(
            get_spinning_planning_slice_query(),
            {
                **scope,
                "tran_date": report_d,
                "spell_id": None,
                "spinning_type": SPINNING_MACHINE_TYPE_NAME,
            },
        ).fetchall()
    ]
    sp_mtd = [
        dict(r._mapping)
        for r in db.execute(
            get_dpr_spinning_mtd_query(),
            {**range_binds, "spinning_type": SPINNING_MACHINE_TYPE_NAME},
        ).fetchall()
    ]
    spinning = build_spinning_section(
        sp_slice, sp_mtd, total_frames, count_basis=sqc_count_basis(report_d)
    )

    # Winding (branch-keyed): day reconciliation + MTD per quality.
    winding = None
    if branch_id is not None:
        wd_day = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_reconciliation_query(),
                {
                    "branch_id": branch_id,
                    "tran_date": report_d,
                    "spell_id": None,
                    "item_id": None,
                },
            ).fetchall()
        ]
        wd_mtd = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_winding_mtd_query(),
                {
                    "branch_id": branch_id,
                    "date_from": date_from,
                    "report_date": report_d,
                },
            ).fetchall()
        ]
        # Ply output rides the winding doff entry (no twisting module), so
        # it is split off the winding totals on the yarn master's flag —
        # the same discriminator the winding↔weaving difference report uses.
        # Empty set on a tenant that has not run the is_twisted migration.
        twisted_ids = fetch_twisted_yarn_item_ids(db)
        winding = build_winding_section(
            wd_day,
            wd_mtd,
            twisted_ids or set(),
            twisting_available=twisted_ids is not None,
        )

    # Weaving: fresh stored log rows per spell (live slice for stale/unsynced
    # spells) + stored-formula MTD.
    wv_slice = fetch_weaving_day_rows(
        db, scope["co_id"], report_d, scope.get("branch_id")
    )
    wv_mtd = [
        dict(r._mapping)
        for r in db.execute(get_dpr_weaving_mtd_query(), range_binds).fetchall()
    ]
    weaving = build_weaving_section(wv_slice, wv_mtd, total_looms)

    # Finishing (incl. bale press).
    finishing_rows = [
        dict(r._mapping)
        for r in db.execute(get_dpr_finishing_query(), range_binds).fetchall()
    ]
    finishing = {
        "processes": [
            {
                "process": r["process"],
                "uom": r["prod_uom"],
                "day_qty": _f(r["day_qty"]),
                "day_wt_kg": _f(r["day_wt_kg"]),
                "mtd_qty": _f(r["mtd_qty"]),
                "mtd_wt_kg": _f(r["mtd_wt_kg"]),
            }
            for r in finishing_rows
        ]
    }

    # Batching oil — JBO / Ecofin (R-08-02 emulsion log).
    emulsion = build_emulsion_section(
        db.execute(get_dpr_emulsion_query(), range_binds).first(), report_d
    )

    # Yarn heavy-light (R-08-16 SQC counts).
    hl_rows = [
        dict(r._mapping)
        for r in db.execute(get_dpr_yarn_heavy_light_query(), range_binds).fetchall()
    ]
    yarn_heavy_light = build_yarn_heavy_light(hl_rows)

    # Jute stock position (branch-keyed report reused as-is).
    jute = None
    if branch_id is not None:
        jute_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_jute_stock_report_query(),
                {"branch_id": branch_id, "report_date": report_d.isoformat()},
            ).fetchall()
        ]
        # Days of cover = closing stock / the report date's spinning
        # production, or the 7-producing-day average on a holiday / part
        # day (TKT-000172).
        daily_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_spinning_daily_production_query(),
                {
                    **scope,
                    "window_from": report_d - timedelta(days=COVER_LOOKBACK_DAYS),
                    "report_date": report_d,
                    "spinning_type": SPINNING_MACHINE_TYPE_NAME,
                },
            ).fetchall()
        ]
        jute = build_jute_section(
            jute_rows, num_days, pick_cover_basis(daily_rows, report_d)
        )

    # Worker hands.
    hands_rows = [
        dict(r._mapping)
        for r in db.execute(get_dpr_hands_query(), range_binds).fetchall()
    ]
    hands = build_hands_section(hands_rows)
    # The same hands per department of the worked designation, for the
    # stage-wise hands/MT below (TKT-000185).
    hands_dept_rows = [
        dict(r._mapping)
        for r in db.execute(get_dpr_hands_by_dept_query(), range_binds).fetchall()
    ]

    # Shifts worked (day + MTD) — the per-shift averages above are read
    # against it (TKT-000152).
    shifts_worked = build_shifts_worked(
        [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_shifts_worked_query(),
                {**range_binds, "spinning_type": SPINNING_MACHINE_TYPE_NAME},
            ).fetchall()
        ],
        report_d,
    )

    # Purchased yarn issued to the floor (approved Yarn Issues net of returns)
    # for the yarn balance line; None on a tenant without the yarn tables.
    purchased = fetch_purchased_yarn_issue(db, range_binds)

    # Power line (ITM-004): day / MTD kWh, DG hours and kWh per tonne -- the
    # day over the DPR's own cloth and yarn totals, the MTD over the tonnes of
    # the keyed days (carried on the row, so it reconciles with /power_report).
    # None on a tenant without the table, keyed False when nothing was entered
    # for the date. Deliberately the LAST query of the sheet so older tests
    # only append one result.
    power = build_power_section(
        fetch_power_readings(
            db, {**range_binds, "spinning_type": SPINNING_MACHINE_TYPE_NAME}
        ),
        spinning["total"]["production_mt"],
        weaving["total"]["production_mt"],
    )

    # Labour productivity (TKT-000185): hands per MT of cloth, stage-wise —
    # yarn-stage hands ÷ spinning MT + cloth-stage hands ÷ weaving MT, common
    # hands spread pro-rata. It replaces the TKT-000117 "all hands ÷ spinning
    # output" figure, which could not see purchased yarn (woven here, never
    # spun here) or surplus yarn (spun, not woven) — see dpr_hands_stages.py.
    # The TKT-000117 basis stays as the fallback for a tenant whose attendance
    # carries no department at all, so the old golden case still reconciles.
    day_prod_mt = spinning["total"]["production_mt"]
    mtd_prod_mt = spinning["mtd"]["production_mt"]
    stages = build_hands_stages(
        hands_dept_rows,
        day_prod_mt,
        mtd_prod_mt,
        weaving["total"]["production_mt"],
        weaving["total"]["mtd_production_mt"],
    )
    hands["stages"] = stages
    # The TKT-000117 figure (all hands ÷ spinning output) stays on the sheet
    # beside the stage-wise one, for continuity with earlier reports (owner,
    # 2026-10-11); it is also what the fallback shows.
    hands["spinning_basis"] = {
        "label": "All hands ÷ spinning output (previous basis)",
        "day_hands": hands["total"]["day_hands"],
        "mtd_hands": hands["total"]["mtd_hands"],
        "day_mt": _round(day_prod_mt, 3),
        "mtd_mt": _round(mtd_prod_mt, 3),
        "hands_per_mt": ratio_or_none(hands["total"]["day_hands"], day_prod_mt),
        "mtd_hands_per_mt": ratio_or_none(hands["total"]["mtd_hands"], mtd_prod_mt),
    }
    if stages["available"]:
        hands["hands_per_mt"] = stages["total"]["hands_per_mt"]
        hands["mtd_hands_per_mt"] = stages["total"]["mtd_hands_per_mt"]
        hands["basis_label"] = STAGE_BASIS_LABEL
    else:
        hands["hands_per_mt"] = hands["spinning_basis"]["hands_per_mt"]
        hands["mtd_hands_per_mt"] = hands["spinning_basis"]["mtd_hands_per_mt"]
        hands["basis_label"] = FALLBACK_BASIS_LABEL
    # Spun + purchased − woven: the yarn that went to stock or sale, so a day
    # where spinning and weaving disagree explains itself on the sheet.
    hands["yarn_balance"] = build_yarn_balance(
        day_prod_mt,
        mtd_prod_mt,
        weaving["total"]["production_mt"],
        weaving["total"]["mtd_production_mt"],
        purchased["day_kg"] if purchased else None,
        purchased["mtd_kg"] if purchased else None,
        purchased is not None,
    )

    machine_run = build_machine_run(spinning, weaving)

    kpi = {
        "spinning_mt": spinning["total"]["production_mt"],
        "spinning_mtd_mt": spinning["mtd"]["production_mt"],
        "frame_utilization_pct": spinning["total"]["utilization_pct"],
        "spinning_eff_pct": spinning["total"]["eff_pct"],
        "weaving_mt": weaving["total"]["production_mt"],
        "weaving_mtd_mt": weaving["total"]["mtd_production_mt"],
        "loom_utilization_pct": weaving["total"]["utilization_pct"],
        "weaving_eff_pct": weaving["total"]["eff_pct"],
        # Net of twisting — see build_winding_section.
        "winding_kg": winding["total"]["production_kg"] if winding else None,
        "winding_mtd_kg": winding["total"]["mtd_production_kg"] if winding else None,
        "twisting_kg": winding["total"]["twisting_kg"] if winding else None,
        "jbo_pct": emulsion["day"]["oil_pct"] if emulsion and emulsion["day"] else None,
        "total_hands": hands["total"]["day_hands"],
        "shifts_worked": shifts_worked["day"],
        "shifts_worked_mtd": shifts_worked["mtd"],
        # Hands per MT of cloth = yarn stage + cloth stage (TKT-000185); the
        # two addends and the MTD figure ride on the card so it reconciles
        # on screen. hands_stage_basis False = TKT-000117 fallback (all hands
        # ÷ spinning MT) because no department could be resolved.
        "hands_per_mt": hands["hands_per_mt"],
        "hands_per_mt_yarn": stages["yarn"]["hands_per_mt"] if stages["available"] else None,
        "hands_per_mt_cloth": stages["cloth"]["hands_per_mt"] if stages["available"] else None,
        "hands_per_mt_mtd": hands["mtd_hands_per_mt"],
        "hands_stage_basis": stages["available"],
        # The previous figure (all hands ÷ spinning MT), kept beside the new one.
        "hands_per_mt_spinning_basis": hands["spinning_basis"]["hands_per_mt"],
        # Raw stored unit on purpose: jute weights are kg on some tenants
        # and quintal on others (see juteProcurement/issue.py) — never
        # hardcode a unit conversion here.
        "jute_closing_weight": _round(jute["totals"]["closing_weight"], 1)
        if jute
        else None,
        "jute_days_stock": jute["days_stock"] if jute else None,
        # The divisor and its date, so the card reconciles on screen.
        "jute_cover_basis_kg": jute["cover_basis"]["production_kg"]
        if jute and jute["cover_basis"]
        else None,
        "jute_cover_basis_date": jute["cover_basis"]["date"]
        if jute and jute["cover_basis"]
        else None,
        # Power (ITM-004): None when not keyed for the date or table absent.
        "power_kwh": power["day"]["kwh"] if power and power["keyed"] else None,
        "dg_hours": power["day"]["dg_hours"] if power and power["keyed"] else None,
        "kwh_per_t_cloth": power["day"]["kwh_per_t_cloth"] if power and power["keyed"] else None,
    }

    return {
        "report_date": report_d.isoformat(),
        "date_from": date_from.isoformat(),
        "kpi": kpi,
        "spinning": spinning,
        "winding": winding,
        "weaving": weaving,
        "finishing": finishing,
        "emulsion": emulsion,
        "yarn_heavy_light": yarn_heavy_light,
        "jute": jute,
        "hands": hands,
        "shifts_worked": shifts_worked,
        "machine_run": machine_run,
        "power": power,
        "unavailable": UNAVAILABLE_SECTIONS,
    }


@router.get("/daily_production_report")
def daily_production_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    d = request.query_params.get("report_date")
    if not d:
        raise HTTPException(status_code=400, detail="report_date is required")
    try:
        report_d = date.fromisoformat(d)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report_date (YYYY-MM-DD)")
    try:
        return {"data": build_dpr_data(db, co_id, branch_id, report_d)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
