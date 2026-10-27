"""Low Performer Follow-up — /api/lowPerformerFollowup (owner request 2026-10-07).

The mill marks a few workers (by EB no) of Weaving, Spinning or Winding whose
production / efficiency must be followed up, keeps a dated record of remarks,
warnings / notices (fixed types + optional scanned letter) and hour deductions
entered by the department, and sees what machine / count they worked on and how
they compare with others.

Owner decisions implemented here:
  1. departments WEAVING / SPINNING / WINDING;
  2. hour deductions are ENTERED here (date, spell, hours, reason), never read
     from attendance or payroll;
  3. notices: fixed NOTICE_TYPES + optional attachment (generic file_attachment,
     module 'jute_prod_followup', file_type 'notice', entity = followup_event_id);
  4. comparisons on a person's page: (a) same machine, other spells — the people
     who worked the same loom / frame (winding: the machine from the winder ->
     machine map) on the same dates; (b) same count / quality in the department
     on the same dates; (c) the person's own trend week by week from the start
     date (with the same-date same-quality peers alongside);
  5. per day + spell rows of machine, count / quality, production, efficiency;
  6. access = the Low Producer reports' roles (menu grants, see the menu migration),
     enforced here per branch (require_followup_access): view (1) reads, create (3)
     adds a follow-up / timeline entry, edit (4) changes or closes / reopens.

Production and efficiency are NEVER re-derived: the performance view calls the
Low Producer modules' shared fetch helpers (fetch_weaving_lowprod_rows,
fetch_spinning_lowprod_rows, fetch_winding_lowprod_rows) and only normalises,
groups and averages their rows. Averages use the same "shed average" rule as the
Low Producer reports: zero / blank efficiency is excluded from every average.

Plain ``def`` handlers (sync DB), ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, 400 validation / 404 not found / 409 duplicate.
"""

import logging
import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.approval_query import get_user_edit_access
from src.common.utils import now_ist, parse_json_body
from src.config.db import get_tenant_db
from src.hrms.leaveRequest import get_worker_by_eb_no_query
from src.juteProduction.low_performer_followup_models import (
    JuteProdFollowupEvent,
    JuteProdFollowupPerson,
)
from src.juteProduction.low_performer_followup_query import (
    ATTACHMENT_FILE_TYPE,
    ATTACHMENT_MODULE,
    get_earliest_event_date_query,
    get_employee_in_branch_query,
    get_events_query,
    get_followup_menu_id_query,
    get_followup_rows_query,
    get_hours_cut_spells_query,
    get_latest_event_date_query,
    get_notice_event_scope_query,
    get_open_duplicate_query,
    get_person_hours_cuts_query,
    get_tracked_query,
    get_winding_machine_by_winder_range_query,
)
from src.juteProduction.lowprod_spinning import MAX_DAYS as SPINNING_MAX_DAYS
from src.juteProduction.lowprod_spinning import fetch_spinning_lowprod_rows
from src.juteProduction.lowprod_weaving import MAX_DAYS as WEAVING_MAX_DAYS
from src.juteProduction.lowprod_weaving import fetch_weaving_lowprod_rows, shift_of
from src.juteProduction.lowprod_weaving_query import get_branch_in_company_query
from src.juteProduction.lowprod_winding import MAX_RANGE_DAYS as WINDING_MAX_DAYS
from src.juteProduction.lowprod_winding import fetch_winding_lowprod_rows
from src.juteProduction.spinning_query import get_spells_query

logger = logging.getLogger(__name__)

router = APIRouter()
low_performer_followup_router = router

# ---------------------------------------------------------------------------
# Codes (labels are served by /setup so the UI never hardcodes them twice)
# ---------------------------------------------------------------------------

DEPTS = {"WEAVING": "Weaving", "SPINNING": "Spinning", "WINDING": "Winding"}
NOTICE_TYPES = {
    "VERBAL_WARNING": "Verbal warning",
    "WRITTEN_WARNING": "Written warning",
    "SHOW_CAUSE": "Show-cause notice",
    "FINAL_WARNING": "Final warning",
    "SUSPENSION": "Suspension",
    "OTHER": "Other",
}
CLOSE_REASONS = {
    "IMPROVED": "Improved",
    "TRANSFERRED": "Transferred",
    "LEFT": "Left",
    "OTHER": "Other",
}
EVENT_TYPES = {"REMARK": "Remark", "NOTICE": "Warning / notice", "HOURS_CUT": "Hour cut"}

STATUS_OPEN = 1
STATUS_CLOSED = 0

# Per-request window = the Low Producer report's own cap for that department
# (weaving / winding loop one day-slice per date; spinning is one range query).
MAX_DAYS_BY_DEPT = {
    "WEAVING": WEAVING_MAX_DAYS,
    "SPINNING": SPINNING_MAX_DAYS,
    "WINDING": WINDING_MAX_DAYS,
}
# Window when no dates are sent. Weaving computes one day-slice per date (31 days
# took 11.5 s on sls), so its default is shorter; explicit dates may still go up
# to the department's max.
DEFAULT_DAYS_BY_DEPT = {
    "WEAVING": 14,
    "SPINNING": SPINNING_MAX_DAYS,
    "WINDING": WINDING_MAX_DAYS,
}
# A follow-up started today has no "since" yet: show the week before it.
BASELINE_DAYS = 7
# Same fallback the winding report uses for a spell with no configured hours.
FALLBACK_SPELL_HOURS = 8.0
# No person loses more than a day's hours, whatever the spell set-up says.
DAY_HOURS_CAP = 24.0

MAX_REASON = 500
MAX_REMARKS = 1000
MAX_ISSUED_BY = 100

# Server-side half of the sidebar's hasMenuAccess on this page: the caller's
# access level on the follow-up menu for the follow-up's branch (role grants,
# as approval_utils._user_has_edit_access resolves them).
FOLLOWUP_MENU_PATH = "juteProduction/report/lowPerformerFollowup"
LEVEL_VIEW = 1
LEVEL_CREATE = 3
LEVEL_EDIT = 4
_LEVEL_NAMES = {LEVEL_VIEW: "view", LEVEL_CREATE: "create", LEVEL_EDIT: "edit"}


# =============================================================================
# Small parsing helpers
# =============================================================================


def _today() -> date:
    """Mill date (IST), not the UTC server date."""
    return now_ist().date()


