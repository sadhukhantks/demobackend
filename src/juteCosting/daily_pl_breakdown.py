"""Daily P&L — lazy head breakdowns and the jute-issue data check
(GET /api/juteCostReports/daily_pl/breakdown, GET .../daily_pl/jute_issue_checks)
— ITM-027 §1.5 / §1.6.

The statement on the Daily P&L page (daily_pl.py) shows six heads; four of
them (raw_material, stores, power, wages) open a dialog that fetches ITS
breakdown here, on demand, so the report call stays one aggregated row per
day. Each breakdown is a handful of small tables (top-N where the dimension
is open-ended — never a line dump) over exactly the scope of the head's day
query (daily_pl_breakdown_query.py reuses daily_pl_query's fragments).

The jute-issue data check answers the owner's ask that raw material "depends
on the batch": it pulls the range's issue lines once, with the MR line / MR
header / quality / stock-view balance beside each, and runs the flag rules
(``flag_issue_rows``, pure) — zero weight, no booked value, MR line without
a rate, booked value off the MR rate, MR rate far from the period median of
that quality, item mismatch with the MR line, MR not yet approved, MR
rejected / cancelled / returned, possible double entry (three or more
identical lines on one MR line and day), over-issue against the MR line,
future date — plus the days that
produced cloth with no jute issued and the period's jute kg per kg of cloth
against the standard. Only flagged lines come back, with the suggested value
(weight x MR rate) so the user can go back to the Jute Issue screen of that
day and re-evaluate the line.

Same persona as daily_pl.py: validation before the session is touched
(parse_range_params), {"data": ...} responses, _rows / _fetch_optional,
HTTPException passthrough and 500 on anything else.
"""

import statistics
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteCosting.daily_pl import (
    JUTE_PER_KG_CLOTH_STD,
    POWER_TYPE_LABELS,
    STORES_CATEGORY_LABELS,
    _as_date,
    _f,
    _fetch_optional,
    _ratio,
    _rows,
    parse_range_params,
)
from src.juteCosting.daily_pl_breakdown_query import (
    get_hands_by_department_query,
    get_hands_by_designation_query,
    get_hands_by_spell_query,
    get_hours_profile_query,
    get_jute_issue_by_day_query,
    get_jute_issue_by_mr_query,
    get_jute_issue_by_quality_query,
    get_jute_issue_check_rows_query,
    get_jute_issue_waste_ledger_query,
    get_jute_waste_issue_by_item_query,
    get_power_by_day_type_query,
    get_power_by_meter_query,
    get_stores_by_category_query,
    get_stores_by_dept_query,
    get_stores_by_expense_type_query,
    get_stores_by_group_query,
    get_stores_top_items_query,
)
from src.juteCosting.daily_pl_query import get_cloth_by_quality_daily_query
from src.juteCosting.params import param_value, resolve_params, today_ist
from src.juteCosting.params_query import get_cost_param_rows_for_resolution_query
from src.juteProcurement.constants import JUTE_MR_APPROVED_STATUSES
from src.juteProduction.doff_analysis import _iso, _verify_pair

router = APIRouter()

HEAD_RAW_MATERIAL = "raw_material"
HEAD_STORES = "stores"
HEAD_POWER = "power"
HEAD_WAGES = "wages"
BREAKDOWN_HEADS = (HEAD_RAW_MATERIAL, HEAD_STORES, HEAD_POWER, HEAD_WAGES)
INVALID_HEAD_MSG = f"Invalid head — one of {', '.join(BREAKDOWN_HEADS)}"

