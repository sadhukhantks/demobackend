"""Utilities endpoints — Jute Mechanical & Maintenance (``/api/mechUtility``).

ITM-004: daily power readings. The mill's electrical shift keys the main grid
meter and the DG sets once a day (optionally per spell): opening / closing kWh,
DG running hours and diesel litres, max demand kVA. The report divides the kWh
by the DPR's day tonnes (yarn from spinning, cloth from weaving) for kWh/t, with
a 30-day median as the "std" line — no achievement %, per the 2026-10-07 ruling.

Persona: Portal — ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, plain ``def`` handlers, soft delete via ``active = 0``.
Helpers and the ``try / except HTTPException: raise / except Exception: rollback +
500`` shape mirror ``breakdown_entry.py``. No approval workflow (readings must be
fast); writes save directly.

Tables (``dbqueries/migrations/20261018_jute_power_reading.sql``, ORM in
``models.py``): ``jute_power_meter_mst`` (meters per branch, managed from the
entry page's Meters dialog — deactivate only, never delete, readings keep the
code) and ``jute_power_reading`` (UNIQUE on co / branch / date / spell / meter).

Rules, each in its own pure module-level function (unit-tested without a DB):

    compute_kwh         both bounds keyed -> closing - opening, 400 when closing
                        < opening, a typed kWh is ignored; a bound missing ->
                        the typed kWh (meter reset), 400 when missing or < 0
    validate_tran_date  400 when later than today in IST (server clock is UTC)
    is_locked /         readings older than POWER_LOCK_DAYS are changed only
    check_lock          with Edit (access_type_id >= 4) on the entry page's
                        menu for the branch -- a capability, never a role id
                        (``_can_override``); 403 otherwise
    spike_warning       kWh > POWER_SPIKE_FACTOR x the meter's 30-day median ->
                        WARNING text, never a rejection
    dg_warning          DG litres without hours / DG figures on a non-DG meter
    report_window_from  one DB pass covers the range, the MTD block and the
                        30-day std window
    build_power_report  the whole /power_report payload from fetched rows

Every scoped endpoint first checks that ``branch_id`` belongs to ``co_id``
(``_verify_branch_in_company``): within one tenant a reading must never land
under another company's branch.

A date is keyed EITHER as a whole day OR per spell. Every reader SUMs across
``spell_id``, so the save refuses (400) a whole-day save when spell rows exist
for the date, and a spell save when a whole-day row exists
(``get_power_spell_conflict_query``).

Upsert is select-by-key then UPDATE / INSERT (not ON DUPLICATE KEY) so the saved
ids come back and the execute order is mock-testable; a soft-deleted row under
the same key is re-activated in place. The INSERT runs under a SAVEPOINT: when
two electricians save the same key at once the loser's INSERT hits
``uq_jpr_scope``, the savepoint is rolled back and the row is UPDATEd instead.
One commit per save.

``spell_id`` is stored as INT NOT NULL with 0 = whole day (``WHOLE_DAY_SPELL_ID``)
because MySQL treats NULLs as distinct inside a UNIQUE key; the API shows and
accepts ``null``.
"""

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.portal.query import get_user_menu_access_level_query
from src.common.utils import now_ist
from src.config.db import get_tenant_db
from src.juteMechanical.breakdown_query import get_breakdown_spells_query
from src.juteMechanical.constants import (
    METER_TYPE_DG,
    METER_TYPE_GRID,
    METER_TYPE_LABELS,
    METER_TYPE_SOLAR,
    METER_TYPES,
    POWER_ENTRY_MENU_PATH,
    POWER_LOCK_DAYS,
    POWER_LOCK_OVERRIDE_ACCESS_LEVEL,
    POWER_MEDIAN_WINDOW_DAYS,
    POWER_SPIKE_FACTOR,
    WHOLE_DAY_SPELL_ID,
)
from src.juteMechanical.utility_query import (
    check_power_meter_duplicate_query,
    get_power_kwh_history_query,
    get_power_meter_by_id_query,
    get_power_meters_query,
    get_power_prev_closing_query,
    get_power_reading_active_by_id_query,
    get_power_reading_by_key_query,
    get_power_readings_by_date_query,
    get_power_report_cloth_daily_query,
    get_power_report_readings_query,
    get_power_spell_conflict_query,
    insert_power_meter_query,
    insert_power_reading_query,
    soft_delete_power_reading_query,
    update_power_meter_query,
    update_power_reading_query,
)
from src.juteProduction.constants import SPINNING_MACHINE_TYPE_NAME
from src.juteProduction.doff_analysis_query import get_branch_scope_check_query
from src.juteProduction.dprQueries import get_dpr_spinning_daily_production_query


router = APIRouter()


# =============================================================================
# Messages / limits
# =============================================================================

MAX_RANGE_DAYS = 93