def _json_object(request: Request) -> dict:
    """The POST body, which must be a JSON object (400 otherwise, never 500)."""
    try:
        body = parse_json_body(request)
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _req_int(value, field: str) -> int:
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"{field} is required")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _opt_int(value, field: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _opt_float(value, field: str) -> Optional[float]:
    """A finite number or None; NaN / Infinity (which JSON parsing accepts) -> 400."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    if not math.isfinite(f):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return f


def parse_date(value, field: str, required: bool = True) -> Optional[date]:
    if value is None or value == "":
        if required:
            raise HTTPException(status_code=400, detail=f"{field} is required")
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field} — use YYYY-MM-DD")


def _text(value, field: str, max_len: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} is too long (max {max_len})")
    return s


def validate_dept(code) -> str:
    c = (code or "").strip().upper() if isinstance(code, str) else ""
    if c not in DEPTS:
        raise HTTPException(
            status_code=400, detail="dept_code must be one of WEAVING, SPINNING, WINDING"
        )
    return c


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# =============================================================================
# Pure validation rules (unit-tested without a DB)
# =============================================================================


def validate_followup_dates(start_date: date, review_date: Optional[date], today: date) -> None:
    if start_date > today:
        raise HTTPException(status_code=400, detail="start_date cannot be in the future")
    if review_date is not None and review_date < start_date:
        raise HTTPException(status_code=400, detail="review_date cannot be before start_date")


def validate_start_vs_history(
    start_date: date, first_event_date: Optional[date], closed_date: Optional[date]
) -> None:
    """A start date may not move past recorded history."""
    if first_event_date is not None and start_date > first_event_date:
        raise HTTPException(
            status_code=400,
            detail=(
                f"start_date cannot be after the first timeline entry "
                f"({first_event_date.isoformat()})"
            ),
        )
    if closed_date is not None and start_date > closed_date:
        raise HTTPException(status_code=400, detail="start_date cannot be after closed_date")


def validate_close(
    start_date: date,
    closed_date: date,
    close_reason,
    today: date,
    last_event_date: Optional[date] = None,
) -> str:
    reason = (close_reason or "").strip().upper() if isinstance(close_reason, str) else ""
    if reason not in CLOSE_REASONS:
        raise HTTPException(
            status_code=400,
            detail="close_reason must be one of IMPROVED, TRANSFERRED, LEFT, OTHER",
        )
    if closed_date < start_date:
        raise HTTPException(status_code=400, detail="closed_date cannot be before start_date")
    if last_event_date is not None and closed_date < last_event_date:
        raise HTTPException(
            status_code=400,
            detail=(
                f"closed_date cannot be before the last timeline entry "
                f"({last_event_date.isoformat()})"
            ),
        )
    if closed_date > today:
        raise HTTPException(status_code=400, detail="closed_date cannot be in the future")
    return reason


def clean_event(body: dict, event_type: str, start_date: date, today: date) -> dict:
    """Type-specific event validation; returns the columns to store.

    REMARK needs remarks. NOTICE needs a notice_type (issued_by / remarks optional).
    HOURS_CUT needs spell_id, hours_cut > 0 and a reason (remarks); the spell's
    hours are checked by the caller (needs the DB). Fields that do not belong to
    the type are dropped, so an edit cannot leave a stale spell on a remark.
    """
    if event_type not in EVENT_TYPES:
        raise HTTPException(
            status_code=400, detail="event_type must be one of REMARK, NOTICE, HOURS_CUT"
        )
    event_date = parse_date(body.get("event_date"), "event_date")
    if event_date > today:
        raise HTTPException(status_code=400, detail="event_date cannot be in the future")
    if event_date < start_date:
        raise HTTPException(
            status_code=400,
            detail=f"event_date cannot be before the follow-up start date ({start_date.isoformat()})",
        )
    remarks = _text(body.get("remarks"), "remarks", MAX_REMARKS)
    out = {
        "event_date": event_date,
        "notice_type": None,
        "issued_by": None,
        "spell_id": None,
        "hours_cut": None,
        "remarks": remarks,
    }
    if event_type == "REMARK":
        if not remarks:
            raise HTTPException(status_code=400, detail="remarks is required for a remark")
    elif event_type == "NOTICE":
        nt = body.get("notice_type")
        nt = nt.strip().upper() if isinstance(nt, str) else ""
        if nt not in NOTICE_TYPES:
            raise HTTPException(
                status_code=400,
                detail="notice_type must be one of " + ", ".join(NOTICE_TYPES),
            )
        out["notice_type"] = nt
        out["issued_by"] = _text(body.get("issued_by"), "issued_by", MAX_ISSUED_BY)
    else:  # HOURS_CUT
        spell_id = _opt_int(body.get("spell_id"), "spell_id")
        if spell_id is None:
            raise HTTPException(status_code=400, detail="spell_id is required for an hour cut")
        hours = _opt_float(body.get("hours_cut"), "hours_cut")
        if hours is None or hours <= 0:
            raise HTTPException(status_code=400, detail="hours_cut must be greater than 0")
        if not remarks:
            raise HTTPException(status_code=400, detail="A reason (remarks) is required for an hour cut")
        out["spell_id"] = spell_id
        out["hours_cut"] = round(hours, 2)
    return out


def _pos_hours(v) -> Optional[float]:
    f = _num(v)
    return f if f is not None and f > 0 else None


def hour_cut_limits(spell_code, branch_spells: list) -> tuple:
    """(spell_limit, shift_letter, shift_limit, shift_codes) for a cut on spell_code.

    Spells of one shift letter overlap (A = A1 + A2), so they share ONE cap: the
    full shift's hours = the longest spell of that letter or the shift's own
    configured hours, whichever is larger (dev3 has no full-shift 'A' spell, only
    A1 5 h + A2 3 h on an 8 h shift); FALLBACK_SPELL_HOURS when nothing is set.
    ``branch_spells`` rows: spell_code, working_hours, shift_hours."""
    letter = shift_of(spell_code)
    own = [s for s in branch_spells if s.get("spell_code") == spell_code]
    spell_limit = max(
        (h for h in (_pos_hours(s.get("working_hours")) for s in own) if h), default=None
    ) or FALLBACK_SPELL_HOURS
    same = [s for s in branch_spells if shift_of(s.get("spell_code")) == letter]
    shift_limit = max(
        (
            h
            for s in same
            for h in (_pos_hours(s.get("working_hours")), _pos_hours(s.get("shift_hours")))
            if h
        ),
        default=None,
    ) or FALLBACK_SPELL_HOURS
    codes = sorted({str(s.get("spell_code")) for s in same if s.get("spell_code")})
    return spell_limit, letter, shift_limit, codes


def check_hours_cut_caps(
    hours_cut: float,
    spell_code,
    branch_spells: list,
    existing_cuts: list,
    event_date: date,
) -> None:
    """The new cut plus the PERSON's other active cuts that date (all follow-ups,
    any department) must fit (1) the spell, (2) the shift letter its spells share
    and (3) a 24 h day. ``existing_cuts`` rows: spell_code, hours_cut."""
    spell_limit, letter, shift_limit, codes = hour_cut_limits(spell_code, branch_spells)
    spell_cut = sum(_num(c.get("hours_cut")) or 0.0 for c in existing_cuts if c.get("spell_code") == spell_code)
    shift_cut = sum(
        _num(c.get("hours_cut")) or 0.0
        for c in existing_cuts
        if shift_of(c.get("spell_code")) == letter
    )
    day_cut = sum(_num(c.get("hours_cut")) or 0.0 for c in existing_cuts)
    shared = f"spells {', '.join(codes)} share it, " if len(codes) > 1 else ""
    checks = (
        (spell_cut, spell_limit, f"spell {spell_code} ({spell_limit:g} h)"),
        (shift_cut, shift_limit, f"shift {letter} ({shared}{shift_limit:g} h)"),
        (day_cut, DAY_HOURS_CAP, f"the day ({DAY_HOURS_CAP:g} h)"),
    )
    for already, limit, what in checks:
        if hours_cut + already > limit + 1e-9:
            left = round(max(limit - already, 0.0), 2)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot cut {hours_cut:g} h in {what} on {event_date.isoformat()}: "
                    f"{round(already, 2):g} h already cut for this person, {left:g} h left"
                ),
            )


def resolve_window(
    start_date: date,
    date_from: Optional[date],
    date_to: Optional[date],
    today: date,
    max_days: int,
    default_days: Optional[int] = None,
    closed_date: Optional[date] = None,
) -> tuple:
    """(d_from, d_to, clipped, notes) for the performance view.

    Explicit dates: both required, ordered, within max_days (else 400).
    Default: start_date .. yesterday (a closed follow-up: .. its closed_date when
    earlier), clipped to the last ``default_days`` (default max_days) with the
    start moved UP to a week boundary from start_date, so every trend week but
    the last is whole; a follow-up that starts today (or later) shows the
    BASELINE_DAYS before its start.
    """
    notes = []
    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=400, detail="Send both date_from and date_to, or neither")
    if date_from is not None:
        if date_to < date_from:
            raise HTTPException(status_code=400, detail="date_to is before date_from")
        if (date_to - date_from).days >= max_days:
            raise HTTPException(
                status_code=400, detail=f"Date range too large — at most {max_days} days"
            )
        return date_from, date_to, False, notes
    window = min(default_days or max_days, max_days)
    d_to = today - timedelta(days=1)
    if closed_date is not None and closed_date < d_to:
        d_to = closed_date
        notes.append(f"The follow-up was closed on {closed_date.isoformat()} — showing up to that date.")
    d_from = start_date
    clipped = False
    if d_from > d_to:
        d_from = d_to - timedelta(days=BASELINE_DAYS - 1)
        notes.append(
            f"The follow-up starts {start_date.isoformat()} — showing the "
            f"{BASELINE_DAYS} days before it as a baseline."
        )
    elif (d_to - d_from).days >= window:
        earliest = d_to - timedelta(days=window - 1)
        weeks = -(-(earliest - start_date).days // 7)  # ceil: first whole week inside
        d_from = start_date + timedelta(days=7 * weeks)
        if d_from > d_to:  # only with a window under a week
            d_from = earliest
        clipped = True
        notes.append(
            f"Default window: the last {(d_to - d_from).days + 1} days "
            f"({d_from.isoformat()} to {d_to.isoformat()}, starting on a week of the "
            f"follow-up; a default load covers at most {window} days). Pick From / To "
            f"dates — up to {max_days} days — to see earlier weeks."
        )
    return d_from, d_to, clipped, notes


# =============================================================================
# Pure performance builders (dept-agnostic after normalisation)
# =============================================================================


def _eff(v) -> Optional[float]:
    """Efficiency that counts towards an average (shed-average rule): zero or
    blank efficiency is excluded, never averaged in as 0."""
    f = _num(v)
    return f if f is not None and f > 0 else None


def _avg(values) -> Optional[float]:
    """Shed average: only positive efficiencies count; None when nothing does."""
    vals = [f for f in (_eff(v) for v in values) if f is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round(a - b, 2) if a is not None and b is not None else None


def _r(v, nd: int = 2) -> Optional[float]:
    f = _num(v)
    return round(f, nd) if f is not None else None


def _std(v, nd: int = 3) -> Optional[float]:
    """Standard production, or None when there is no standard (0 / blank) — the
    grid shows blank, never a 0 standard."""
    f = _r(v, nd)
    return f if f is not None and f > 0 else None


def normalize_weaving_rows(rows: list) -> list:
    """lowprod_weaving.build_base_rows rows -> common perf rows.
    std_kg = the loom's 100%-efficiency capacity (prod_100_kg, DPR basis)."""
    return [
        {
            "tran_date": r.get("tran_date"),
            "shift": r.get("shift"),
            "spell_code": r.get("spell_code"),
            "machine_id": r.get("machine_id"),
            "machine": r.get("loom") or r.get("mech_code"),
            "eb_id": r.get("eb_id"),
            "eb_no": r.get("eb_no"),
            "worker_name": r.get("worker_name"),
            "quality_id": r.get("quality_id"),
            "quality": r.get("quality_name"),
            "prod_kg": _r(r.get("act_prod_kg")) or 0.0,
            "std_kg": _r(r.get("prod_100_kg")),
            "eff_pct": _eff(r.get("eff_pct")),
            "hours": _r(r.get("hours")),
        }
        for r in rows
    ]