# --- Data-check thresholds (one line of reason each) -------------------------
# Booked value may legitimately differ from weight x MR rate by moisture /
# claim adjustments; beyond 5 % it is a different rate or a keying error.
VALUE_RATE_TOLERANCE = 0.05
# An MR rate 20 % away from the period median of its quality is a different
# grade booked under the same item, or a wrong rate on the MR line.
RATE_OUTLIER_TOLERANCE = 0.20
# A median of fewer than 3 rated lines is not a benchmark — no outlier flag.
RATE_OUTLIER_MIN_LINES = 3
# The stock view rounds to 3 dp; only a balance below -1 kg is a real over-issue.
OVER_ISSUE_TOLERANCE_KG = 1.0
# Jute kg per kg of cloth: below 0.90 is impossible (less jute than cloth —
# issues missing), above 1.25 means issues booked that did not go to the floor.
RATIO_LOW = 0.90
RATIO_HIGH = 1.25
# Informational only (dup_seconds on each row): two identical lines keyed
# within 120 s would be a double entry, but on sls the Jute Issue screen saves
# a whole day in one go, so every line of a save carries the same
# update_date_time and the window cannot tell a double entry from a repeated
# standard trolley weight — the flag does not use it (owner, 2026-10-20).
DUPLICATE_WINDOW_SECONDS = 120
# The DUPLICATE rule: three or more identical lines (date, MR line, weight)
# on one MR line — that many equal trolleys on one line is worth a look; an
# identical PAIR is normal and is only counted (summary.identical_pairs).
DUPLICATE_GROUP_MIN = 3
# MR header states that are merely not approved YET — in flight, the jute has
# physically arrived (juteProcurement/constants.py, stock eligibility): 1 Open,
# 17 Pending approval level 2, 20 Pending approval, 21 Draft, 24 Transfer
# pending. The floor issues against them, but the rate may still change.
# Anything else outside JUTE_MR_APPROVED_STATUSES (3 Approved, 13 Pending
# chain root) is rejected / cancelled / returned.
MR_OPEN_STATUSES = (1, 17, 20, 21, 24)

FLAG_ZERO_WEIGHT = "ZERO_WEIGHT"
FLAG_NO_VALUE = "NO_VALUE"
FLAG_NO_MR_RATE = "NO_MR_RATE"
FLAG_VALUE_RATE_MISMATCH = "VALUE_RATE_MISMATCH"
FLAG_RATE_OUTLIER = "RATE_OUTLIER"
FLAG_ITEM_MISMATCH = "ITEM_MISMATCH"
FLAG_MR_OPEN = "MR_OPEN"
FLAG_MR_STATUS = "MR_STATUS"
FLAG_DUPLICATE = "DUPLICATE"
FLAG_OVER_ISSUE = "OVER_ISSUE"
FLAG_FUTURE_DATE = "FUTURE_DATE"
FLAG_NO_ISSUE_DAY = "NO_ISSUE_DAY"

FLAG_LABELS = {
    FLAG_ZERO_WEIGHT: "Weight 0",
    FLAG_NO_VALUE: "No issue value booked",
    FLAG_NO_MR_RATE: "MR line has no rate",
    FLAG_VALUE_RATE_MISMATCH: "Booked value does not match the MR rate",
    FLAG_RATE_OUTLIER: "MR rate far from the period median for this quality",
    FLAG_ITEM_MISMATCH: "Issue item differs from the MR line item",
    FLAG_MR_OPEN: "MR not yet approved — rate may change",
    FLAG_MR_STATUS: "MR rejected / cancelled / returned",
    FLAG_DUPLICATE: "Possible double entry (3+ identical lines)",
    FLAG_OVER_ISSUE: "Issued more than the MR line holds",
    FLAG_FUTURE_DATE: "Dated in the future",
    FLAG_NO_ISSUE_DAY: "Cloth produced, no jute issued",
}
LINE_FLAGS = tuple(k for k in FLAG_LABELS if k != FLAG_NO_ISSUE_DAY)

NOTE_EXPENSE_TYPES = (
    "Expense types 7 'Capital' and 4 'Overhauling' are included in stores — decide "
    "later whether to move them out."
)
NOTE_POWER_NOT_TRACKED = "Power readings are not tracked on this tenant (jute_power_reading missing)."


