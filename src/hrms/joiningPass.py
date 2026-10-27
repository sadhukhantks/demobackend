"""Joining Pass — /api/joiningPass (HRMS, owner request 2026-10-24).

A worker absent for MORE than ``min_absent_days`` working days (default 3) needs a
joining pass from the Personnel office before rejoining. This router:

  * /check      the attendance criterion for one worker and a joining date: last
                seen day, first day of absence, absent working / calendar days,
                pass required yes / no, the pass already issued for this absence,
                and the letter's "last fortnight of working" figures (days present
                out of working days, efficiency). The separate attendance app can
                call it later to refuse attendance without a pass (not done here).
  * /issue      stores the pass with those figures frozen; /list, /get (reprint),
                /cancel; /absentees = workers absent above the threshold today.
  * /setup, /rule_save  rule per company + letterhead + access level.

Owner rulings 2026-10-24: own HRMS page (menu hrms/joiningPass); letter blanks filled
from data, read-only, a blank line when there is no data; WORKING days = calendar
days minus approved holidays of the branch (holiday_master status 3) and active
non-working days (tbl_non_working_days) — approved leave already counts as "seen"
(Long Absenteeism rule), so a leave never makes an absence.

The "seen" rule, the worker snapshot, the production basis (Low Producer fetch) and
the letterhead are reused from the Long Absenteeism module, never re-derived.
Plain ``def`` handlers (sync DB), ``{"data": ...}`` responses.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.approval_query import get_user_edit_access
from src.config.db import get_tenant_db
from src.hrms.constants import RESIGN_DETAIL_STATUSES
from src.hrms.joiningPassModels import HrmsJoiningPass, HrmsJoiningPassRule
from src.hrms.joiningPassQuery import (
    get_absentees_query,
    get_branch_spells_query,
    get_eb_by_code_query,
    get_off_days_query,
    get_pass_for_absence_query,
    get_pass_list_query,
    get_pass_query,
    get_presence_dates_query,
    get_table_exists_query,
)
from src.juteProduction import long_absenteeism_common as lac
from src.juteProduction.long_absenteeism import perf_dept_of
from src.juteProduction.long_absenteeism_common import (
    TtlCache,
    db_name,
    json_object,
    opt_int,
    opt_text,
    parse_date,
    req_int,
    rollback,
    today_ist,
    verify_branch,
)
from src.juteProduction.long_absenteeism_letters import company_address
from src.juteProduction.long_absenteeism_letters_query import get_company_letterhead_query
from src.juteProduction.long_absenteeism_query import (
    get_branch_depts_query,
    get_last_attendance_query,
    get_last_seen_query,
    get_live_case_query,
    get_worker_snapshot_query,
)
from src.juteProduction.low_performer_followup import MAX_DAYS_BY_DEPT, build_person_rows, build_person_summary, fetch_dept_rows

logger = logging.getLogger(__name__)

router = APIRouter()

MENU_PATH = "hrms/joiningPass"
LEVEL_VIEW = 1
LEVEL_CREATE = 3
LEVEL_EDIT = 4
_LEVEL_NAMES = {LEVEL_VIEW: "view", LEVEL_CREATE: "create", LEVEL_EDIT: "edit"}

RULE_DEFAULTS = {"min_absent_days": 3, "lookback_days": 60, "fortnight_days": 14}
RULE_LIMITS = {"min_absent_days": (1, 60), "lookback_days": (7, 366), "fortnight_days": (7, 31)}

# "Authorised by" on the pass (reference screen's Authority list); the first is the default.
AUTHORITIES = ("Welfare Officer", "Personnel Manager", "Labour Officer")

MAX_REMARKS = 500
MAX_CANCEL_REASON = 300
MAX_JOIN_AHEAD_DAYS = 30
BLANK = "____________"

# The fortnight efficiency runs the Low Producer fetch (weaving ~5 s for 14 days on
# sls); /check and the /issue that follows it share one result for 10 minutes.
_PERF_CACHE = TtlCache(600)
_MENU_ID_CACHE = TtlCache(600)


# =============================================================================
# Pure helpers (unit-tested without a DB)
# =============================================================================


def as_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def iso(v) -> Optional[str]:
    d = as_date(v)
    return d.isoformat() if d is not None else None


def fmt_letter_date(d) -> str:
    """16-Jan-2026, the sample letter's date style."""
    d = as_date(d)
    return d.strftime("%d-%b-%Y") if d is not None else BLANK