def normalize_spinning_rows(rows: list) -> list:
    """lowprod_spinning.shape_rows rows -> common perf rows (frame + yarn count)."""
    out = []
    for r in rows:
        eb_no = r.get("eb_no")
        out.append(
            {
                "tran_date": r.get("doff_date"),
                "shift": r.get("shift"),
                "spell_code": r.get("spell_code"),
                "machine_id": r.get("machine_id"),
                "machine": r.get("frame"),
                "eb_id": r.get("eb_id"),
                "eb_no": (eb_no.strip() if isinstance(eb_no, str) else eb_no) or None,
                "worker_name": r.get("worker_name"),
                "quality_id": r.get("item_id"),
                "quality": r.get("quality_name"),
                "prod_kg": _r(r.get("net_wt")) or 0.0,
                "std_kg": _std(r.get("std_prod")),
                "eff_pct": _eff(r.get("eff_pct")),
                "hours": _r(r.get("whrs")),
            }
        )
    return out


def normalize_winding_rows(rows: list, machine_map: dict) -> list:
    """lowprod_winding.build_base_rows rows -> common perf rows. Winding entry is
    person-keyed: the machine comes from the winder -> machine map keyed
    (date, spell_id, eb_id); a winder with no mapped machine has machine None."""
    out = []
    for r in rows:
        mc = machine_map.get((str(r.get("tran_date"))[:10], r.get("spell_id"), r.get("eb_id")))
        out.append(
            {
                "tran_date": r.get("tran_date"),
                "shift": r.get("shift"),
                "spell_code": r.get("spell_code"),
                "machine_id": mc[0] if mc else None,
                "machine": mc[1] if mc else None,
                "eb_id": r.get("eb_id"),
                "eb_no": r.get("eb_no"),
                "worker_name": r.get("worker_name"),
                "quality_id": r.get("item_id"),
                "quality": r.get("quality_name"),
                "prod_kg": _r(r.get("prod_kg"), 3) or 0.0,
                "std_kg": _std(r.get("std_prod")),
                "eff_pct": _eff(r.get("eff_pct")),
                "hours": _r(r.get("att_hrs")),
            }
        )
    return out