def _note_waste_ledger(lines: int, kg: float, value: float) -> str:
    booked = "booked ₹0 by design" if value == 0 else f"booked ₹{value:,.2f}"
    return (
        f"{lines:,} waste-ledger line{'' if lines == 1 else 's'} ({kg / 1000.0:,.1f} t, {booked}) "
        f"{'is' if lines == 1 else 'are'} outside this check"
    )


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def _n(v):
    """DECIMAL -> float, date -> ISO, everything else as is (JSON-ready)."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):
        return _iso(v)
    return v


def _norm(rows: Optional[List[dict]]) -> List[dict]:
    return [{k: _n(v) for k, v in r.items()} for r in (rows or [])]


def _int(v) -> int:
    return int(_f(v))


def _round_or_none(v, dp: int) -> Optional[float]:
    return None if v is None else round(float(v), dp)


def _as_datetime(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Breakdown builders (pure)
# ---------------------------------------------------------------------------


def build_raw_material_breakdown(by_quality, by_mr, by_day, waste_by_item) -> dict:
    """§1.5 raw_material: by_quality (avg_rate_qtl = booked value / valued kg
    x 100), by_mr (top 30 MRs by kg — one row per MR: its lines, distinct
    qualities ``items`` with their names joined in ``item_name``, and the
    kg-weighted MR rate of its rated lines, None when none is rated), by_day,
    waste_by_item and a summary."""
    qualities = []
    for r in _norm(by_quality):
        qualities.append(
            {
                "item_id": r.get("item_id"),
                "item_name": r.get("item_name"),
                "lines": _int(r.get("lines")),
                "kg": round(_f(r.get("kg")), 3),
                "value": round(_f(r.get("value")), 2),
                "avg_rate_qtl": _ratio(_f(r.get("value")) * 100.0, _f(r.get("valued_kg"))),
                "zero_value_lines": _int(r.get("zero_value_lines")),
                "mr_rated_value": round(_f(r.get("mr_rated_value")), 2),
            }
        )
    total_kg = round(sum(q["kg"] for q in qualities), 3)
    total_value = round(sum(q["value"] for q in qualities), 2)
    valued_kg = sum(_f(r.get("valued_kg")) for r in by_quality or [])
    return {
        "by_quality": qualities,
        "by_mr": [
            {
                "jute_mr_id": r.get("jute_mr_id"),
                "branch_mr_no": r.get("branch_mr_no"),
                "jute_gate_entry_no": r.get("jute_gate_entry_no"),
                "mr_date": r.get("mr_date"),
                "item_name": r.get("item_name"),
                "items": _int(r.get("items")),
                "mr_rate_qtl": _round_or_none(r.get("mr_rate_qtl"), 2),
                "kg": round(_f(r.get("kg")), 3),
                "value": round(_f(r.get("value")), 2),
                "lines": _int(r.get("lines")),
            }
            for r in _norm(by_mr)
        ],
        "by_day": [
            {
                "tran_date": r.get("tran_date"),
                "kg": round(_f(r.get("kg")), 3),
                "value": round(_f(r.get("value")), 2),
                "lines": _int(r.get("lines")),
                "zero_value_lines": _int(r.get("zero_value_lines")),
            }
            for r in _norm(by_day)
        ],
        "waste_by_item": [
            {
                "item_name": r.get("item_name"),
                "kg": round(_f(r.get("kg")), 3),
                "value": round(_f(r.get("value")), 2),
            }
            for r in _norm(waste_by_item)
        ],
        "summary": {
            "lines": sum(q["lines"] for q in qualities),
            "kg": total_kg,
            "value": total_value,
            "avg_rate_qtl": _ratio(total_value * 100.0, valued_kg),
            "zero_value_lines": sum(q["zero_value_lines"] for q in qualities),
            "mr_rated_value": round(sum(q["mr_rated_value"] for q in qualities), 2),
        },
    }


def _value_lines(rows, *keys) -> List[dict]:
    out = []
    for r in _norm(rows):
        row = {k: r.get(k) for k in keys}
        row["value"] = round(_f(r.get("value")), 2)
        row["lines"] = _int(r.get("lines"))
        out.append(row)
    return out


def build_stores_breakdown(by_category, by_group, by_dept, by_expense_type, top_items) -> dict:
    """§1.5 stores: by_category (labelled), by_group top 25, by_dept top 15,
    by_expense_type (Capital / Overhauling shown, see NOTE_EXPENSE_TYPES),
    top_items top 25 and a summary. ``by_category`` arrives UNROUNDED from
    SQL: each row is rounded to 2 dp for display, and summary.value is the
    unrounded sum rounded ONCE — the P&L stores line's rule, so the dialog
    total matches it to the paisa (summing rounded categories drifted)."""
    categories = _value_lines(by_category, "category")
    total_value = round(sum(_f(r.get("value")) for r in by_category or []), 2)
    for c in categories:
        c["label"] = STORES_CATEGORY_LABELS.get(c["category"], c["category"])
    return {
        "by_category": categories,
        "by_group": _value_lines(by_group, "item_grp_name", "category"),
        "by_dept": _value_lines(by_dept, "dept_id", "dept_desc"),
        "by_expense_type": _value_lines(by_expense_type, "expense_type_id", "expense_type_name"),
        "top_items": [
            {
                "item_name": r.get("item_name"),
                "item_grp_name": r.get("item_grp_name"),
                "qty": round(_f(r.get("qty")), 3),
                "value": round(_f(r.get("value")), 2),
            }
            for r in _norm(top_items)
        ],
        "summary": {
            "value": total_value,
            "lines": sum(c["lines"] for c in categories),
        },
        "notes": [NOTE_EXPENSE_TYPES],
    }


def build_power_breakdown(by_meter: Optional[list], by_day_type: Optional[list]) -> dict:
    """§1.5 power: by_meter, by_day_type; ``available`` False with empty
    tables when the readings table is not on the tenant."""
    if by_meter is None:
        return {
            "available": False,
            "by_meter": [],
            "by_day_type": [],
            "summary": {"kwh": None, "dg_hours": None, "dg_litres": None},
            "notes": [NOTE_POWER_NOT_TRACKED],
        }
    meters = [
        {
            "meter_code": r.get("meter_code"),
            "meter_name": r.get("meter_name"),
            "meter_type": r.get("meter_type"),
            "meter_type_label": POWER_TYPE_LABELS.get(r.get("meter_type"), r.get("meter_type")),
            "kwh": round(_f(r.get("kwh")), 2),
            "dg_hours": round(_f(r.get("dg_hours")), 2),
            "dg_litres": round(_f(r.get("dg_litres")), 2),
            "max_demand_kva": _n(r.get("max_demand_kva")),
            "days": _int(r.get("days")),
        }
        for r in _norm(by_meter)
    ]
    return {
        "available": True,
        "by_meter": meters,
        "by_day_type": [
            {
                "tran_date": r.get("tran_date"),
                "meter_type": r.get("meter_type"),
                "kwh": round(_f(r.get("kwh")), 2),
            }
            for r in _norm(by_day_type)
        ],
        "summary": {
            "kwh": round(sum(m["kwh"] for m in meters), 2),
            "dg_hours": round(sum(m["dg_hours"] for m in meters), 2),
            "dg_litres": round(sum(m["dg_litres"] for m in meters), 2),
        },
        "notes": [],
    }


def _hands_lines(rows, *keys) -> List[dict]:
    out = []
    for r in _norm(rows):
        row = {k: r.get(k) for k in keys}
        row["rows"] = _int(r.get("rows"))
        row["hours"] = round(_f(r.get("hours")), 2)
        row["hands"] = round(_f(r.get("hands")), 2)
        out.append(row)
    return out


def build_wages_breakdown(by_spell, by_designation, by_department, hours_profile, rate_pick) -> dict:
    """§1.5 wages: by_spell, by_designation top 15, by_department, the
    working-hours profile, the WAGE_PER_HAND_DAY in force on date_to (with
    its source) and a summary (hands x rate). Department is routed through
    the worked designation (designation_mst.dept_id, the DPR / TKT-185
    route); a note says how much of the attendance still resolves to none
    (0 % on sls 1-19 Oct 2026 — the old worked_department_id route left 94 %
    unassigned)."""
    spells = _hands_lines(by_spell, "spell_id", "spell_name")
    departments = _hands_lines(by_department, "department")
    hands = round(sum(s["hands"] for s in spells), 2)
    hours = round(sum(s["hours"] for s in spells), 2)
    rows = sum(s["rows"] for s in spells)
    rate = None
    if rate_pick is not None:
        rate = {
            "value": float(rate_pick["value"]),
            "source": rate_pick.get("source"),
            "effective_date": _iso(rate_pick["effective_date"]) if rate_pick.get("effective_date") else None,
            "scope": "branch" if rate_pick.get("branch_id") is not None else "all",
        }
    notes = []
    unassigned = sum(d["rows"] for d in departments if d["department"] == "Not assigned")
    if rows and unassigned:
        notes.append(
            f"{unassigned / rows * 100:.0f} % of the attendance rows carry no resolvable "
            "department (worked designation → designation_mst.dept_id) — shown as 'Not assigned'."
        )
    return {
        "by_spell": spells,
        "by_designation": _hands_lines(by_designation, "designation"),
        "by_department": departments,
        "hours_profile": [
            {"working_hours": _n(r.get("working_hours")), "rows": _int(r.get("rows"))}
            for r in _norm(hours_profile)
        ],
        "rate": rate,
        "summary": {
            "rows": rows,
            "hours": hours,
            "hands": hands,
            "wages": round(hands * rate["value"], 2) if rate is not None else None,
        },
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Jute-issue data check (pure)
# ---------------------------------------------------------------------------


def compute_rate_medians(rows: List[dict]) -> Dict[int, dict]:
    """Per item_id: the median MR rate (₹/qtl) of the lines that carry one
    and how many such lines there are ({"median", "n"})."""
    rates: Dict[int, List[float]] = {}
    for r in rows:
        rate = _f(r.get("mr_rate_qtl"))
        if rate > 0 and r.get("item_id") is not None:
            rates.setdefault(int(r["item_id"]), []).append(rate)
    return {k: {"median": round(statistics.median(v), 2), "n": len(v)} for k, v in rates.items()}


def duplicate_context(rows: List[dict]) -> List[tuple]:
    """Per row (same order): (dup_group_size, dup_seconds). Lines are grouped
    on (issue_date, jute_mr_li_id, weight); dup_group_size is the group's
    size and dup_seconds the smallest keyed-in gap (update_date_time) to
    another line of the group with the same COALESCE(issue_value, 0) — None
    when there is no such partner or a timestamp is missing (shown for
    information; see DUPLICATE_WINDOW_SECONDS). The DUPLICATE rule reads
    dup_group_size >= DUPLICATE_GROUP_MIN only."""
    groups: Dict[tuple, List[int]] = {}
    for i, r in enumerate(rows):
        key = (_as_date(r.get("issue_date")), r.get("jute_mr_li_id"), round(_f(r.get("weight_kg")), 3))
        groups.setdefault(key, []).append(i)
    out: List[tuple] = [(1, None)] * len(rows)
    for members in groups.values():
        for i in members:
            t = _as_datetime(rows[i].get("keyed_at"))
            value = round(_f(rows[i].get("issue_value")), 2)
            best = None
            if t is not None:
                for k in members:
                    if k == i or round(_f(rows[k].get("issue_value")), 2) != value:
                        continue
                    tk = _as_datetime(rows[k].get("keyed_at"))
                    if tk is None:
                        continue
                    gap = abs(int((t - tk).total_seconds()))
                    best = gap if best is None or gap < best else best
            out[i] = (len(members), best)
    return out


def flag_issue_rows(rows: List[dict], medians: Dict[int, dict], today: Optional[date] = None) -> List[dict]:
    """Annotate every issue line with its flags (the §1.6 rules), implied and
    median rates and the suggested value — weight / 100 x the MR rate, or,
    when the MR line has no rate (the very lines the owner wants to fix),
    x the period median of that quality so the user still gets a figure to
    key; ``suggested_basis`` says which ("mr_rate" / "median" / None).
    Returns EVERY row (flags may be empty) in the input order; the caller
    keeps the flagged ones for the response and uses the rest for the totals."""
    today = today or today_ist()
    dup_ctx = duplicate_context(rows)
    out = []
    for i, r in enumerate(rows):
        dup_group_size, dup_seconds = dup_ctx[i]
        weight = _f(r.get("weight_kg"))
        value = _f(r.get("issue_value"))
        mr_rate = _f(r.get("mr_rate_qtl"))
        issue_item = r.get("issue_item_id")
        mr_item = r.get("mr_item_id")
        mr_status_id = r.get("mr_status_id")
        balance = r.get("mr_line_balance_kg")
        issue_date = _as_date(r.get("issue_date"))
        item_id = r.get("item_id")
        med = medians.get(int(item_id)) if item_id is not None else None
        median = med["median"] if (med is not None and med["n"] >= RATE_OUTLIER_MIN_LINES) else None
        implied = round(value / weight * 100.0, 2) if (weight > 0 and value > 0) else None

        flags: List[str] = []
        if weight <= 0:
            flags.append(FLAG_ZERO_WEIGHT)
        if weight > 0 and value == 0:
            flags.append(FLAG_NO_VALUE)
        if mr_rate <= 0:
            flags.append(FLAG_NO_MR_RATE)
        if implied is not None and mr_rate > 0 and abs(implied - mr_rate) > VALUE_RATE_TOLERANCE * mr_rate:
            flags.append(FLAG_VALUE_RATE_MISMATCH)
        if mr_rate > 0 and median is not None and abs(mr_rate - median) > RATE_OUTLIER_TOLERANCE * median:
            flags.append(FLAG_RATE_OUTLIER)
        if issue_item is not None and mr_item is not None and int(issue_item) != int(mr_item):
            flags.append(FLAG_ITEM_MISMATCH)
        if mr_status_id is not None and int(mr_status_id) in MR_OPEN_STATUSES:
            flags.append(FLAG_MR_OPEN)
        elif mr_status_id is None or int(mr_status_id) not in JUTE_MR_APPROVED_STATUSES:
            flags.append(FLAG_MR_STATUS)
        if dup_group_size >= DUPLICATE_GROUP_MIN:
            flags.append(FLAG_DUPLICATE)
        if balance is not None and _f(balance) < -OVER_ISSUE_TOLERANCE_KG:
            flags.append(FLAG_OVER_ISSUE)
        if issue_date is not None and issue_date > today:
            flags.append(FLAG_FUTURE_DATE)

        if mr_rate > 0:
            suggested, basis = round(weight / 100.0 * mr_rate, 2), "mr_rate"
        elif median is not None:
            suggested, basis = round(weight / 100.0 * median, 2), "median"
        else:
            suggested, basis = None, None

        out.append(
            {
                "jute_issue_id": r.get("jute_issue_id"),
                "issue_date": _iso(issue_date) if issue_date else None,
                "issue_status_id": r.get("issue_status_id"),
                "jute_mr_id": r.get("jute_mr_id"),
                "jute_mr_li_id": r.get("jute_mr_li_id"),
                "branch_mr_no": r.get("branch_mr_no"),
                "jute_gate_entry_no": r.get("jute_gate_entry_no"),
                "mr_status_id": mr_status_id,
                "mr_status": r.get("mr_status"),
                "item_id": item_id,
                "item_name": r.get("item_name"),
                "weight_kg": round(weight, 3),
                "issue_value": round(value, 2),
                "implied_rate_qtl": implied,
                "mr_rate_qtl": round(mr_rate, 2) if mr_rate > 0 else None,
                "median_rate_qtl": median,
                "suggested_value": suggested,
                "suggested_basis": basis,
                "mr_line_balance_kg": round(_f(balance), 3) if balance is not None else None,
                "dup_group_size": dup_group_size,
                "dup_seconds": dup_seconds,
                "flags": flags,
            }
        )
    return out


def build_issue_checks(
    rows: List[dict],
    cloth_rows: List[dict],
    d_from: date,
    d_to: date,
    std: float = JUTE_PER_KG_CLOTH_STD,
    today: Optional[date] = None,
    waste_ledger: Optional[List[dict]] = None,
) -> dict:
    """§1.6 response: summary, flagged rows (ordered date, id), day flags,
    the flag labels and notes. ``std`` is the keyed JUTE_PER_KG_CLOTH or 1.06.
    ``summary.identical_pairs`` counts the 2-line identical groups (date, MR
    line, weight) that are NOT flagged — repeated trolley weights are normal
    — so the dialog can say so. ``waste_ledger`` is the one-row result of
    get_jute_issue_waste_ledger_query (the same scope's lines with NO MR
    line, outside every rule here): summary.waste_ledger_lines / _kg /
    _value and a note say how much the check does not cover."""
    annotated = flag_issue_rows(rows, compute_rate_medians(rows), today)
    flagged = [r for r in annotated if r["flags"]]
    identical_pairs = sum(1 for r in annotated if r["dup_group_size"] == 2) // 2
    by_flag = {code: 0 for code in FLAG_LABELS}  # every code, always (stable shape)
    for r in flagged:
        for code in r["flags"]:
            by_flag[code] += 1

    cloth_by_day: Dict[date, float] = {}
    for r in cloth_rows or []:
        dd = _as_date(r.get("tran_date"))
        if dd is not None and d_from <= dd <= d_to:
            cloth_by_day[dd] = cloth_by_day.get(dd, 0.0) + _f(r.get("production_mt"))
    issued_by_day: Dict[date, float] = {}
    for r in annotated:
        dd = _as_date(r.get("issue_date"))
        if dd is not None:
            issued_by_day[dd] = issued_by_day.get(dd, 0.0) + r["weight_kg"]
    day_flags = [
        {
            "tran_date": _iso(dd),
            "cloth_t": round(t, 3),
            "issued_kg": round(issued_by_day.get(dd, 0.0), 3),
            "flag": FLAG_NO_ISSUE_DAY,
        }
        for dd, t in sorted(cloth_by_day.items())
        if t > 0 and issued_by_day.get(dd, 0.0) == 0
    ]
    by_flag[FLAG_NO_ISSUE_DAY] = len(day_flags)

    issued_kg = round(sum(r["weight_kg"] for r in annotated), 3)
    cloth_t = round(sum(cloth_by_day.values()), 3)
    wl = waste_ledger[0] if waste_ledger else {}
    wl_lines = _int(wl.get("lines"))
    wl_kg = round(_f(wl.get("kg")), 3)
    wl_value = round(_f(wl.get("value")), 2)
    notes = [_note_waste_ledger(wl_lines, wl_kg, wl_value)] if wl_lines else []
    ratio = _ratio(issued_kg, cloth_t * 1000.0, 3)
    ratio_flag = None
    if ratio is not None:
        ratio_flag = "LOW" if ratio < RATIO_LOW else ("HIGH" if ratio > RATIO_HIGH else None)
    return {
        "summary": {
            "lines": len(annotated),
            "flagged": len(flagged),
            "by_flag": by_flag,
            "identical_pairs": identical_pairs,
            "issued_kg": issued_kg,
            "cloth_t": cloth_t,
            "jute_kg_per_kg_cloth": ratio,
            "std": std,
            "ratio_flag": ratio_flag,
            "waste_ledger_lines": wl_lines,
            "waste_ledger_kg": wl_kg,
            "waste_ledger_value": wl_value,
        },
        "rows": flagged,
        "day_flags": day_flags,
        "flag_labels": dict(FLAG_LABELS),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _meta(co_id, branch_id, d_from, d_to, **extra) -> dict:
    meta = {"co_id": co_id, "branch_id": branch_id, "date_from": _iso(d_from), "date_to": _iso(d_to)}
    meta.update(extra)
    return meta


@router.get("/daily_pl/breakdown")
def daily_pl_breakdown(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = parse_range_params(request)
    head = (request.query_params.get("head") or "").strip()
    if head not in BREAKDOWN_HEADS:
        raise HTTPException(status_code=400, detail=INVALID_HEAD_MSG)
    try:
        if branch_id is not None:
            _verify_pair(db, co_id, branch_id)
        binds = {"co_id": co_id, "branch_id": branch_id, "date_from": d_from, "date_to": d_to}
        if head == HEAD_RAW_MATERIAL:
            data = build_raw_material_breakdown(
                _rows(db, get_jute_issue_by_quality_query(), binds),
                _rows(db, get_jute_issue_by_mr_query(), binds),
                _rows(db, get_jute_issue_by_day_query(), binds),
                _rows(db, get_jute_waste_issue_by_item_query(), binds),
            )
        elif head == HEAD_STORES:
            data = build_stores_breakdown(
                _rows(db, get_stores_by_category_query(), binds),
                _rows(db, get_stores_by_group_query(), binds),
                _rows(db, get_stores_by_dept_query(), binds),
                _rows(db, get_stores_by_expense_type_query(), binds),
                _rows(db, get_stores_top_items_query(), binds),
            )
        elif head == HEAD_POWER:
            by_meter = _fetch_optional(db, get_power_by_meter_query(), binds)
            by_day_type = _fetch_optional(db, get_power_by_day_type_query(), binds) if by_meter is not None else None
            data = build_power_breakdown(by_meter, by_day_type)
        else:
            params_rows = _rows(
                db,
                get_cost_param_rows_for_resolution_query(),
                {"co_id": co_id, "branch_id": branch_id, "on_date": d_to},
            )
            data = build_wages_breakdown(
                _rows(db, get_hands_by_spell_query(), binds),
                _rows(db, get_hands_by_designation_query(), binds),
                _rows(db, get_hands_by_department_query(), binds),
                _rows(db, get_hours_profile_query(), binds),
                param_value(resolve_params(params_rows, d_to), "WAGE_PER_HAND_DAY"),
            )
        data["meta"] = _meta(co_id, branch_id, d_from, d_to, head=head)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily_pl/jute_issue_checks")
def daily_pl_jute_issue_checks(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = parse_range_params(request)
    try:
        if branch_id is not None:
            _verify_pair(db, co_id, branch_id)
        binds = {"co_id": co_id, "branch_id": branch_id, "date_from": d_from, "date_to": d_to}
        params_rows = _rows(
            db,
            get_cost_param_rows_for_resolution_query(),
            {"co_id": co_id, "branch_id": branch_id, "on_date": d_to},
        )
        std_pick = param_value(resolve_params(params_rows, d_to), "JUTE_PER_KG_CLOTH")
        std = float(std_pick["value"]) if std_pick is not None else JUTE_PER_KG_CLOTH_STD
        rows = _rows(db, get_jute_issue_check_rows_query(), binds)
        cloth = _rows(db, get_cloth_by_quality_daily_query(), binds)
        waste_ledger = _rows(db, get_jute_issue_waste_ledger_query(), binds)
        data = build_issue_checks(rows, cloth, d_from, d_to, std, today_ist(), waste_ledger)
        data["meta"] = _meta(
            co_id,
            branch_id,
            d_from,
            d_to,
            thresholds={
                "value_rate_tolerance": VALUE_RATE_TOLERANCE,
                "rate_outlier_tolerance": RATE_OUTLIER_TOLERANCE,
                "rate_outlier_min_lines": RATE_OUTLIER_MIN_LINES,
                "over_issue_tolerance_kg": OVER_ISSUE_TOLERANCE_KG,
                "duplicate_window_seconds": DUPLICATE_WINDOW_SECONDS,
                "duplicate_group_min": DUPLICATE_GROUP_MIN,
                "ratio_low": RATIO_LOW,
                "ratio_high": RATIO_HIGH,
            },
        )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