def working_days(d_from: date, d_to: date, off_days: Iterable[date]) -> list:
    """Working days from d_from to d_to inclusive: every calendar day that is
    not an off day (approved holiday / non-working day). Empty when d_from > d_to."""
    off = set(off_days)
    out, d = [], d_from
    while d <= d_to:
        if d not in off:
            out.append(d)
        d += timedelta(days=1)
    return out


def absence_of(last_seen: date, join_date: date, off_days: Iterable[date]) -> dict:
    """The absence between the last seen day and the joining day, both excluded:
    working days and calendar days absent. ``absent_from`` is the day after the
    last seen day (the sample letter's "absenting from", even on a Sunday);
    None when there is no day in between."""
    first = last_seen + timedelta(days=1)
    days = working_days(first, join_date - timedelta(days=1), off_days)
    return {
        "absent_from": first if first < join_date else None,
        "absent_working_days": len(days),
        "absent_calendar_days": max((join_date - last_seen).days - 1, 0),
    }


def pass_required(absent_working_days: int, min_absent_days: int) -> bool:
    """A pass is needed when absent MORE than min_absent_days working days."""
    return int(absent_working_days) > int(min_absent_days)


def fortnight_window(last_attendance: date, fortnight_days: int) -> tuple:
    """(first day, last day) of the last fortnight of working: the
    ``fortnight_days`` calendar days ending on the last attendance day."""
    return last_attendance - timedelta(days=int(fortnight_days) - 1), last_attendance


def perf_window(fn_from: date, fn_to: date, dept: str) -> tuple:
    """The fortnight capped to the department's Low Producer window."""
    max_days = int(MAX_DAYS_BY_DEPT[dept])
    return max(fn_from, fn_to - timedelta(days=max_days - 1)), fn_to


def make_pass_no(branch_id: int, on_date: date, pass_id: int) -> str:
    """JP/<branch_id>/<yy>/<pass id, at least 4 digits> — from the row's own id, so
    two passes issued at the same moment never share a number."""
    return f"JP/{int(branch_id)}/{on_date.year % 100:02d}/{int(pass_id):04d}"


def clean_rule(body: dict) -> dict:
    """Validated rule fields of a /rule_save body (400 on a bad value)."""
    out = {}
    for key, (lo, hi) in RULE_LIMITS.items():
        v = body.get(key)
        if v is None or v == "":
            raise HTTPException(status_code=400, detail=f"{key} is required")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid {key} format")
        if n < lo or n > hi:
            raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
        out[key] = n
    return out


def _num(v) -> Optional[float]:
    if v is None:
        return None
    return float(v) if isinstance(v, (Decimal, int, float)) else None


def letterhead_address(company: Optional[dict]) -> Optional[str]:
    """The branch (mill) address under the company name, as on the sample pass;
    the company's registered address when the branch has none. The PIN is added
    only when the address text does not already carry it."""
    company = company or {}
    parts = [
        str(company.get(k)).strip() for k in ("branch_address1", "branch_address2")
        if company.get(k) is not None and str(company.get(k)).strip()
    ]
    if not parts:
        return company_address(company)
    addr = ", ".join(parts)
    pin = company.get("branch_zipcode")
    if pin not in (None, "", 0, "0") and str(pin).strip() not in addr:
        addr = f"{addr} - {pin}"
    return addr