def build_winder_machine_map(map_rows: list) -> dict:
    """Winder -> machine map rows (each carrying tran_date, in query order) ->
    {(date, spell_id, eb_id): (machine_id, label)}. The first row of a key that
    names a machine wins; a map row without a machine is skipped (no machine to
    compare on)."""
    out = {}
    for m in map_rows:
        if m.get("machine_id") is None:
            continue
        label = (m.get("mech_code") or "").strip() or m.get("machine_name")
        out.setdefault(
            (str(m.get("tran_date"))[:10], m.get("spell_id"), m.get("eb_id")),
            (m.get("machine_id"), label),
        )
    return out


def _sort_key(r):
    return (str(r.get("tran_date") or ""), r.get("spell_code") or "", str(r.get("machine") or ""))


def build_person_rows(rows: list, eb_id: int) -> list:
    """The tracked person's own day + spell rows (machine, count, prod, eff)."""
    out = [
        {
            "tran_date": r["tran_date"],
            "shift": r["shift"],
            "spell_code": r["spell_code"],
            "machine_id": r["machine_id"],
            "machine": r["machine"],
            "quality_id": r["quality_id"],
            "quality": r["quality"],
            "prod_kg": r["prod_kg"],
            "std_kg": r["std_kg"],
            "eff_pct": r["eff_pct"],
            "hours": r["hours"],
        }
        for r in rows
        if r.get("eb_id") == eb_id
    ]
    out.sort(key=_sort_key)
    return out


def kg_per_hr(rows: list) -> Optional[float]:
    """Production per hour worked over the rows that carry hours — a plain ratio of
    the Low Producer rows' own kg and hours, shown next to efficiency because a
    quality with no standard (e.g. sls winding has no targets) has no efficiency."""
    hrs = [r for r in rows if (_num(r.get("hours")) or 0) > 0]
    total_h = sum(_num(r["hours"]) for r in hrs)
    if total_h <= 0:
        return None
    return round(sum(_num(r.get("prod_kg")) or 0.0 for r in hrs) / total_h, 2)


def build_person_summary(person_rows: list) -> dict:
    return {
        "days_worked": len({r["tran_date"] for r in person_rows}),
        "rows": len(person_rows),
        "prod_kg": round(sum(_num(r["prod_kg"]) or 0.0 for r in person_rows), 2),
        "std_kg": round(sum(_num(r["std_kg"]) or 0.0 for r in person_rows), 2),
        "avg_eff": _avg(r["eff_pct"] for r in person_rows),
        "kg_per_hr": kg_per_hr(person_rows),
    }


def _row_shift(r) -> str:
    """Shift letter of a perf row ('A1' / 'A2' / 'A' -> 'A'), the Low Producer rule."""
    return shift_of(r.get("spell_code") or r.get("shift"))


def build_same_machine(rows: list, eb_id: int) -> dict:
    """Comparison (a): on each date + machine the person worked, the OTHER people
    on that machine in the other SHIFTS of that date.

    Rows of other workers in a shift the person also worked on that machine are
    left out: spells of one letter overlap (A vs A1 / A2), and a shared frame side
    in the same shift is not "another shift"."""
    mine: dict = {}
    for r in rows:
        if r.get("eb_id") == eb_id and r.get("machine_id") is not None:
            k = (r["tran_date"], r["machine_id"])
            e = mine.setdefault(
                k, {"spells": set(), "shifts": set(), "rows": [], "machine": r["machine"]}
            )
            e["spells"].add(r["spell_code"])
            e["shifts"].add(_row_shift(r))
            e["rows"].append(r)

    detail = []
    per_machine: dict = {}
    for (d, mc), e in mine.items():
        pm = per_machine.setdefault(
            mc, {"machine": e["machine"], "dates": set(), "mine": [], "others": []}
        )
        pm["dates"].add(d)
        pm["mine"].extend(e["rows"])
    for r in rows:
        k = (r["tran_date"], r.get("machine_id"))
        e = mine.get(k)
        if e is None or r.get("eb_id") == eb_id or _row_shift(r) in e["shifts"]:
            continue
        per_machine[r["machine_id"]]["others"].append(r)
        detail.append(
            {
                "tran_date": r["tran_date"],
                "machine_id": r["machine_id"],
                "machine": r["machine"],
                "person_spells": ", ".join(sorted(s for s in e["spells"] if s)),
                "person_eff": _avg(x["eff_pct"] for x in e["rows"]),
                "person_prod_kg": round(sum(_num(x["prod_kg"]) or 0.0 for x in e["rows"]), 2),
                "other_eb_id": r.get("eb_id"),
                "other_eb_no": r.get("eb_no"),
                "other_name": r.get("worker_name"),
                "other_spell_code": r["spell_code"],
                "other_shift": r["shift"],
                "quality": r["quality"],
                "other_eff": r["eff_pct"],
                "other_prod_kg": r["prod_kg"],
                "other_hours": r["hours"],
            }
        )
    detail.sort(key=lambda x: (str(x["tran_date"]), str(x["machine"] or ""), x["other_spell_code"] or ""))
    summary = []
    for mc, pm in per_machine.items():
        mine_avg = _avg(r["eff_pct"] for r in pm["mine"])
        others_avg = _avg(r["eff_pct"] for r in pm["others"])
        summary.append(
            {
                "machine_id": mc,
                "machine": pm["machine"],
                "person_days": len(pm["dates"]),
                "person_avg_eff": mine_avg,
                "others_avg_eff": others_avg,
                "others_rows": len(pm["others"]),
                "diff": _diff(mine_avg, others_avg),
                "person_kg_per_hr": kg_per_hr(pm["mine"]),
                "others_kg_per_hr": kg_per_hr(pm["others"]),
            }
        )
    summary.sort(key=lambda x: (-x["person_days"], str(x["machine"] or "")))
    return {"summary": summary, "rows": detail}


def _worker_key(r):
    return r.get("eb_id") if r.get("eb_id") is not None else "__contract__"


def build_same_quality(rows: list, eb_id: int) -> list:
    """Comparison (b): per count / quality the person worked — the department's
    average efficiency on that quality on the dates the person worked it (with
    and without the person), and over the whole window."""
    mine: dict = {}
    for r in rows:
        if r.get("eb_id") == eb_id:
            e = mine.setdefault(r["quality_id"], {"quality": r["quality"], "dates": set(), "rows": []})
            e["dates"].add(r["tran_date"])
            e["rows"].append(r)
    out = []
    for q, e in mine.items():
        same_dates = [r for r in rows if r["quality_id"] == q and r["tran_date"] in e["dates"]]
        others = [r for r in same_dates if r.get("eb_id") != eb_id]
        person_avg = _avg(r["eff_pct"] for r in e["rows"])
        others_avg = _avg(r["eff_pct"] for r in others)
        out.append(
            {
                "quality_id": q,
                "quality": e["quality"],
                "person_days": len(e["dates"]),
                "person_rows": len(e["rows"]),
                "person_avg_eff": person_avg,
                "dept_avg_eff": _avg(r["eff_pct"] for r in same_dates),
                "others_avg_eff": others_avg,
                "others_workers": len({_worker_key(r) for r in others}),
                "range_dept_avg_eff": _avg(r["eff_pct"] for r in rows if r["quality_id"] == q),
                "diff": _diff(person_avg, others_avg),
                "person_kg_per_hr": kg_per_hr(e["rows"]),
                "others_kg_per_hr": kg_per_hr(others),
            }
        )
    out.sort(key=lambda x: (-x["person_rows"], str(x["quality"] or "")))
    return out