CLOSING_BELOW_OPENING_MSG = "closing_kwh cannot be lower than opening_kwh"
KWH_REQUIRED_MSG = "kwh is required when opening_kwh or closing_kwh is missing"
KWH_NEGATIVE_MSG = "kwh cannot be negative"
FUTURE_DATE_MSG = "tran_date cannot be in the future"
LOCKED_MSG = (
    f"Readings older than {POWER_LOCK_DAYS} days can only be changed with Edit permission "
    "on the Power Readings menu"
)
SPELL_CONFLICT_WHOLE_DAY_MSG = (
    "This date is already keyed per spell — delete those readings before keying it as a whole day"
)
SPELL_CONFLICT_PER_SPELL_MSG = (
    "This date is already keyed as a whole day — delete that reading before keying it per spell"
)
BRANCH_NOT_IN_COMPANY_MSG = "branch_id does not belong to co_id"
UNKNOWN_METER_MSG = "Unknown meter_code for this branch"
NO_ROWS_MSG = "No readings to save"
METER_NOT_FOUND_MSG = "Power meter not found"
METER_DUP_MSG = "A meter with this code already exists for this branch"
METER_DUP_INACTIVE_HINT = " but is inactive — reactivate it instead"
BAD_METER_TYPE_MSG = "meter_type must be one of: " + ", ".join(METER_TYPES)
READING_NOT_FOUND_MSG = "Power reading not found"
BRANCH_REQUIRED_MSG = "branch_id is required"
TRAN_DATE_REQUIRED_MSG = "tran_date is required"
TRAN_DATE_FORMAT_MSG = "Invalid tran_date format"
DATE_ORDER_MSG = "date_to is before date_from"
RANGE_TOO_LARGE_MSG = f"Date range too large — at most {MAX_RANGE_DAYS} days"

_METER_TYPE_ORDER = {METER_TYPE_GRID: 0, METER_TYPE_DG: 1, METER_TYPE_SOLAR: 2}


# =============================================================================
# Pydantic models
# =============================================================================


class PowerMeterPayload(BaseModel):
    """POST /meter_create and PUT /meter_edit body.

    ``branch_id`` is required on create (400 without it) and IGNORED on edit: a
    meter never moves branch, its readings reference it by code within the
    stored (co, branch).
    """

    co_id: int
    branch_id: Optional[int] = None
    meter_code: str = Field(min_length=1, max_length=20)
    meter_name: str = Field(min_length=1, max_length=100)
    meter_type: str
    active: int = 1


class PowerReadingRow(BaseModel):
    """One meter's figures for the date being saved."""

    meter_code: str
    opening_kwh: Optional[float] = Field(default=None, ge=0)
    closing_kwh: Optional[float] = Field(default=None, ge=0)
    kwh: Optional[float] = None  # used only when a bound is missing (meter reset); < 0 is a 400
    dg_hours: Optional[float] = Field(default=None, ge=0)
    dg_litres: Optional[float] = Field(default=None, ge=0)
    max_demand_kva: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = Field(default=None, max_length=255)


class PowerReadingsSave(BaseModel):
    """POST /readings_save body — every meter of one date (+ optional spell)."""

    co_id: int
    branch_id: int
    tran_date: date
    spell_id: Optional[int] = None  # None = whole day
    rows: List[PowerReadingRow]


# =============================================================================
# Request helpers (mirror breakdown_entry.py)
# =============================================================================


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
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _require_branch_id(request: Request) -> int:
    branch_id = _optional_branch_id(request)
    if branch_id is None:
        raise HTTPException(status_code=400, detail=BRANCH_REQUIRED_MSG)
    return branch_id