def letter_of(p: dict, company: Optional[dict]) -> dict:
    """The joining pass letter (EJM sample wording) from a stored pass row. The
    blanks of the sample are filled from the frozen figures; a figure with no data
    stays a blank line to fill by hand."""
    eff = _num(p.get("fn_efficiency"))
    eff_txt = f"{eff:.2f}%" if eff is not None else BLANK
    present = p.get("fn_days_present")
    working = p.get("fn_working_days")
    present_txt = str(int(present)) if present is not None else BLANK
    working_txt = str(int(working)) if working is not None else BLANK
    company = company or {}
    return {
        "company_name": (company.get("co_name") or "").strip() or None,
        "company_address": letterhead_address(company),
        "date": fmt_letter_date(p.get("join_date")),
        "eb_no": p.get("emp_code"),
        "name": p.get("emp_name"),
        "department": p.get("dept_desc"),
        "title": "JOINING PASS FOR DEPARTMENT",
        "paragraphs": [
            (
                f"You have been absenting from your duty from {fmt_letter_date(p.get('absent_from'))}. "
                f"Your efficiency during the last fortnight of your working is {eff_txt}. "
                f"Your record for number of days presence in the last fortnight of your working is "
                f"{present_txt} out of {working_txt} working days. You are advised to improve your "
                "production / productivity / attendance record. Considering your written appeal, management "
                "is providing you an opportunity and allowing you to join the duty w.e.f. "
                f"{fmt_letter_date(p.get('join_date'))}."
            ),
            (
                "You are hereby warned not to be absent so long without information to this office in future, "
                "failing which disciplinary action will be taken against you. Please consider this as the final "
                "chance and no more chances will be provided to you."
            ),
        ],
        "signature_label": "Signature / L. thumb Impression",
        "authorised_label": f"Authorised by {p.get('authority') or AUTHORITIES[0]}",
        "manager_label": "Personnel Manager",
        "issued_on": fmt_letter_date(p.get("issue_date")),
        "pass_no": p.get("pass_no"),
    }


def preview_row(a: dict, issue_date: date) -> dict:
    """A not-yet-issued pass row from an assessment, for the letter preview."""
    return {
        **a,
        "dept_desc": a.get("sub_dept_desc") or a.get("dept_desc"),
        "issue_date": issue_date,
        "pass_no": None,
    }


def pass_out(m: dict) -> dict:
    """API shape of a stored pass row (dates ISO, efficiency float)."""
    out = dict(m)
    jd = as_date(out.get("join_date"))
    # Absent To (reference screen) = the day before the joining day.
    out["absent_to"] = (jd - timedelta(days=1)).isoformat() if jd is not None and out.get("absent_from") else None
    for k in ("last_seen_date", "absent_from", "join_date", "issue_date", "fn_from", "fn_to"):
        out[k] = iso(out.get(k))
    for k in ("cancelled_date_time", "updated_date_time"):
        v = out.get(k)
        out[k] = v.isoformat() if isinstance(v, (date, datetime)) else v
    out["fn_efficiency"] = _num(out.get("fn_efficiency"))
    return out


# =============================================================================
# Access, rule, letterhead
# =============================================================================


def _user_id(token_data: dict):
    return (token_data or {}).get("user_id")


def menu_id(db: Session) -> Optional[int]:
    key = (db_name(db), "jp_menu_id")
    cached = _MENU_ID_CACHE.get(key)
    if cached is not None:
        return cached or None
    row = db.execute(
        text(
            "SELECT MIN(menu_id) FROM menu_mst WHERE active = 1 AND TRIM(BOTH '/' FROM menu_path) = :menu_path"
        ),
        {"menu_path": MENU_PATH},
    ).first()
    mid = int(row[0]) if row and row[0] is not None else None
    if key[0]:
        _MENU_ID_CACHE.set(key, mid or 0)
    return mid