def week_no_of(d: date, start_date: date) -> int:
    """1 = the first 7 days from start_date; 0, -1, ... = weeks before marking."""
    return (d - start_date).days // 7 + 1


def build_weekly_trend(rows: list, eb_id: int, start_date: date) -> list:
    """Comparison (c): the person's efficiency week by week, weeks anchored on the
    follow-up start date, with the peers who worked the same quality on the same
    dates as the person that week (peer_avg_eff) for context."""
    weeks: dict = {}
    for r in rows:
        if r.get("eb_id") != eb_id:
            continue
        d = parse_date(r["tran_date"], "tran_date")
        w = weeks.setdefault(week_no_of(d, start_date), {"dates": set(), "rows": [], "pairs": set()})
        w["dates"].add(r["tran_date"])
        w["rows"].append(r)
        w["pairs"].add((r["tran_date"], r["quality_id"]))
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        ws = start_date + timedelta(days=(wk - 1) * 7)
        peers = [
            r
            for r in rows
            if r.get("eb_id") != eb_id and (r["tran_date"], r["quality_id"]) in w["pairs"]
        ]
        out.append(
            {
                "week_no": wk,
                "week_start": ws.isoformat(),
                "week_end": (ws + timedelta(days=6)).isoformat(),
                "days_worked": len(w["dates"]),
                "prod_kg": round(sum(_num(r["prod_kg"]) or 0.0 for r in w["rows"]), 2),
                "avg_eff": _avg(r["eff_pct"] for r in w["rows"]),
                "peer_avg_eff": _avg(r["eff_pct"] for r in peers),
                "kg_per_hr": kg_per_hr(w["rows"]),
                "peer_kg_per_hr": kg_per_hr(peers),
            }
        )
    return out


def performance_notes(dept: str, rows: list, person_rows: list, eb_no) -> list:
    notes = []
    if not person_rows:
        notes.append(
            f"No {DEPTS[dept].lower()} production rows for EB {eb_no or '?'} in this window."
        )
    elif all(r["eff_pct"] is None for r in person_rows):
        notes.append(
            "None of the person's rows has a standard / target, so no efficiency is "
            "shown — compare kg per hour instead."
        )
    if dept == "WINDING":
        notes.append(
            "Winding machines come from the winder -> machine map (Winding Quality tab); "
            "days without a mapped machine have no same-machine comparison."
        )
    elif dept == "SPINNING":
        notes.append(
            "Spinning efficiency is net weight against the frame's standard pro-rated to "
            "the hours worked (Spinning Low Producer basis)."
        )
    else:
        notes.append(
            "Weaving efficiency comes from the weaving day-slice per loom + spell "
            "(Weaving Low Producer basis)."
        )
    notes.append(
        "Averages leave out rows with zero / blank efficiency (shed-average rule)."
    )
    return notes


# =============================================================================
# DB helpers
# =============================================================================


def shape_followup_row(m: dict, today: date) -> dict:
    status = int(m.get("status") or 0)
    start = m.get("start_date")
    review = m.get("review_date")
    closed = m.get("closed_date")
    end = closed if (status == STATUS_CLOSED and closed) else today
    days = (end - start).days if isinstance(start, date) and isinstance(end, date) else 0
    return {
        "followup_id": int(m["followup_id"]),
        "co_id": int(m["co_id"]),
        "branch_id": int(m["branch_id"]),
        "eb_id": int(m["eb_id"]),
        "eb_no": m.get("eb_no"),
        "worker_name": m.get("worker_name"),
        "dept_code": m.get("dept_code"),
        "reason": m.get("reason"),
        "start_date": _iso(start),
        "review_date": _iso(review),
        "status": status,
        "closed_date": _iso(closed),
        "close_reason": m.get("close_reason"),
        "close_remarks": m.get("close_remarks"),
        "remark_count": int(m.get("remark_count") or 0),
        "notice_count": int(m.get("notice_count") or 0),
        "hours_cut_count": int(m.get("hours_cut_count") or 0),
        "total_hours_cut": round(_num(m.get("total_hours_cut")) or 0.0, 2),
        "last_notice_type": m.get("last_notice_type"),
        "last_event_date": _iso(m.get("last_event_date")),
        "review_due": bool(
            status == STATUS_OPEN and isinstance(review, date) and review <= today
        ),
        "days_tracked": max(days, 0),
        "updated_date_time": _iso(m.get("updated_date_time")),
    }


def shape_event_row(m: dict) -> dict:
    nt = m.get("notice_type")
    return {
        "followup_event_id": int(m["followup_event_id"]),
        "followup_id": int(m["followup_id"]),
        "event_type": m.get("event_type"),
        "event_date": _iso(m.get("event_date")),
        "notice_type": nt,
        "notice_type_label": NOTICE_TYPES.get(nt) if nt else None,
        "issued_by": m.get("issued_by"),
        "spell_id": m.get("spell_id"),
        "spell_code": m.get("spell_code"),
        "hours_cut": _r(m.get("hours_cut")),
        "remarks": m.get("remarks"),
        "attachment_id": m.get("attachment_id"),
        "attachment_filename": m.get("attachment_filename"),
        "updated_by": m.get("updated_by"),
        "updated_date_time": _iso(m.get("updated_date_time")),
    }


def _fetch_followup_rows(db: Session, co_id: int, branch_id=None, dept_code=None, status=None, followup_id=None):
    return [
        dict(r._mapping)
        for r in db.execute(
            get_followup_rows_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "dept_code": dept_code,
                "status": status,
                "followup_id": followup_id,
            },
        ).fetchall()
    ]