def _optional_int(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _require_date(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _of(v) -> Optional[float]:
    """Cast to float but preserve NULL (Decimal / Double columns)."""
    return None if v is None else float(v)


def _as_date(v: Any) -> Optional[date]:
    """date / datetime / 'YYYY-MM-DD' -> date (DB drivers and mocks differ)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _user_id(token_data: dict) -> Optional[int]:
    raw = (token_data or {}).get("user_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _enum_options(values, labels: dict) -> List[Dict[str, str]]:
    return [{"code": c, "label": labels.get(c, c)} for c in values]


def _spell_key(spell_id: Optional[int]) -> int:
    """API spell_id (None = whole day) -> stored key (0 = whole day)."""
    return WHOLE_DAY_SPELL_ID if spell_id is None else int(spell_id)


def _spell_out(spell_id) -> Optional[int]:
    """Stored key -> API spell_id (0 -> null)."""
    if spell_id is None:
        return None
    s = int(spell_id)
    return None if s == WHOLE_DAY_SPELL_ID else s


def _verify_branch_in_company(db: Session, co_id: int, branch_id: int) -> None:
    """400 unless branch_id is one of co_id's branches (same tenant, other
    company): a reading or meter must never land under another company's
    branch. One SELECT 1 against branch_mst, the first statement of every
    scoped endpoint."""
    if db.execute(
        get_branch_scope_check_query(), {"co_id": co_id, "branch_id": branch_id}
    ).first() is None:
        raise HTTPException(status_code=400, detail=BRANCH_NOT_IN_COMPANY_MSG)


def _can_override(db: Session, user_id: Optional[int], co_id: int, branch_id: int) -> bool:
    """True when a role the user holds on (co, branch) grants Edit
    (access_type_id >= POWER_LOCK_OVERRIDE_ACCESS_LEVEL) on the entry page's
    menu -- the sidebar's hasMenuAccess ordinal, resolved server-side through
    the house query (control_co_module caps honoured). No grant -> False."""
    if user_id is None:
        return False
    level = db.execute(
        get_user_menu_access_level_query(),
        {
            "user_id": int(user_id),
            "co_id": int(co_id),
            "branch_id": int(branch_id),
            "menu_path": POWER_ENTRY_MENU_PATH,
        },
    ).scalar()
    return level is not None and int(level) >= POWER_LOCK_OVERRIDE_ACCESS_LEVEL


def _enforce_lock(
    db: Session, tran_date: date, today: date, user_id: Optional[int], co_id: int, branch_id: int
) -> None:
    """check_lock with the capability looked up only when the date IS locked
    (no DB round trip inside the window)."""
    if is_locked(tran_date, today):
        check_lock(tran_date, today, _can_override(db, user_id, co_id, branch_id))


def _load_spells(db: Session, branch_id: Optional[int]) -> List[dict]:
    """Active spells, de-duplicated by spell_code (as entry_create_setup does)."""
    spells = []
    seen = set()
    for r in db.execute(get_breakdown_spells_query(), {"branch_id": branch_id}).fetchall():
        m = dict(r._mapping)
        code = m["spell_code"]
        if code in seen:
            continue
        seen.add(code)
        spells.append(
            {
                "spell_id": int(m["spell_id"]),
                "spell_code": code,
                "spell_name": m.get("spell_name"),
                "working_hours": _f(m.get("working_hours")),
            }
        )
    return spells


def _meter_out(row: dict) -> dict:
    return {
        "power_meter_id": int(row["power_meter_id"]),
        "co_id": int(row["co_id"]),
        "branch_id": int(row["branch_id"]),
        "meter_code": row["meter_code"],
        "meter_name": row["meter_name"],
        "meter_type": row["meter_type"],
        "meter_type_label": METER_TYPE_LABELS.get(row["meter_type"], row["meter_type"]),
        "active": int(row["active"]) if row.get("active") is not None else 1,
    }


# =============================================================================
# Rules — pure, unit-testable (no DB)
# =============================================================================


def compute_kwh(opening: Optional[float], closing: Optional[float], typed: Optional[float]) -> float:
    """The kWh to store.

    Both bounds keyed -> closing - opening (400 when closing < opening); a typed
    kWh is ignored then. A bound missing (meter reset / first reading) -> the
    typed kWh: 400 KWH_REQUIRED_MSG when absent, 400 KWH_NEGATIVE_MSG when < 0
    (two distinct messages -- a negative is a mis-key, not a missing value).
    """
    if opening is not None and closing is not None:
        if float(closing) < float(opening):
            raise HTTPException(status_code=400, detail=CLOSING_BELOW_OPENING_MSG)
        return round(float(closing) - float(opening), 2)
    if typed is None:
        raise HTTPException(status_code=400, detail=KWH_REQUIRED_MSG)
    if float(typed) < 0:
        raise HTTPException(status_code=400, detail=KWH_NEGATIVE_MSG)
    return round(float(typed), 2)


def validate_tran_date(tran_date: date, today: date) -> None:
    """400 when the reading date is after today (callers pass now_ist().date())."""
    if tran_date > today:
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)


def is_locked(tran_date: date, today: date) -> bool:
    """A reading older than POWER_LOCK_DAYS (exclusive) is locked."""
    return (today - tran_date).days > POWER_LOCK_DAYS


def check_lock(tran_date: date, today: date, can_override: bool) -> None:
    if is_locked(tran_date, today) and not can_override:
        raise HTTPException(status_code=403, detail=LOCKED_MSG)


def median_or_none(values) -> Optional[float]:
    vals = [float(v) for v in (values or []) if v is not None]
    return round(median(vals), 2) if vals else None


def spike_warning(meter_code: str, kwh: float, median30: Optional[float]) -> Optional[str]:
    """Warning text when kwh > POWER_SPIKE_FACTOR x the 30-day median; None otherwise
    (also None when there is no history to compare with)."""
    if median30 is None or float(median30) <= 0:
        return None
    if float(kwh) > POWER_SPIKE_FACTOR * float(median30):
        return (
            f"{meter_code}: {float(kwh):,.0f} kWh is more than {POWER_SPIKE_FACTOR:g}x the "
            f"30-day median {float(median30):,.0f} kWh — check the reading"
        )
    return None


def dg_warning(
    meter_code: str,
    meter_type: Optional[str],
    dg_hours: Optional[float],
    dg_litres: Optional[float],
) -> Optional[str]:
    """DG litres without hours, or DG figures on a non-DG meter. Warnings only."""
    hours = _f(dg_hours)
    litres = _f(dg_litres)
    if litres > 0 and hours <= 0:
        return f"{meter_code}: DG litres keyed without DG hours"
    if meter_type != METER_TYPE_DG and (hours > 0 or litres > 0):
        return f"{meter_code}: DG hours keyed on a non-DG meter"
    return None


def report_window_from(d_from: date, d_to: date) -> date:
    """Earliest date one DB pass must cover: the range itself, the month of
    date_to (MTD block) and the 30-day std window ending on date_to."""
    return min(
        d_from,
        d_to.replace(day=1),
        d_to - timedelta(days=POWER_MEDIAN_WINDOW_DAYS - 1),
    )


def _ratio(num: float, den: float, nd: int = 2) -> Optional[float]:
    return round(num / den, nd) if den and den > 0 else None


def _block_totals(entries: List[dict]) -> dict:
    """Totals / MTD block over day entries: kWh summed over KEYED days, tonnes of
    those same keyed days, so kWh/t reconciles on screen."""
    keyed = [e for e in entries if e["keyed"]]
    kwh_total = sum(e["kwh_total"] for e in keyed)
    kwh_grid = sum(e["kwh_grid"] for e in keyed)
    kwh_dg = sum(e["kwh_dg"] for e in keyed)
    kwh_solar = sum(e["kwh_solar"] for e in keyed)
    dg_hours = sum(e["dg_hours"] for e in keyed)
    dg_litres = sum(e["dg_litres"] for e in keyed)
    yarn_t = sum(e["yarn_t"] for e in keyed)
    cloth_t = sum(e["cloth_t"] for e in keyed)
    kvas = [e["max_demand_kva"] for e in keyed if e["max_demand_kva"] is not None]
    return {
        "days": len(entries),
        "days_keyed": len(keyed),
        "kwh_total": round(kwh_total, 2),
        "kwh_grid": round(kwh_grid, 2),
        "kwh_dg": round(kwh_dg, 2),
        "kwh_solar": round(kwh_solar, 2),
        "dg_hours": round(dg_hours, 2),
        "dg_litres": round(dg_litres, 2),
        "max_demand_kva": max(kvas) if kvas else None,
        "yarn_t": round(yarn_t, 3),
        "cloth_t": round(cloth_t, 3),
        "kwh_per_t_yarn": _ratio(kwh_total, yarn_t) if keyed else None,
        "kwh_per_t_cloth": _ratio(kwh_total, cloth_t) if keyed else None,
        "dg_share_pct": round(kwh_dg * 100.0 / kwh_total, 2) if kwh_total > 0 else None,
        "avg_kwh_per_keyed_day": round(kwh_total / len(keyed), 2) if keyed else None,
    }


def build_power_report(
    reading_rows: List[dict],
    yarn_rows: List[dict],
    cloth_rows: List[dict],
    meters: List[dict],
    d_from: date,
    d_to: date,
) -> dict:
    """The /power_report payload (without meta) from already-fetched rows.

    reading_rows: (tran_date, meter_code, meter_name, meter_type, kwh, dg_hours,
                   dg_litres, max_demand_kva) per date x meter over the window
    yarn_rows:    (tran_date, production_kg) per day — DPR spinning feed
    cloth_rows:   (tran_date, production_mt) per day — DPR weaving feed
    meters:       the branch's meter master rows (active and inactive)

    Never raises on empty inputs: every day comes back unkeyed.
    """
    window_from = report_window_from(d_from, d_to)

    meter_info: Dict[str, dict] = {}
    for m in meters:
        meter_info[m["meter_code"]] = {
            "meter_code": m["meter_code"],
            "meter_name": m.get("meter_name") or m["meter_code"],
            "meter_type": m.get("meter_type") or METER_TYPE_GRID,
            "active": int(m.get("active") or 0),
        }

    by_day: Dict[date, Dict[str, dict]] = {}
    seen_codes = set()
    for r in reading_rows:
        d = _as_date(r.get("tran_date"))
        code = r["meter_code"]
        if d is None or code is None:
            continue
        seen_codes.add(code)
        if code not in meter_info:
            meter_info[code] = {
                "meter_code": code,
                "meter_name": r.get("meter_name") or code,
                "meter_type": r.get("meter_type") or METER_TYPE_GRID,
                "active": 0,
            }
        slot = by_day.setdefault(d, {}).setdefault(
            code, {"kwh": 0.0, "dg_hours": 0.0, "dg_litres": 0.0, "max_demand_kva": None}
        )
        slot["kwh"] += _f(r.get("kwh"))
        slot["dg_hours"] += _f(r.get("dg_hours"))
        slot["dg_litres"] += _f(r.get("dg_litres"))
        kva = _of(r.get("max_demand_kva"))
        if kva is not None and (slot["max_demand_kva"] is None or kva > slot["max_demand_kva"]):
            slot["max_demand_kva"] = kva

    report_meters = [
        m for m in meter_info.values() if m["active"] == 1 or m["meter_code"] in seen_codes
    ]
    report_meters.sort(key=lambda m: (_METER_TYPE_ORDER.get(m["meter_type"], 9), m["meter_code"]))
    meter_codes = [m["meter_code"] for m in report_meters]
    type_of = {m["meter_code"]: m["meter_type"] for m in report_meters}

    yarn_t_by_day: Dict[date, float] = {}
    for r in yarn_rows:
        d = _as_date(r.get("tran_date"))
        if d is not None:
            yarn_t_by_day[d] = round(yarn_t_by_day.get(d, 0.0) + _f(r.get("production_kg")) / 1000.0, 3)
    cloth_t_by_day: Dict[date, float] = {}
    for r in cloth_rows:
        d = _as_date(r.get("tran_date"))
        if d is not None:
            cloth_t_by_day[d] = round(cloth_t_by_day.get(d, 0.0) + _f(r.get("production_mt")), 3)

    def day_entry(d: date) -> dict:
        rows = by_day.get(d, {})
        keyed = bool(rows)
        kwh_by_meter = {
            code: (round(rows[code]["kwh"], 2) if code in rows else None) for code in meter_codes
        }
        total = grid = dg = solar = hours = litres = 0.0
        kvas = []
        for code, slot in rows.items():
            total += slot["kwh"]
            mt = type_of.get(code, METER_TYPE_GRID)
            if mt == METER_TYPE_DG:
                dg += slot["kwh"]
            elif mt == METER_TYPE_SOLAR:
                solar += slot["kwh"]
            else:
                grid += slot["kwh"]
            hours += slot["dg_hours"]
            litres += slot["dg_litres"]
            if slot["max_demand_kva"] is not None:
                kvas.append(slot["max_demand_kva"])
        yarn_t = yarn_t_by_day.get(d, 0.0)
        cloth_t = cloth_t_by_day.get(d, 0.0)
        return {
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "keyed": keyed,
            "kwh_total": round(total, 2),
            "kwh_grid": round(grid, 2),
            "kwh_dg": round(dg, 2),
            "kwh_solar": round(solar, 2),
            "kwh_by_meter": kwh_by_meter,
            "dg_hours": round(hours, 2),
            "dg_litres": round(litres, 2),
            "max_demand_kva": max(kvas) if kvas else None,
            "yarn_t": yarn_t,
            "cloth_t": cloth_t,
            "kwh_per_t_yarn": _ratio(total, yarn_t) if keyed else None,
            "kwh_per_t_cloth": _ratio(total, cloth_t) if keyed else None,
            "dg_share_pct": round(dg * 100.0 / total, 2) if keyed and total > 0 else None,
        }

    entries: Dict[date, dict] = {}
    d = window_from
    while d <= d_to:
        entries[d] = day_entry(d)
        d += timedelta(days=1)

    days = [entries[x] for x in sorted(entries) if d_from <= x <= d_to]
    mtd_from = d_to.replace(day=1)
    mtd_entries = [entries[x] for x in sorted(entries) if mtd_from <= x <= d_to]
    std_from = d_to - timedelta(days=POWER_MEDIAN_WINDOW_DAYS - 1)
    std_days = [entries[x] for x in sorted(entries) if std_from <= x <= d_to and entries[x]["keyed"]]

    std = {
        "basis": f"{POWER_MEDIAN_WINDOW_DAYS}-day median ending {d_to.isoformat()}",
        "days_used": len(std_days),
        "kwh_total": median_or_none([e["kwh_total"] for e in std_days]),
        "kwh_per_t_cloth": median_or_none([e["kwh_per_t_cloth"] for e in std_days]),
        "kwh_per_t_yarn": median_or_none([e["kwh_per_t_yarn"] for e in std_days]),
        "dg_share_pct": median_or_none([e["dg_share_pct"] for e in std_days]),
    }

    return {
        "meters": [
            {"meter_code": m["meter_code"], "meter_name": m["meter_name"], "meter_type": m["meter_type"]}
            for m in report_meters
        ],
        "days": days,
        "totals": _block_totals(days),
        "mtd": _block_totals(mtd_entries),
        "std": std,
    }


def build_reading_rows(
    meters: List[dict], readings: List[dict], prev_closing: Dict[str, dict], medians: Dict[str, Optional[float]]
) -> List[dict]:
    """GET /readings rows: one per ACTIVE meter (master order) merged with its
    saved reading, plus any saved reading whose meter is now inactive / unknown
    (meter_active 0) so it can still be deleted."""

    def one(meter: dict, reading: Optional[dict]) -> dict:
        code = meter["meter_code"]
        prev = prev_closing.get(code)
        return {
            "meter_code": code,
            "meter_name": meter.get("meter_name") or code,
            "meter_type": meter.get("meter_type") or METER_TYPE_GRID,
            "meter_active": int(meter.get("active") or 0),
            "power_reading_id": int(reading["power_reading_id"]) if reading else None,
            "opening_kwh": _of(reading.get("opening_kwh")) if reading else None,
            "closing_kwh": _of(reading.get("closing_kwh")) if reading else None,
            "kwh": _of(reading.get("kwh")) if reading else None,
            "dg_hours": _of(reading.get("dg_hours")) if reading else None,
            "dg_litres": _of(reading.get("dg_litres")) if reading else None,
            "max_demand_kva": _of(reading.get("max_demand_kva")) if reading else None,
            "remarks": reading.get("remarks") if reading else None,
            "prefill_opening_kwh": _of(prev.get("closing_kwh")) if prev else None,
            "prefill_from_date": str(prev.get("tran_date")) if prev and prev.get("tran_date") is not None else None,
            "median30_kwh": medians.get(code),
        }

    by_code = {r["meter_code"]: r for r in readings}
    out = []
    active_codes = set()
    for m in meters:
        if int(m.get("active") or 0) != 1:
            continue
        active_codes.add(m["meter_code"])
        out.append(one(m, by_code.get(m["meter_code"])))
    for r in readings:
        if r["meter_code"] in active_codes:
            continue
        stub = {
            "meter_code": r["meter_code"],
            "meter_name": r.get("meter_name") or r["meter_code"],
            "meter_type": r.get("meter_type") or METER_TYPE_GRID,
            "active": 0,
        }
        out.append(one(stub, r))
    return out


def _row_is_blank(row: PowerReadingRow) -> bool:
    values = (
        row.opening_kwh,
        row.closing_kwh,
        row.kwh,
        row.dg_hours,
        row.dg_litres,
        row.max_demand_kva,
    )
    return all(v is None for v in values) and not (row.remarks or "").strip()


# =============================================================================
# Endpoints — meters
# =============================================================================


@router.get("/meters")
def meters(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Meters of one branch; the meter_type options for the dialog ride under
    ``master`` (house ``{"data", "master"}`` envelope)."""
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    include_inactive = 1 if (request.query_params.get("include_inactive") or "") in ("1", "true", "True") else 0
    try:
        _verify_branch_in_company(db, co_id, branch_id)
        rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_meters_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": include_inactive},
            ).fetchall()
        ]
        return {
            "data": [_meter_out(r) for r in rows],
            "master": {"meter_types": _enum_options(METER_TYPES, METER_TYPE_LABELS)},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _normalise_meter_payload(body: PowerMeterPayload, require_branch: bool) -> dict:
    """Pure validation of the meter body. branch_id is demanded on create only
    (edit keeps the stored branch)."""
    code = (body.meter_code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="meter_code is required")
    if body.meter_type not in METER_TYPES:
        raise HTTPException(status_code=400, detail=BAD_METER_TYPE_MSG)
    name = (body.meter_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="meter_name is required")
    if require_branch and body.branch_id is None:
        raise HTTPException(status_code=400, detail=BRANCH_REQUIRED_MSG)
    return {
        "co_id": int(body.co_id),
        "branch_id": None if body.branch_id is None else int(body.branch_id),
        "meter_code": code,
        "meter_name": name,
        "meter_type": body.meter_type,
        "active": 1 if int(body.active) else 0,
    }


def _check_meter_duplicate(db: Session, co_id: int, branch_id: int, code: str, exclude_id: Optional[int]) -> None:
    dup = db.execute(
        check_power_meter_duplicate_query(),
        {"co_id": co_id, "branch_id": branch_id, "meter_code": code, "power_meter_id": exclude_id},
    ).fetchone()
    if dup is not None:
        hint = METER_DUP_INACTIVE_HINT if int(dup.active or 0) == 0 else ""
        raise HTTPException(status_code=400, detail=METER_DUP_MSG + hint)


@router.post("/meter_create")
def meter_create(
    body: PowerMeterPayload,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    values = _normalise_meter_payload(body, require_branch=True)
    try:
        _verify_branch_in_company(db, values["co_id"], values["branch_id"])
        _check_meter_duplicate(db, values["co_id"], values["branch_id"], values["meter_code"], None)
        result = db.execute(insert_power_meter_query(), {**values, "updated_by": _user_id(token_data)})
        db.commit()
        return {"data": {"power_meter_id": int(result.lastrowid)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/meter_edit/{power_meter_id}")
def meter_edit(
    power_meter_id: int,
    body: PowerMeterPayload,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Rename / retype / (de)activate a meter. No delete: deactivate instead —
    readings keep their meter_code. The body's branch_id is ignored: the
    stored branch is the meter's branch."""
    values = _normalise_meter_payload(body, require_branch=False)
    try:
        existing = db.execute(
            get_power_meter_by_id_query(),
            {"power_meter_id": int(power_meter_id), "co_id": values["co_id"]},
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail=METER_NOT_FOUND_MSG)
        branch_id = int(existing.branch_id)
        _check_meter_duplicate(db, values["co_id"], branch_id, values["meter_code"], int(power_meter_id))
        db.execute(
            update_power_meter_query(),
            {
                "power_meter_id": int(power_meter_id),
                "co_id": values["co_id"],
                "meter_code": values["meter_code"],
                "meter_name": values["meter_name"],
                "meter_type": values["meter_type"],
                "active": values["active"],
                "updated_by": _user_id(token_data),
            },
        )
        db.commit()
        return {"data": {"power_meter_id": int(power_meter_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — readings
# =============================================================================


def _history_binds(co_id: int, branch_id: int, tran_date: date, meter_code: Optional[str]) -> dict:
    """The 30 days strictly before tran_date (the median basis of the spike check)."""
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "meter_code": meter_code,
        "date_from": tran_date - timedelta(days=POWER_MEDIAN_WINDOW_DAYS),
        "date_to": tran_date - timedelta(days=1),
    }


@router.get("/readings")
def readings(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The entry grid for one date (+ optional spell): one row per active meter
    merged with what is saved, yesterday's closing offered as today's opening,
    each meter's 30-day median for the inline spike hint, the lock state and the
    spell list."""
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    raw = request.query_params.get("tran_date")
    if not raw:
        raise HTTPException(status_code=400, detail=TRAN_DATE_REQUIRED_MSG)
    try:
        tran_date = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=TRAN_DATE_FORMAT_MSG)
    spell_id = _optional_int(request, "spell_id")
    key = _spell_key(spell_id)
    try:
        today = now_ist().date()
        _verify_branch_in_company(db, co_id, branch_id)
        can_override = _can_override(db, _user_id(token_data), co_id, branch_id)
        spells = _load_spells(db, branch_id)
        meters_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_meters_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": 1},
            ).fetchall()
        ]
        reading_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_readings_by_date_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": tran_date, "spell_id": key},
            ).fetchall()
        ]
        prev_closing = {}
        for r in db.execute(
            get_power_prev_closing_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "tran_date": tran_date,
                "spell_id": key,
                "whole_day": WHOLE_DAY_SPELL_ID,
            },
        ).fetchall():
            m = dict(r._mapping)
            prev_closing[m["meter_code"]] = m
        history: Dict[str, list] = {}
        for r in db.execute(
            get_power_kwh_history_query(), _history_binds(co_id, branch_id, tran_date, None)
        ).fetchall():
            m = r._mapping
            history.setdefault(m["meter_code"], []).append(m.get("kwh"))
        medians = {code: median_or_none(vals) for code, vals in history.items()}

        return {
            "data": {
                "tran_date": tran_date.isoformat(),
                "spell_id": _spell_out(key),
                "today": today.isoformat(),
                "lock": {
                    "locked": is_locked(tran_date, today),
                    "can_override": can_override,
                    "lock_days": POWER_LOCK_DAYS,
                },
                "spells": spells,
                "rows": build_reading_rows(meters_rows, reading_rows, prev_closing, medians),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/readings_save")
def readings_save(
    body: PowerReadingsSave,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Bulk upsert of one date's readings. Rows with nothing keyed are skipped;
    a row already saved under the same (date, spell, meter) key is updated in
    place (and re-activated if it had been deleted). Warnings (spike, DG) come
    back beside the saved ids — they never block the save.

    Statement order (mock-tested): branch-in-company check, [lock capability
    when the date is locked], meters, spell-shape conflict, 30-day history for
    every meter in ONE pass, then per row: select by key, UPDATE or INSERT.
    """
    co_id = int(body.co_id)
    branch_id = int(body.branch_id)
    today = now_ist().date()
    validate_tran_date(body.tran_date, today)
    key = _spell_key(body.spell_id)
    user_id = _user_id(token_data)
    try:
        _verify_branch_in_company(db, co_id, branch_id)
        _enforce_lock(db, body.tran_date, today, user_id, co_id, branch_id)

        meters_by_code = {}
        for r in db.execute(
            get_power_meters_query(),
            {"co_id": co_id, "branch_id": branch_id, "include_inactive": 1},
        ).fetchall():
            m = dict(r._mapping)
            meters_by_code[m["meter_code"]] = m

        rows = [r for r in body.rows if not _row_is_blank(r)]
        if not rows:
            raise HTTPException(status_code=400, detail=NO_ROWS_MSG)

        # Pure validation of every row BEFORE any per-row statement, so a bad
        # payload never touches the DB.
        warnings: List[str] = []
        prepared = []
        for r in rows:
            code = (r.meter_code or "").strip().upper()
            meter = meters_by_code.get(code)
            if meter is None:
                raise HTTPException(status_code=400, detail=UNKNOWN_METER_MSG)
            kwh = compute_kwh(r.opening_kwh, r.closing_kwh, r.kwh)
            w = dg_warning(code, meter.get("meter_type"), r.dg_hours, r.dg_litres)
            if w:
                warnings.append(w)
            prepared.append((code, r, kwh))

        # One date is keyed as a whole day OR per spell, never both: every
        # reader SUMs across spell_id and would double-count the day.
        conflict = db.execute(
            get_power_spell_conflict_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "tran_date": body.tran_date,
                "spell_id": key,
                "whole_day": WHOLE_DAY_SPELL_ID,
            },
        ).first()
        if conflict is not None:
            raise HTTPException(
                status_code=400,
                detail=SPELL_CONFLICT_WHOLE_DAY_MSG
                if key == WHOLE_DAY_SPELL_ID
                else SPELL_CONFLICT_PER_SPELL_MSG,
            )

        # The 30-day history of EVERY meter in one pass (as GET /readings does),
        # grouped per meter for the spike check.
        history: Dict[str, list] = {}
        for h in db.execute(
            get_power_kwh_history_query(), _history_binds(co_id, branch_id, body.tran_date, None)
        ).fetchall():
            m = h._mapping
            history.setdefault(m["meter_code"], []).append(m.get("kwh"))
        for code, r, kwh in prepared:
            w = spike_warning(code, kwh, median_or_none(history.get(code)))
            if w:
                warnings.append(w)

        ids: List[int] = []
        for code, r, kwh in prepared:
            key_binds = {
                "co_id": co_id,
                "branch_id": branch_id,
                "tran_date": body.tran_date,
                "spell_id": key,
                "meter_code": code,
            }
            existing = db.execute(get_power_reading_by_key_query(), key_binds).fetchone()
            values = {
                "opening_kwh": r.opening_kwh,
                "closing_kwh": r.closing_kwh,
                "kwh": kwh,
                "dg_hours": r.dg_hours,
                "dg_litres": r.dg_litres,
                "max_demand_kva": r.max_demand_kva,
                "remarks": (r.remarks or "").strip() or None,
                "updated_by": user_id,
            }
            if existing is not None:
                reading_id = int(existing.power_reading_id)
                db.execute(update_power_reading_query(), {**values, "power_reading_id": reading_id})
            else:
                try:
                    # SAVEPOINT: a concurrent save of the same key between our
                    # select and this INSERT raises on uq_jpr_scope; only the
                    # savepoint is rolled back and the row is updated instead.
                    with db.begin_nested():
                        result = db.execute(insert_power_reading_query(), {**values, **key_binds})
                    reading_id = int(result.lastrowid)
                except IntegrityError:
                    raced = db.execute(get_power_reading_by_key_query(), key_binds).fetchone()
                    if raced is None:
                        raise
                    reading_id = int(raced.power_reading_id)
                    db.execute(update_power_reading_query(), {**values, "power_reading_id": reading_id})
            ids.append(reading_id)

        db.commit()
        return {"data": {"saved": len(ids), "ids": ids, "warnings": warnings}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/reading_delete/{power_reading_id}")
def reading_delete(
    power_reading_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete one reading (same 7-day lock as a save). The row's own
    (co, branch) scopes the lock capability; ``co_id`` on the query string
    must match the row's company."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_power_reading_active_by_id_query(),
            {"power_reading_id": int(power_reading_id), "co_id": co_id},
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=READING_NOT_FOUND_MSG)
        today = now_ist().date()
        _enforce_lock(
            db, _as_date(row.tran_date), today, _user_id(token_data), co_id, int(row.branch_id)
        )
        db.execute(
            soft_delete_power_reading_query(),
            {"power_reading_id": int(power_reading_id), "updated_by": _user_id(token_data)},
        )
        db.commit()
        return {"data": {"message": "Deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoint — Power & DG report
# =============================================================================


@router.get("/power_report")
def power_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Day-wise kWh by meter, DG hours / litres, cloth and yarn tonnes (from the
    DPR's day totals) and kWh/t over [date_from, date_to] (max MAX_RANGE_DAYS),
    with an MTD block for date_to's month and a 30-day-median "std"."""
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d_from = _require_date(request, "date_from")
    d_to = _require_date(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail=DATE_ORDER_MSG)
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=RANGE_TOO_LARGE_MSG)
    try:
        _verify_branch_in_company(db, co_id, branch_id)
        window_from = report_window_from(d_from, d_to)
        reading_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_report_readings_query(),
                {"co_id": co_id, "branch_id": branch_id, "window_from": window_from, "date_to": d_to},
            ).fetchall()
        ]
        yarn_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_dpr_spinning_daily_production_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "window_from": window_from,
                    "report_date": d_to,
                    "spinning_type": SPINNING_MACHINE_TYPE_NAME,
                },
            ).fetchall()
        ]
        cloth_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_report_cloth_daily_query(),
                {"co_id": co_id, "branch_id": branch_id, "date_from": window_from, "report_date": d_to},
            ).fetchall()
        ]
        meter_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_power_meters_query(),
                {"co_id": co_id, "branch_id": branch_id, "include_inactive": 1},
            ).fetchall()
        ]
        data = build_power_report(reading_rows, yarn_rows, cloth_rows, meter_rows, d_from, d_to)
        notes = [
            "kWh/t uses the DPR's spinning (yarn) and weaving (cloth) day tonnes",
            f"Std = {POWER_MEDIAN_WINDOW_DAYS}-day median ending {d_to.isoformat()} over keyed days; no achievement %",
            "Range / MTD tonnes count keyed days only, so kWh/t reconciles with the kWh shown",
        ]
        if data["totals"]["days_keyed"] == 0:
            notes.append("No power readings keyed in this range")
        data["meta"] = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "window_from": window_from.isoformat(),
            "days_keyed": data["totals"]["days_keyed"],
            "notes": notes,
        }
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