def access_level(db: Session, user_id, branch_id) -> int:
    """Highest access_type_id the user holds on the Joining Pass menu for the
    branch (1 view .. 4 edit; 0 = no grant / no menu row)."""
    if not user_id or branch_id is None:
        return 0
    mid = menu_id(db)
    if mid is None:
        return 0
    row = db.execute(
        get_user_edit_access(), {"user_id": int(user_id), "branch_id": int(branch_id), "menu_id": mid}
    ).first()
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def require_access(db: Session, token_data: dict, co_id: int, branch_id: int, level: int) -> int:
    verify_branch(db, co_id, branch_id)
    have = access_level(db, _user_id(token_data), branch_id)
    if have < level:
        raise HTTPException(
            status_code=403,
            detail=f"You need {_LEVEL_NAMES.get(level, str(level))} access to Joining Pass for this branch",
        )
    return have


def load_rule(db: Session, co_id: int) -> dict:
    r = db.get(HrmsJoiningPassRule, int(co_id))
    if r is None:
        return dict(RULE_DEFAULTS)
    return {k: int(getattr(r, k)) for k in RULE_DEFAULTS}


def load_company(db: Session, branch_id: int) -> dict:
    row = db.execute(get_company_letterhead_query(), {"branch_id": int(branch_id)}).first()
    return dict(row._mapping) if row is not None else {}


def _has_non_working_days(db: Session) -> bool:
    """tbl_non_working_days exists on sls but not on every tenant (dev3)."""
    key = (db_name(db), "jp_nwd_table")
    cached = _MENU_ID_CACHE.get(key)
    if cached is not None:
        return bool(cached)
    found = bool(db.execute(get_table_exists_query(), {"table_name": "tbl_non_working_days"}).scalar())
    if key[0]:
        _MENU_ID_CACHE.set(key, 1 if found else 0)
    return found


def _off_days(db: Session, co_id: int, branch_id: int, d_from: date, d_to: date) -> set:
    if d_from > d_to:
        return set()
    rows = db.execute(
        get_off_days_query(_has_non_working_days(db)),
        {"co_id": co_id, "branch_id": branch_id, "d_from": d_from, "d_to": d_to},
    ).fetchall()
    return {as_date(r[0]) for r in rows if r[0] is not None}


def _efficiency(db: Session, co_id: int, branch_id: int, eb_id: int, snap: dict, fn_from: date, fn_to: date) -> dict:
    """Average efficiency over the fortnight through the Low Producer fetch; None
    (with the reason) for a worker with no production basis."""
    dept = perf_dept_of(snap.get("designation"), snap.get("dept_desc"), snap.get("sub_dept_desc"))
    if dept is None:
        return {
            "fn_efficiency": None,
            "fn_eff_dept": None,
            "eff_note": "No production basis (efficiency exists only for weavers, spinners and winders)",
        }
    key = (db_name(db), "jp_eff", int(eb_id), int(branch_id), fn_from.isoformat(), fn_to.isoformat())
    cached = _PERF_CACHE.get(key)
    if cached is not None:
        return cached
    d_from, d_to = perf_window(fn_from, fn_to, dept)
    rows = fetch_dept_rows(db, dept, int(co_id), int(branch_id), d_from, d_to)
    summary = build_person_summary(build_person_rows(rows, int(eb_id)))
    eff = summary.get("avg_eff")
    out = {
        "fn_efficiency": round(float(eff), 2) if eff is not None else None,
        "fn_eff_dept": dept,
        "eff_note": None if eff is not None else f"No {dept.lower()} production recorded in the fortnight",
    }
    if key[0]:
        _PERF_CACHE.set(key, out)
    return out