def _followup_out(db: Session, co_id: int, followup_id: int) -> dict:
    rows = _fetch_followup_rows(db, co_id, followup_id=followup_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    return shape_followup_row(rows[0], _today())


def _load_followup(db: Session, co_id: int, followup_id: int) -> JuteProdFollowupPerson:
    f = db.get(JuteProdFollowupPerson, followup_id)
    if f is None or int(f.active or 0) != 1 or int(f.co_id) != co_id:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    return f


def _require_open(f: JuteProdFollowupPerson) -> None:
    if int(f.status) != STATUS_OPEN:
        raise HTTPException(
            status_code=400, detail="This follow-up is closed — reopen it to change its timeline"
        )


def _verify_branch(db: Session, co_id: int, branch_id: int) -> None:
    if not db.execute(
        get_branch_in_company_query(), {"branch_id": branch_id, "co_id": co_id}
    ).first():
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")


def _branch_spells(db: Session, branch_id: int) -> list:
    return [dict(r._mapping) for r in db.execute(get_spells_query(), {"branch_id": branch_id}).fetchall()]


def followup_access_level(db: Session, user_id, branch_id) -> int:
    """Highest access_type_id the user holds on the follow-up menu for the branch
    (1 view .. 4 edit; 0 = no grant / no menu). Same resolution as
    approval_utils._user_has_edit_access: get_user_edit_access over the user's
    roles on the branch, with the control_co_module override."""
    if not user_id or branch_id is None:
        return 0
    menu = db.execute(get_followup_menu_id_query(), {"menu_path": FOLLOWUP_MENU_PATH}).first()
    if not menu or menu[0] is None:
        return 0
    row = db.execute(
        get_user_edit_access(),
        {"user_id": int(user_id), "branch_id": int(branch_id), "menu_id": int(menu[0])},
    ).first()
    level = row[0] if row else None
    try:
        return int(level) if level is not None else 0
    except (TypeError, ValueError):
        return 0


def require_followup_access(db: Session, token_data: dict, branch_id, level: int) -> None:
    """403 unless the caller holds at least ``level`` on the follow-up menu for
    the branch (the stored follow-up's branch for id-based calls)."""
    user_id = (token_data or {}).get("user_id")
    if followup_access_level(db, user_id, branch_id) < level:
        raise HTTPException(
            status_code=403,
            detail=(
                f"You need {_LEVEL_NAMES.get(level, str(level))} access to Low Performer "
                "Follow-up for this branch"
            ),
        )


def _require_branch_access(db: Session, token_data: dict, co_id: int, branch_id: int, level: int) -> None:
    """Client-sent co_id + branch_id: the pair must match, then the level is checked."""
    _verify_branch(db, co_id, branch_id)
    require_followup_access(db, token_data, branch_id, level)


def check_notice_attachment_access(
    db: Session, token_data: dict, co_id: int, entity_id: int, action: str
) -> None:
    """Gate of the generic attachment API for module 'jute_prod_followup'
    (entity_id = followup_event_id), called by src/common/attachments/router.py.

    read (list / download): the event's follow-up is in co_id, view (>= 1).
    upload / delete: also an ACTIVE NOTICE row of an OPEN follow-up, with
    create (>= 3) / edit (>= 4) on the follow-up's branch."""
    level = {"read": LEVEL_VIEW, "upload": LEVEL_CREATE, "delete": LEVEL_EDIT}[action]
    row = db.execute(get_notice_event_scope_query(), {"followup_event_id": int(entity_id)}).first()
    m = row._mapping if row is not None else None
    if m is None or int(m["co_id"]) != int(co_id) or int(m["followup_active"] or 0) != 1:
        raise HTTPException(status_code=404, detail="Notice entry not found")
    require_followup_access(db, token_data, int(m["branch_id"]), level)
    if action == "read":
        return
    if int(m["event_active"] or 0) != 1 or m["event_type"] != "NOTICE":
        raise HTTPException(
            status_code=400, detail="A scan can only be attached to an active warning / notice entry"
        )
    if int(m["status"]) != STATUS_OPEN:
        raise HTTPException(
            status_code=400, detail="This follow-up is closed — reopen it to change its timeline"
        )


def _duplicate_message(dept_code: str, dup_id=None) -> str:
    ref = f" (#{dup_id})" if dup_id is not None else ""
    return f"This person is already on an open {DEPTS[dept_code]} follow-up{ref} in this branch"


def _find_open_duplicate(db: Session, f_like: dict, exclude_id=None):
    dup = db.execute(
        get_open_duplicate_query(),
        {
            "co_id": f_like["co_id"],
            "branch_id": f_like["branch_id"],
            "eb_id": f_like["eb_id"],
            "dept_code": f_like["dept_code"],
            "exclude_id": exclude_id,
        },
    ).first()
    return dup[0] if dup else None


def _guard_duplicate(db: Session, f_like: dict, exclude_id=None) -> None:
    dup_id = _find_open_duplicate(db, f_like, exclude_id)
    if dup_id is not None:
        raise HTTPException(status_code=409, detail=_duplicate_message(f_like["dept_code"], dup_id))


def _is_open_unique_violation(exc: IntegrityError) -> bool:
    """The DB's one-open-follow-up key (uq_jpfp_open) refused the write — a
    concurrent request opened the same person + branch + dept first."""
    orig = getattr(exc, "orig", None)
    code = orig.args[0] if orig is not None and getattr(orig, "args", None) else None
    return code == 1062 or "uq_jpfp_open" in str(exc)


def _duplicate_conflict(db: Session, f_like: dict, exclude_id=None) -> HTTPException:
    """409 for a commit the unique key refused, with the app guard's message
    (the winning row's id when it can be read back)."""
    _rollback(db)
    try:
        dup_id = _find_open_duplicate(db, f_like, exclude_id)
    except Exception:  # pragma: no cover — the message is still right without the id
        dup_id = None
    return HTTPException(status_code=409, detail=_duplicate_message(f_like["dept_code"], dup_id))


def _event_out(db: Session, co_id: int, followup_id: int, event_id: int) -> dict:
    rows = db.execute(
        get_events_query(),
        {
            "followup_id": followup_id,
            "co_id": co_id,
            "module": ATTACHMENT_MODULE,
            "file_type": ATTACHMENT_FILE_TYPE,
            "followup_event_id": event_id,
        },
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Event not found")
    return shape_event_row(dict(rows[0]._mapping))


def _check_hours_cut(db: Session, f: JuteProdFollowupPerson, cleaned: dict, exclude_id=None) -> None:
    """The spell must be the branch's; the cut must fit the spell, the shift and
    the day together with the person's other cuts that date (all follow-ups)."""
    branch_spells = [
        dict(r._mapping)
        for r in db.execute(get_hours_cut_spells_query(), {"branch_id": int(f.branch_id)}).fetchall()
    ]
    spell = next(
        (s for s in branch_spells if int(s["spell_id"]) == int(cleaned["spell_id"])), None
    )
    if spell is None:
        raise HTTPException(status_code=400, detail="spell_id is not a spell of this branch")
    existing = [
        dict(r._mapping)
        for r in db.execute(
            get_person_hours_cuts_query(),
            {"eb_id": int(f.eb_id), "event_date": cleaned["event_date"], "exclude_id": exclude_id},
        ).fetchall()
    ]
    check_hours_cut_caps(
        cleaned["hours_cut"], spell.get("spell_code"), branch_spells, existing, cleaned["event_date"]
    )


def _rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:  # pragma: no cover — best effort
        pass


# =============================================================================
# Endpoints — register
# =============================================================================


@router.get("/setup")
def followup_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Code lists + the branch's spells (for the hour-cut form)."""
    co_id = _req_int(request.query_params.get("co_id"), "co_id")
    branch_id = _req_int(request.query_params.get("branch_id"), "branch_id")
    try:
        _require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        seen, spells = set(), []
        for s in _branch_spells(db, branch_id):
            # spell_mst fans out per shift; one entry per code for the dropdown.
            if s.get("spell_code") in seen:
                continue
            seen.add(s.get("spell_code"))
            spells.append(
                {
                    "spell_id": int(s["spell_id"]),
                    "spell_code": s.get("spell_code"),
                    "spell_name": s.get("spell_name"),
                    "working_hours": _num(s.get("working_hours")),
                }
            )

        def cl(d):
            return [{"code": k, "label": v} for k, v in d.items()]

        return {
            "data": {
                "depts": cl(DEPTS),
                "notice_types": cl(NOTICE_TYPES),
                "close_reasons": cl(CLOSE_REASONS),
                "event_types": cl(EVENT_TYPES),
                "spells": spells,
                "today": _today().isoformat(),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup setup failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def followup_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = _req_int(q.get("co_id"), "co_id")
    branch_id = _req_int(q.get("branch_id"), "branch_id")
    dept = validate_dept(q.get("dept_code")) if q.get("dept_code") else None
    status = _opt_int(q.get("status"), "status")
    if status is not None and status not in (STATUS_OPEN, STATUS_CLOSED):
        raise HTTPException(status_code=400, detail="status must be 1 (open) or 0 (closed)")
    try:
        _require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        today = _today()
        rows = _fetch_followup_rows(db, co_id, branch_id, dept, status)
        return {"data": [shape_followup_row(r, today) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup list failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get")
def followup_get(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _req_int(request.query_params.get("co_id"), "co_id")
    followup_id = _req_int(request.query_params.get("followup_id"), "followup_id")
    try:
        row = _followup_out(db, co_id, followup_id)
        require_followup_access(db, token_data, row["branch_id"], LEVEL_VIEW)
        return {"data": row}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup get failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create")
def followup_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    branch_id = _req_int(body.get("branch_id"), "branch_id")
    dept = validate_dept(body.get("dept_code"))
    today = _today()
    start_date = parse_date(body.get("start_date"), "start_date")
    review_date = parse_date(body.get("review_date"), "review_date", required=False)
    validate_followup_dates(start_date, review_date, today)
    reason = _text(body.get("reason"), "reason", MAX_REASON)
    eb_no = (str(body.get("eb_no") or "")).strip()
    eb_id_in = _opt_int(body.get("eb_id"), "eb_id")
    if not eb_no and eb_id_in is None:
        raise HTTPException(status_code=400, detail="eb_no (or eb_id) is required")
    try:
        _require_branch_access(db, token_data, co_id, branch_id, LEVEL_CREATE)
        if eb_no:
            emp = db.execute(
                get_worker_by_eb_no_query(), {"emp_code": eb_no, "branch_id": branch_id}
            ).first()
        else:
            emp = db.execute(
                get_employee_in_branch_query(), {"eb_id": eb_id_in, "branch_id": branch_id}
            ).first()
        if not emp:
            raise HTTPException(
                status_code=400, detail="Employee not found for this EB No in the selected branch"
            )
        eb_id = int(emp._mapping["eb_id"])
        key = {"co_id": co_id, "branch_id": branch_id, "eb_id": eb_id, "dept_code": dept}
        _guard_duplicate(db, key)
        f = JuteProdFollowupPerson(
            **key,
            reason=reason,
            start_date=start_date,
            review_date=review_date,
            status=STATUS_OPEN,
            active=1,
            updated_by=token_data.get("user_id"),
        )
        db.add(f)
        try:
            db.commit()
        except IntegrityError as ie:
            if _is_open_unique_violation(ie):
                raise _duplicate_conflict(db, key)
            raise
        db.refresh(f)
        return {"data": _followup_out(db, co_id, f.followup_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup create failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
def followup_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    followup_id = _req_int(body.get("followup_id"), "followup_id")
    start_date = parse_date(body.get("start_date"), "start_date")
    review_date = parse_date(body.get("review_date"), "review_date", required=False)
    validate_followup_dates(start_date, review_date, _today())
    reason = _text(body.get("reason"), "reason", MAX_REASON)
    try:
        f = _load_followup(db, co_id, followup_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_EDIT)
        first = db.execute(get_earliest_event_date_query(), {"followup_id": followup_id}).first()
        first_date = parse_date(first[0], "first_date", required=False) if first else None
        validate_start_vs_history(
            start_date, first_date, f.closed_date if int(f.status) == STATUS_CLOSED else None
        )
        f.reason = reason
        f.start_date = start_date
        f.review_date = review_date
        f.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": _followup_out(db, co_id, followup_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup update failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/close")
def followup_close(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    followup_id = _req_int(body.get("followup_id"), "followup_id")
    closed_date = parse_date(body.get("closed_date"), "closed_date")
    remarks = _text(body.get("close_remarks"), "close_remarks", MAX_REASON)
    try:
        f = _load_followup(db, co_id, followup_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_EDIT)
        if int(f.status) != STATUS_OPEN:
            raise HTTPException(status_code=400, detail="This follow-up is already closed")
        last = db.execute(get_latest_event_date_query(), {"followup_id": followup_id}).first()
        last_date = parse_date(last[0], "last_date", required=False) if last else None
        reason = validate_close(
            f.start_date, closed_date, body.get("close_reason"), _today(), last_date
        )
        f.status = STATUS_CLOSED
        f.closed_date = closed_date
        f.close_reason = reason
        f.close_remarks = remarks
        f.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": _followup_out(db, co_id, followup_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup close failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen")
def followup_reopen(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reopen a closed follow-up. The close details move into an automatic REMARK
    on the timeline (dated today) so the record keeps them."""
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    followup_id = _req_int(body.get("followup_id"), "followup_id")
    try:
        f = _load_followup(db, co_id, followup_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_EDIT)
        if int(f.status) != STATUS_CLOSED:
            raise HTTPException(status_code=400, detail="This follow-up is already open")
        key = {"co_id": f.co_id, "branch_id": f.branch_id, "eb_id": f.eb_id, "dept_code": f.dept_code}
        _guard_duplicate(db, key, exclude_id=f.followup_id)
        note = (
            f"Follow-up reopened. It was closed on "
            f"{_iso(f.closed_date) or '?'} ({CLOSE_REASONS.get(f.close_reason or '', f.close_reason or '-')})"
            + (f": {f.close_remarks}" if f.close_remarks else "")
        )[:MAX_REMARKS]
        db.add(
            JuteProdFollowupEvent(
                followup_id=f.followup_id,
                event_type="REMARK",
                event_date=max(_today(), f.start_date),
                remarks=note,
                active=1,
                updated_by=token_data.get("user_id"),
            )
        )
        f.status = STATUS_OPEN
        f.closed_date = None
        f.close_reason = None
        f.close_remarks = None
        f.updated_by = token_data.get("user_id")
        try:
            db.commit()
        except IntegrityError as ie:
            if _is_open_unique_violation(ie):
                raise _duplicate_conflict(db, key, exclude_id=followup_id)
            raise
        return {"data": _followup_out(db, co_id, followup_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup reopen failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — timeline
# =============================================================================


@router.get("/events")
def followup_events(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _req_int(request.query_params.get("co_id"), "co_id")
    followup_id = _req_int(request.query_params.get("followup_id"), "followup_id")
    try:
        f = _load_followup(db, co_id, followup_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_VIEW)
        rows = db.execute(
            get_events_query(),
            {
                "followup_id": followup_id,
                "co_id": co_id,
                "module": ATTACHMENT_MODULE,
                "file_type": ATTACHMENT_FILE_TYPE,
                "followup_event_id": None,
            },
        ).fetchall()
        return {"data": [shape_event_row(dict(r._mapping)) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup events failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/event_create")
def followup_event_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    followup_id = _req_int(body.get("followup_id"), "followup_id")
    event_type = (str(body.get("event_type") or "")).strip().upper()
    try:
        f = _load_followup(db, co_id, followup_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_CREATE)
        _require_open(f)
        cleaned = clean_event(body, event_type, f.start_date, _today())
        if event_type == "HOURS_CUT":
            _check_hours_cut(db, f, cleaned)
        ev = JuteProdFollowupEvent(
            followup_id=f.followup_id,
            event_type=event_type,
            active=1,
            updated_by=token_data.get("user_id"),
            **cleaned,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        return {"data": _event_out(db, co_id, f.followup_id, ev.followup_event_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup event create failed")
        raise HTTPException(status_code=500, detail=str(e))


def _load_event(db: Session, co_id: int, event_id: int) -> tuple:
    ev = db.get(JuteProdFollowupEvent, event_id)
    if ev is None or int(ev.active or 0) != 1:
        raise HTTPException(status_code=404, detail="Event not found")
    f = _load_followup(db, co_id, int(ev.followup_id))
    return ev, f


@router.post("/event_update")
def followup_event_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    event_id = _req_int(body.get("followup_event_id"), "followup_event_id")
    try:
        ev, f = _load_event(db, co_id, event_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_EDIT)
        _require_open(f)
        sent_type = (str(body.get("event_type") or "")).strip().upper()
        if sent_type and sent_type != ev.event_type:
            raise HTTPException(status_code=400, detail="event_type cannot be changed")
        cleaned = clean_event(body, ev.event_type, f.start_date, _today())
        if ev.event_type == "HOURS_CUT":
            _check_hours_cut(db, f, cleaned, exclude_id=ev.followup_event_id)
        for k, v in cleaned.items():
            setattr(ev, k, v)
        ev.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": _event_out(db, co_id, f.followup_id, ev.followup_event_id)}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup event update failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/event_deactivate")
def followup_event_deactivate(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = _json_object(request)
    co_id = _req_int(body.get("co_id"), "co_id")
    event_id = _req_int(body.get("followup_event_id"), "followup_event_id")
    try:
        ev, f = _load_event(db, co_id, event_id)
        require_followup_access(db, token_data, f.branch_id, LEVEL_EDIT)
        _require_open(f)
        ev.active = 0
        ev.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": {"followup_event_id": event_id, "active": 0}}
    except HTTPException:
        _rollback(db)
        raise
    except Exception as e:
        _rollback(db)
        logger.exception("followup event deactivate failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — lookups + performance
# =============================================================================


@router.get("/tracked")
def followup_tracked(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Open follow-ups of a branch + dept (the Low Producer pages' "Tracked" badge)."""
    q = request.query_params
    co_id = _req_int(q.get("co_id"), "co_id")
    branch_id = _req_int(q.get("branch_id"), "branch_id")
    dept = validate_dept(q.get("dept_code"))
    try:
        _require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        rows = db.execute(
            get_tracked_query(), {"co_id": co_id, "branch_id": branch_id, "dept_code": dept}
        ).fetchall()
        return {
            "data": [
                {
                    "followup_id": int(m["followup_id"]),
                    "eb_id": int(m["eb_id"]),
                    "dept_code": m["dept_code"],
                    "start_date": _iso(m["start_date"]),
                    "review_date": _iso(m["review_date"]),
                }
                for m in (r._mapping for r in rows)
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup tracked failed")
        raise HTTPException(status_code=500, detail=str(e))


def fetch_dept_rows(db: Session, dept: str, co_id: int, branch_id: int, d_from: date, d_to: date) -> list:
    """Normalised perf rows of the whole department (branch) over the window,
    through the matching Low Producer module's shared fetch."""
    if dept == "WEAVING":
        return normalize_weaving_rows(fetch_weaving_lowprod_rows(db, co_id, branch_id, d_from, d_to))
    if dept == "SPINNING":
        return normalize_spinning_rows(fetch_spinning_lowprod_rows(db, co_id, branch_id, d_from, d_to))
    rows, _dups = fetch_winding_lowprod_rows(db, co_id, branch_id, d_from, d_to)
    map_rows = [
        dict(r._mapping)
        for r in db.execute(
            get_winding_machine_by_winder_range_query(),
            {"branch_id": branch_id, "date_from": d_from, "date_to": d_to},
        ).fetchall()
    ]
    return normalize_winding_rows(rows, build_winder_machine_map(map_rows))


def build_performance(followup: dict, rows: list, start_date: date) -> dict:
    eb_id = followup["eb_id"]
    person_rows = build_person_rows(rows, eb_id)
    return {
        "person_rows": person_rows,
        "person_summary": build_person_summary(person_rows),
        "same_machine": build_same_machine(rows, eb_id),
        "same_quality": build_same_quality(rows, eb_id),
        "weekly_trend": build_weekly_trend(rows, eb_id, start_date),
        "notes": performance_notes(followup["dept_code"], rows, person_rows, followup.get("eb_no")),
    }


@router.get("/performance")
def followup_performance(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Per day + spell rows of the person (machine, count / quality, production,
    efficiency) and comparisons (a) same machine other shifts, (b) same quality in
    the department, (c) weekly trend from the start date.

    No dates = the default window (DEFAULT_DAYS_BY_DEPT, ending yesterday or at the
    close date); ``default_days`` / ``max_days`` / ``clipped`` / notes tell the UI
    which window was used."""
    q = request.query_params
    co_id = _req_int(q.get("co_id"), "co_id")
    followup_id = _req_int(q.get("followup_id"), "followup_id")
    date_from = parse_date(q.get("date_from"), "date_from", required=False)
    date_to = parse_date(q.get("date_to"), "date_to", required=False)
    try:
        followup = _followup_out(db, co_id, followup_id)
        require_followup_access(db, token_data, followup["branch_id"], LEVEL_VIEW)
        dept = followup["dept_code"]
        start_date = parse_date(followup["start_date"], "start_date")
        max_days = MAX_DAYS_BY_DEPT[dept]
        default_days = DEFAULT_DAYS_BY_DEPT[dept]
        closed_date = (
            parse_date(followup["closed_date"], "closed_date", required=False)
            if followup["status"] == STATUS_CLOSED
            else None
        )
        d_from, d_to, clipped, window_notes = resolve_window(
            start_date, date_from, date_to, _today(), max_days, default_days, closed_date
        )
        rows = fetch_dept_rows(db, dept, co_id, followup["branch_id"], d_from, d_to)
        perf = build_performance(followup, rows, start_date)
        perf["notes"] = window_notes + perf["notes"]
        return {
            "data": {
                "followup": followup,
                "date_from": d_from.isoformat(),
                "date_to": d_to.isoformat(),
                "max_days": max_days,
                "default_days": default_days,
                "clipped": clipped,
                **perf,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("followup performance failed")
        raise HTTPException(status_code=500, detail=str(e))