def assess(db: Session, co_id: int, branch_id: int, eb_id: int, join_date: date, rule: dict) -> dict:
    """Everything /check shows and /issue stores for one worker joining on
    join_date (see the module docstring)."""
    snap_row = db.execute(get_worker_snapshot_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id)}).first()
    if not snap_row:
        raise HTTPException(status_code=404, detail="Worker not found in this branch")
    snap = dict(snap_row._mapping)
    status = snap.get("emp_status")
    separated = status is not None and int(status) in RESIGN_DETAIL_STATUSES
    as_on = join_date - timedelta(days=1)
    row = db.execute(
        get_last_seen_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "as_on": as_on}
    ).first()
    last_seen = as_date(row[0]) if row else None
    att = db.execute(
        get_last_attendance_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "as_on": as_on}
    ).first()
    last_att = as_date(att._mapping["attendance_date"]) if att is not None else None

    out = {
        "eb_id": int(eb_id),
        "emp_code": snap.get("emp_code"),
        "emp_name": snap.get("emp_name"),
        "dept_id": int(snap["dept_id"]) if snap.get("dept_id") is not None else None,
        "dept_desc": snap.get("dept_desc"),
        "sub_dept_desc": snap.get("sub_dept_desc"),
        "designation": snap.get("designation"),
        "category": snap.get("category"),
        "separated": separated,
        "join_date": join_date.isoformat(),
        "min_absent_days": int(rule["min_absent_days"]),
        "last_seen_date": iso(last_seen),
        "last_attendance_date": iso(last_att),
        "last_spell_code": att._mapping.get("spell_code") if att is not None else None,
        "absent_from": None,
        "absent_working_days": 0,
        "absent_calendar_days": 0,
        "pass_required": False,
        "existing_pass": None,
        "fn_from": None,
        "fn_to": None,
        "fn_days_present": None,
        "fn_working_days": None,
        "fn_efficiency": None,
        "fn_eff_dept": None,
        "eff_note": None,
        "la_case": None,
    }
    if last_seen is None:
        out["note"] = "No attendance or approved leave found before this date in the branch"
        return out

    fn_from = fn_to = None
    if last_att is not None:
        fn_from, fn_to = fortnight_window(last_att, int(rule["fortnight_days"]))
    lo = min(d for d in (last_seen, fn_from) if d is not None)
    off = _off_days(db, int(co_id), int(branch_id), lo, join_date)

    ab = absence_of(last_seen, join_date, off)
    out.update(
        {
            "absent_from": iso(ab["absent_from"]),
            "absent_working_days": ab["absent_working_days"],
            "absent_calendar_days": ab["absent_calendar_days"],
            "pass_required": pass_required(ab["absent_working_days"], rule["min_absent_days"]),
        }
    )
    ex = db.execute(
        get_pass_for_absence_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "last_seen_date": last_seen}
    ).first()
    if ex is not None:
        m = ex._mapping
        out["existing_pass"] = {
            "joining_pass_id": int(m["joining_pass_id"]),
            "pass_no": m["pass_no"],
            "join_date": iso(m["join_date"]),
            "issue_date": iso(m["issue_date"]),
        }
    case = db.execute(
        get_live_case_query(), {"co_id": int(co_id), "branch_id": int(branch_id), "eb_id": int(eb_id), "exclude_id": None}
    ).first()
    if case is not None:
        out["la_case"] = {"case_id": int(case._mapping["case_id"]), "case_no": case._mapping["case_no"]}

    if fn_from is not None:
        present = db.execute(
            get_presence_dates_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "d_from": fn_from, "d_to": fn_to}
        ).fetchall()
        out.update(
            {
                "fn_from": fn_from.isoformat(),
                "fn_to": fn_to.isoformat(),
                "fn_days_present": len({as_date(r[0]) for r in present}),
                "fn_working_days": len(working_days(fn_from, fn_to, off)),
            }
        )
        out.update(_efficiency(db, int(co_id), int(branch_id), int(eb_id), snap, fn_from, fn_to))
    return out


def _resolve_eb(db: Session, branch_id: int, eb_id: Optional[int], emp_code: Optional[str]) -> int:
    if eb_id is not None:
        return int(eb_id)
    if not emp_code:
        raise HTTPException(status_code=400, detail="eb_id or emp_code is required")
    row = db.execute(get_eb_by_code_query(), {"branch_id": int(branch_id), "emp_code": emp_code}).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No worker with EB No {emp_code} in this branch")
    return int(row[0])


def _load_pass(db: Session, co_id: int, pass_id: int) -> dict:
    row = db.execute(get_pass_query(), {"co_id": int(co_id), "joining_pass_id": int(pass_id)}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Joining pass not found")
    return dict(row._mapping)


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/setup")
def jp_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Rule, departments of the branch, letterhead, today (IST) and the caller's
    access level on the branch."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    try:
        level = require_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        depts = [
            {"dept_id": int(m["dept_id"]), "dept_desc": m.get("dept_desc")}
            for m in (r._mapping for r in db.execute(get_branch_depts_query(), {"branch_id": branch_id}).fetchall())
        ]
        company = load_company(db, branch_id)
        spells = [
            {"spell_id": int(m["spell_id"]), "spell_code": m.get("spell_code"), "shift_name": m.get("shift_name")}
            for m in (r._mapping for r in db.execute(get_branch_spells_query(), {"branch_id": branch_id}).fetchall())
        ]
        return {
            "data": {
                "rule": load_rule(db, co_id),
                "spells": spells,
                "authorities": list(AUTHORITIES),
                "rule_limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in RULE_LIMITS.items()},
                "depts": depts,
                "letterhead": {"co_name": company.get("co_name"), "address": letterhead_address(company)},
                "today": today_ist().isoformat(),
                "access_level": level,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("joining pass setup failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check")
def jp_check(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The attendance criterion for one worker (eb_id or emp_code) joining on
    join_date (default today): absent working days, pass required, the pass
    already issued for this absence and the letter figures."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    eb_id = opt_int(q, "eb_id")
    emp_code = (q.get("emp_code") or "").strip() or None
    join_date = parse_date(q.get("join_date"), "join_date")
    try:
        require_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        join_date = join_date or today_ist()
        eb = _resolve_eb(db, branch_id, eb_id, emp_code)
        a = assess(db, co_id, branch_id, eb, join_date, load_rule(db, co_id))
        a["letter"] = letter_of(preview_row(a, today_ist()), load_company(db, branch_id))
        return {"data": a}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("joining pass check failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/absentees")
def jp_absentees(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Workers who would need a pass if they came on as_on (default today):
    absent more than the threshold in working days, last seen within the rule's
    look-back. Rows carry the pass already issued for the absence, if any."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    as_on = parse_date(q.get("as_on"), "as_on")
    dept_id = opt_int(q, "dept_id")
    try:
        require_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        as_on = as_on or today_ist()
        rule = load_rule(db, co_id)
        n = int(rule["min_absent_days"])
        from_date = as_on - timedelta(days=int(rule["lookback_days"]))
        rows = db.execute(
            get_absentees_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "from_date": from_date,
                "before_date": as_on - timedelta(days=n + 1),
                "as_on": as_on - timedelta(days=1),
                "exclude_regex": lac.exclude_prefix_regex(lac.load_rule(db, co_id)["exclude_prefixes"]),
                "dept_id": dept_id,
            },
        ).fetchall()
        off = _off_days(db, co_id, branch_id, from_date, as_on)
        out = []
        for r in rows:
            m = r._mapping
            last_seen = as_date(m["last_seen"])
            ab = absence_of(last_seen, as_on, off)
            if not pass_required(ab["absent_working_days"], n):
                continue
            out.append(
                {
                    "eb_id": int(m["eb_id"]),
                    "emp_code": m.get("emp_code"),
                    "emp_name": m.get("emp_name"),
                    "dept_id": int(m["dept_id"]) if m.get("dept_id") is not None else None,
                    "dept_desc": m.get("dept_desc"),
                    "designation": m.get("designation"),
                    "category": m.get("category"),
                    "last_seen_date": iso(last_seen),
                    "absent_from": iso(ab["absent_from"]),
                    "absent_working_days": ab["absent_working_days"],
                    "absent_calendar_days": ab["absent_calendar_days"],
                    "joining_pass_id": int(m["joining_pass_id"]) if m.get("joining_pass_id") is not None else None,
                    "pass_no": m.get("pass_no"),
                    "pass_join_date": iso(m.get("pass_join_date")),
                }
            )
        return {"data": {"as_on": as_on.isoformat(), "min_absent_days": n, "rows": out}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("joining pass absentees failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/issue")
def jp_issue(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Issue the pass: the figures are computed again on the server and frozen
    on the row (the letter text is never typed by hand). 409 when an active pass
    already covers this absence (reprint it instead)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = req_int(body, "branch_id")
    eb_id = req_int(body, "eb_id")
    join_date = parse_date(body.get("join_date"), "join_date", required=True)
    issue_date = parse_date(body.get("issue_date"), "issue_date")
    remarks = opt_text(body, "remarks", MAX_REMARKS)
    spell_id = opt_int(body, "spell_id")
    authority = opt_text(body, "authority", 50) or AUTHORITIES[0]
    if authority not in AUTHORITIES:
        raise HTTPException(status_code=400, detail=f"Invalid authority: {authority}")
    try:
        require_access(db, token_data, co_id, branch_id, LEVEL_CREATE)
        spell_code = None
        if spell_id is not None:
            spells = {
                int(r._mapping["spell_id"]): r._mapping["spell_code"]
                for r in db.execute(get_branch_spells_query(), {"branch_id": branch_id}).fetchall()
            }
            if spell_id not in spells:
                raise HTTPException(status_code=400, detail="spell_id is not a spell of this branch")
            spell_code = spells[spell_id]
        today = today_ist()
        issue_date = issue_date or today
        if issue_date > today:
            raise HTTPException(status_code=400, detail="issue_date cannot be in the future")
        if join_date > today + timedelta(days=MAX_JOIN_AHEAD_DAYS):
            raise HTTPException(status_code=400, detail=f"join_date cannot be more than {MAX_JOIN_AHEAD_DAYS} days ahead")
        a = assess(db, co_id, branch_id, eb_id, join_date, load_rule(db, co_id))
        if a["separated"]:
            raise HTTPException(status_code=400, detail="This worker has already left the roll (separated in HRMS)")
        if a["last_seen_date"] is None or a["absent_working_days"] < 1:
            raise HTTPException(status_code=400, detail="The worker was not absent on any working day before this date")
        if a["existing_pass"] is not None:
            ex = a["existing_pass"]
            raise HTTPException(
                status_code=409,
                detail=f"Pass {ex['pass_no']} was already issued for this absence on {fmt_letter_date(ex['issue_date'])} — reprint it",
            )
        user_id = _user_id(token_data)
        p = HrmsJoiningPass(
            co_id=co_id,
            branch_id=branch_id,
            eb_id=eb_id,
            emp_code=a["emp_code"],
            emp_name=(a["emp_name"] or "")[:150] or None,
            dept_id=a["dept_id"],
            # the letter's "Department": the sub-department name (BEAMING, WEAVING ...)
            dept_desc=(a["sub_dept_desc"] or a["dept_desc"] or "")[:100] or None,
            designation=(a["designation"] or "")[:100] or None,
            last_seen_date=as_date(a["last_seen_date"]),
            absent_from=as_date(a["absent_from"]),
            absent_working_days=a["absent_working_days"],
            absent_calendar_days=a["absent_calendar_days"],
            join_date=join_date,
            issue_date=issue_date,
            spell_id=spell_id,
            spell_code=spell_code,
            authority=authority,
            fn_from=as_date(a["fn_from"]),
            fn_to=as_date(a["fn_to"]),
            fn_days_present=a["fn_days_present"],
            fn_working_days=a["fn_working_days"],
            fn_efficiency=Decimal(str(a["fn_efficiency"])) if a["fn_efficiency"] is not None else None,
            fn_eff_dept=a["fn_eff_dept"],
            la_case_id=a["la_case"]["case_id"] if a["la_case"] else None,
            remarks=remarks,
            active=1,
            issued_by=user_id,
            updated_by=user_id,
        )
        db.add(p)
        db.flush()
        p.pass_no = make_pass_no(branch_id, issue_date, p.joining_pass_id)
        if a["la_case"]:
            lac.add_event(
                db,
                a["la_case"]["case_id"],
                "SYSTEM",
                f"Joining pass {p.pass_no} issued on {fmt_letter_date(issue_date)}: allowed to join w.e.f. "
                f"{fmt_letter_date(join_date)}",
                user_id,
                event_date=issue_date,
            )
        db.commit()
        row = _load_pass(db, co_id, p.joining_pass_id)
        return {"data": {**pass_out(row), "letter": letter_of(row, load_company(db, branch_id))}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("joining pass issue failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def jp_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Register: passes of the branch by issue date (default the last 30 days)."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    date_to = parse_date(q.get("date_to"), "date_to")
    date_from = parse_date(q.get("date_from"), "date_from")
    search = (q.get("search") or "").strip()[:100] or None
    include_cancelled = 1 if (q.get("include_cancelled") or "").lower() in ("1", "true", "yes") else 0
    try:
        require_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        date_to = date_to or today_ist()
        date_from = date_from or (date_to - timedelta(days=30))
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from cannot be after date_to")
        rows = db.execute(
            get_pass_list_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "date_from": date_from,
                "date_to": date_to,
                "search": f"%{search}%" if search else None,
                "include_cancelled": include_cancelled,
            },
        ).fetchall()
        return {"data": [pass_out(dict(r._mapping)) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("joining pass list failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get")
def jp_get(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One pass with its letter (reprint: the stored figures, never recomputed)."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    pass_id = req_int(q, "joining_pass_id")
    try:
        row = _load_pass(db, co_id, pass_id)
        require_access(db, token_data, co_id, int(row["branch_id"]), LEVEL_VIEW)
        return {"data": {**pass_out(row), "letter": letter_of(row, load_company(db, int(row["branch_id"])))}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("joining pass get failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel")
def jp_cancel(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Cancel a pass issued by mistake (edit access, reason required). The
    absence then needs a new pass."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    pass_id = req_int(body, "joining_pass_id")
    reason = opt_text(body, "reason", MAX_CANCEL_REASON)
    try:
        if not reason:
            raise HTTPException(status_code=400, detail="reason is required")
        p = db.get(HrmsJoiningPass, pass_id)
        if p is None or int(p.co_id) != co_id:
            raise HTTPException(status_code=404, detail="Joining pass not found")
        require_access(db, token_data, co_id, int(p.branch_id), LEVEL_EDIT)
        if int(p.active) != 1:
            raise HTTPException(status_code=400, detail="This pass is already cancelled")
        user_id = _user_id(token_data)
        p.active = 0
        p.cancel_reason = reason
        p.cancelled_by = user_id
        p.cancelled_date_time = datetime.now()
        p.updated_by = user_id
        if p.la_case_id:
            lac.add_event(db, int(p.la_case_id), "SYSTEM", f"Joining pass {p.pass_no} cancelled: {reason}", user_id)
        db.commit()
        return {"data": pass_out(_load_pass(db, co_id, pass_id))}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("joining pass cancel failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rule_save")
def jp_rule_save(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Save the company rule (edit access on the sent branch)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = req_int(body, "branch_id")
    fields = clean_rule(body)
    try:
        require_access(db, token_data, co_id, branch_id, LEVEL_EDIT)
        r = db.get(HrmsJoiningPassRule, co_id)
        if r is None:
            r = HrmsJoiningPassRule(co_id=co_id)
            db.add(r)
        for k, v in fields.items():
            setattr(r, k, v)
        r.updated_by = _user_id(token_data)
        db.commit()
        return {"data": load_rule(db, co_id)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("joining pass rule save failed")
        raise HTTPException(status_code=500, detail=str(e))
