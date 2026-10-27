"""Long Absenteeism follow-up — cases router, /api/longAbsenteeism (owner request 2026-10-11).

One tracked case per long-absent worker (eb_id) per branch: candidates from
attendance + approved leave, open / dismiss, verification, Excel-style remarks
and extra columns, park / resume / exit / reopen, the dated timeline (user
REMARK / FEEDBACK / REPLY + system entries), rejoin detection and the
last-30-days performance snapshot.

Letters, templates, rule, dispatch, union receipts, the struck-off approval and
the reports live in the second router (long_absenteeism_letters.py) on the same
prefix. Codes, defaults, parsing, access, loaders and shaping are shared from
long_absenteeism_common.py.

Spec: docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md
(sections 4.1-4.6, 4.12, 5, 6 "BE-1 router").
Pattern: src/juteProduction/low_performer_followup.py.

Plain ``def`` handlers (sync DB), ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, 400 validation / 403 access / 404 not found /
409 duplicate live case, 500 with ``logger.exception`` and a rollback.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.constants import RESIGN_DETAIL_STATUSES
from src.juteProduction import long_absenteeism_common as lac
from sqlalchemy import event
from src.juteProduction.long_absenteeism_common import (
    TtlCache,
    db_name,
    ESI_STL_RESULTS,
    EXIT_REASONS,
    LEAVE_LINK_COLUMNS,
    LEVEL_CREATE,
    LEVEL_EDIT,
    LEVEL_VIEW,
    MAX_EXTRA,
    MAX_NOTE,
    MAX_REMARKS,
    MAX_TEXT,
    REPLY_CHANNELS,
    SO_PENDING,
    STAGE_LABELS,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_PARKED,
    USER_EVENT_TYPES,
    USER_EXIT_REASONS,
    add_event,
    auto_verified_fields,
    build_leave_options,
    case_out,
    codes_payload,
    due_band,
    event_to_dict,
    exclude_prefix_regex,
    exit_options,
    fmt_dmy,
    json_object,
    letter_to_dict,
    leave_type_name_of,
    load_case,
    load_rule,
    on_leave_text,
    opt_bool,
    opt_int,
    opt_text,
    parse_date,
    plan_leave_refresh,
    req_code,
    req_int,
    require_access,
    require_branch_access,
    require_live,
    require_open,
    rollback,
    today_ist,
    verify_branch,
)
from src.juteProduction.long_absenteeism_models import (
    JuteProdAbsentCase,
    JuteProdAbsentEvent,
    JuteProdAbsentLetter,
)
from src.juteProduction.long_absenteeism_query import (
    EVENT_ATTACHMENT_MODULE,
    LETTER_ATTACHMENT_MODULE,
    LETTER_ATTACHMENT_TYPES,
    LETTER_COLUMNS,
    RECEIPT_ATTACHMENT_MODULE,
    RECEIPT_ATTACHMENT_TYPE,
    get_branch_depts_query,
    get_candidates_query,
    get_case_leave_query,
    get_case_letter_attachments_query,
    get_case_letters_query,
    get_case_list_query,
    get_case_receipts_query,
    get_events_query,
    get_last_attendance_query,
    get_last_seen_query,
    get_leave_type_query,
    get_leave_types_query,
    get_live_case_query,
    get_other_cases_query,
    get_pending_leave_query,
    get_rejoin_query,
    get_set_rejoined_query,
    get_worker_snapshot_query,
    lock_case_query,
)
from src.juteProduction.low_performer_followup import (
    MAX_DAYS_BY_DEPT,
    build_person_rows,
    build_person_summary,
    fetch_dept_rows,
)

logger = logging.getLogger(__name__)

# The candidates list scans the branch's attendance history (2-3 s on sls); it only
# changes with new attendance / leave (daily) or with our own writes (open, dismiss,
# exit, reopen), which invalidate it. 120 s keeps repeated watchlist loads instant.
_CANDIDATES_CACHE = TtlCache(120)


def invalidate_candidates(db: Session, co_id: int, branch_id: int) -> None:
    """Drop the cached candidates of the branch now AND again after this session's
    commit (a concurrent /candidates call between the two could otherwise re-cache
    the pre-write list)."""
    prefix = (db_name(db), "candidates", int(co_id), int(branch_id))
    _CANDIDATES_CACHE.invalidate(prefix)

    def _after_commit(session):
        _CANDIDATES_CACHE.invalidate(prefix)

    try:
        event.listen(db, "after_commit", _after_commit, once=True)
    except Exception:  # pragma: no cover - MagicMock sessions in tests
        pass


router = APIRouter()
long_absenteeism_router = router

LIVE_UNIQUE_KEY = "uq_jpac_live"
LIST_VIEWS = ("due", "open", "parked", "closed", "all")
# /list view -> the case-status filter of get_case_list_query (due = every live
# case, then rows without a band are dropped in Python).
_VIEW_STATUS_MODE = {"due": "live", "open": "open", "parked": "parked", "closed": "closed", "all": "all"}
DUE_BANDS = lac.DUE_BANDS   # watchlist bands, in the order due_band tests them

# Column widths of the snapshot (jute_prod_absent_case).
_SNAPSHOT_WIDTHS = {
    "emp_code": 20, "emp_name": 150, "dept_desc": 100, "sub_dept_desc": 100, "designation": 100,
    "category": 50, "father_name": 150, "address": 500, "mobile": 20, "esi_no": 30, "pf_no": 30,
    "last_machine": 50, "last_spell": 20,
}
_UPDATE_TEXT_FIELDS = {
    "remarks": MAX_REMARKS,
    "extra_1": MAX_EXTRA, "extra_2": MAX_EXTRA, "extra_3": MAX_EXTRA, "extra_4": MAX_EXTRA, "extra_5": MAX_EXTRA,
}
MAX_VERIFY_NOTE = 300
MAX_EXIT_REMARKS = 500
MAX_CONTACT = 100
MAX_SEARCH = 100
MAX_SNOOZE_DAYS = 365

# Performance: the 30 days up to the last working date (31 calendar days),
# capped to the Low Producer report's own window for the department.
PERF_LOOKBACK_DAYS = 30


# =============================================================================
# Pure helpers (unit-tested without a DB)
# =============================================================================


def _as_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _iso(v) -> Optional[str]:
    d = _as_date(v)
    return d.isoformat() if d is not None else None


def _cut(v, width: int) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:width] if s else None


def compose_address(m: dict) -> Optional[str]:
    """One-line correspondence address: line 1, line 2, city, state, PIN (blank
    parts skipped; a 0 PIN is blank)."""
    pin = m.get("pin_code")
    parts = [
        m.get("address_line_1"),
        m.get("address_line_2"),
        m.get("city_name"),
        m.get("state_name"),
        str(pin) if pin not in (None, "", 0, "0") else None,
    ]
    out = ", ".join(str(p).strip() for p in parts if p is not None and str(p).strip())
    return out[:_SNAPSHOT_WIDTHS["address"]] or None


def make_case_no(branch_id: int, on_date: date, case_id: int) -> str:
    """LA/<branch_id>/<yy>/<case_id, at least 4 digits>. The number comes from the
    row's own case_id (known after the INSERT is flushed), so two cases opened at
    the same moment can never get the same number."""
    return f"LA/{int(branch_id)}/{on_date.year % 100:02d}/{int(case_id):04d}"


# due_band / case_out live in long_absenteeism_common (one case shape for both
# routers); imported above and re-exported here for the tests.


def perf_dept_of(designation, dept_desc, sub_dept_desc) -> Optional[str]:
    """Low Producer department of a worker: designation weaver* -> WEAVING,
    spinner* -> SPINNING, department / sub-department containing WIND -> WINDING,
    else None (no production basis)."""
    desig = (designation or "").strip().lower()
    if desig.startswith("weaver"):
        return "WEAVING"
    if desig.startswith("spinner"):
        return "SPINNING"
    if "WIND" in f"{dept_desc or ''} {sub_dept_desc or ''}".upper():
        return "WINDING"
    return None


def perf_window(last_working_date: date, dept: str) -> tuple:
    """(d_from, d_to): the 30 days before the last working date up to it, capped
    to the department's Low Producer window (weaving / winding 31 days)."""
    max_days = int(MAX_DAYS_BY_DEPT[dept])
    back = min(PERF_LOOKBACK_DAYS, max_days - 1)
    return last_working_date - timedelta(days=back), last_working_date


def _orm(model, m: dict):
    """A transient (never added) ORM instance from a raw row, so the shared
    *_to_dict shapers can be reused on SQL results."""
    cols = {col.name for col in model.__table__.columns}
    return model(**{k: v for k, v in m.items() if k in cols})


def _yes(v) -> str:
    return "yes" if v else "no"


def _int_or_none(v) -> Optional[int]:
    return int(v) if v is not None and v != "" else None


# =============================================================================
# DB helpers
# =============================================================================


def _duplicate_message(found) -> str:
    if found is None:
        return "This worker already has a live (open or parked) case in this branch"
    case_id, case_no = found
    ref = case_no or f"#{case_id}"
    return f"This worker already has a live (open or parked) case ({ref}) in this branch"


def _find_live_case(db: Session, key: dict, exclude_id=None):
    row = db.execute(
        get_live_case_query(),
        {
            "co_id": int(key["co_id"]),
            "branch_id": int(key["branch_id"]),
            "eb_id": int(key["eb_id"]),
            "exclude_id": exclude_id,
        },
    ).first()
    if not row:
        return None
    m = row._mapping
    return int(m["case_id"]), m.get("case_no")


def _guard_live(db: Session, key: dict, exclude_id=None) -> None:
    found = _find_live_case(db, key, exclude_id)
    if found is not None:
        raise HTTPException(status_code=409, detail=_duplicate_message(found))


def _is_live_unique_violation(exc: IntegrityError) -> bool:
    """The DB's one-live-case key (uq_jpac_live) refused the write — a concurrent
    request opened / reopened a case of the same worker + branch first."""
    orig = getattr(exc, "orig", None)
    code = orig.args[0] if orig is not None and getattr(orig, "args", None) else None
    return code == 1062 or LIVE_UNIQUE_KEY in str(exc)


def _live_conflict(db: Session, key: dict, exclude_id=None) -> HTTPException:
    """409 for a write the unique key refused, with the guard's message (the
    winning case's number when it can be read back)."""
    rollback(db)
    try:
        found = _find_live_case(db, key, exclude_id)
    except Exception:  # pragma: no cover - the message is still right without the number
        found = None
    return HTTPException(status_code=409, detail=_duplicate_message(found))


def _commit_live(db: Session, key: dict, exclude_id=None) -> None:
    """Commit a write that creates / revives a live case, mapping the unique key's
    refusal to 409."""
    try:
        db.commit()
    except IntegrityError as ie:
        if _is_live_unique_violation(ie):
            raise _live_conflict(db, key, exclude_id)
        raise


def _snapshot(db: Session, eb_id: int, branch_id: int) -> dict:
    row = db.execute(get_worker_snapshot_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id)}).first()
    if not row:
        raise HTTPException(status_code=400, detail="Worker not found in this branch")
    m = dict(row._mapping)
    status = m.get("emp_status")
    if status is not None and int(status) in RESIGN_DETAIL_STATUSES:
        raise HTTPException(status_code=400, detail="This worker has already left the roll (separated in HRMS)")
    return m


def _snapshot_columns(m: dict, full: bool = True) -> dict:
    """Case snapshot columns from the worker row (``full=False`` = the minimal set
    a dismissal keeps: emp_code, name, dept, designation)."""
    out = {
        "emp_code": _cut(m.get("emp_code"), _SNAPSHOT_WIDTHS["emp_code"]),
        "emp_name": _cut(m.get("emp_name"), _SNAPSHOT_WIDTHS["emp_name"]),
        "dept_id": int(m["dept_id"]) if m.get("dept_id") is not None else None,
        "dept_desc": _cut(m.get("dept_desc"), _SNAPSHOT_WIDTHS["dept_desc"]),
        "designation": _cut(m.get("designation"), _SNAPSHOT_WIDTHS["designation"]),
    }
    if full:
        out.update(
            {
                "sub_dept_desc": _cut(m.get("sub_dept_desc"), _SNAPSHOT_WIDTHS["sub_dept_desc"]),
                "category": _cut(m.get("category"), _SNAPSHOT_WIDTHS["category"]),
                "father_name": _cut(m.get("father_name"), _SNAPSHOT_WIDTHS["father_name"]),
                "address": compose_address(m),
                "mobile": _cut(m.get("mobile"), _SNAPSHOT_WIDTHS["mobile"]),
                "date_of_join": _as_date(m.get("date_of_join")),
                "esi_no": _cut(m.get("esi_no"), _SNAPSHOT_WIDTHS["esi_no"]),
                "pf_no": _cut(m.get("pf_no"), _SNAPSHOT_WIDTHS["pf_no"]),
            }
        )
    return out


def _last_seen(db: Session, eb_id: int, branch_id: int, as_on: date) -> Optional[date]:
    row = db.execute(
        get_last_seen_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "as_on": as_on}
    ).first()
    return _as_date(row[0]) if row else None


def _resolve_lwd(db: Session, given: Optional[date], eb_id: int, branch_id: int, today: date) -> date:
    lwd = given or _last_seen(db, eb_id, branch_id, today)
    if lwd is None:
        raise HTTPException(
            status_code=400,
            detail="No attendance or approved leave found for this worker — enter the last working date",
        )
    return lwd


def _number_case(db: Session, c: JuteProdAbsentCase, on_date: date) -> None:
    """Give a case its number from its own case_id (flushing first so a new row
    has one). Collision-free: no count / MAX + 1 race between two requests."""
    if c.case_id is None:
        db.flush()
    c.case_no = make_case_no(int(c.branch_id), on_date, int(c.case_id))


def _last_attendance_columns(db: Session, eb_id: int, branch_id: int, today: date) -> dict:
    """last_machine / last_spell of the case snapshot: the worker's latest active,
    non-rejected attendance row in the branch (machine of its latest man-machine
    mark, if any)."""
    last = db.execute(
        get_last_attendance_query(), {"eb_id": int(eb_id), "branch_id": int(branch_id), "as_on": today}
    ).first()
    lm = last._mapping if last is not None else {}
    return {
        "last_machine": _cut(lm.get("machine_name"), _SNAPSHOT_WIDTHS["last_machine"]),
        "last_spell": _cut(lm.get("spell_code"), _SNAPSHOT_WIDTHS["last_spell"]),
    }


def _lock_case(db: Session, case_id: int) -> None:
    """SELECT ... FOR UPDATE on the case: the first statement of every lifecycle
    change, so it serialises with the letters router's writes on the same row."""
    db.execute(lock_case_query(), {"case_id": int(case_id)})


def refresh_rejoins_branch(db: Session, co_id: int, branch_id: int) -> int:
    """4.6 for every live case of the branch: store the first attendance after the
    last working date when it changed (silently: no event). Commits only when
    something changed; returns the number of cases updated."""
    rows = db.execute(get_rejoin_query(), {"co_id": int(co_id), "branch_id": int(branch_id), "case_id": None}).fetchall()
    changed = 0
    for r in rows:
        m = r._mapping
        new = _as_date(m["first_seen"])
        if new != _as_date(m["rejoined_seen_date"]):
            db.execute(get_set_rejoined_query(), {"case_id": int(m["case_id"]), "rejoined_seen_date": new})
            changed += 1
    if changed:
        db.commit()
    return changed


def refresh_rejoin_case(db: Session, c: JuteProdAbsentCase) -> bool:
    """4.6 for one (live) case on the ORM row; commits only when it changed."""
    if int(c.status) == STATUS_CLOSED:
        return False
    row = db.execute(
        get_rejoin_query(), {"co_id": int(c.co_id), "branch_id": int(c.branch_id), "case_id": int(c.case_id)}
    ).first()
    if not row:
        return False
    new = _as_date(row._mapping["first_seen"])
    if new == _as_date(c.rejoined_seen_date):
        return False
    c.rejoined_seen_date = new
    db.commit()
    return True


# Leave module link (TKT-000223, owner ruling 12.10.2026). The rules are the pure
# plan_leave_refresh in long_absenteeism_common; these apply them on ORM rows
# (so add_event can log the SYSTEM entries) right after the rejoin refresh.
_LEAVE_STATE_FIELDS = ("status", "status_id", "stage_code", "next_due_date", "rejoined_seen_date", "leave_parked")


def _leave_state_of_row(m) -> dict:
    """plan_leave_refresh's case state from a get_case_leave_query row."""
    out = {k: m.get(k) for k in _LEAVE_STATE_FIELDS}
    out.update(
        leave_transaction_id=m.get("stored_ltid"),
        leave_type_id=m.get("stored_type_id"),
        leave_type_name=m.get("stored_type_name"),
        leave_from=m.get("stored_from"),
        leave_to=m.get("stored_to"),
    )
    return out


def _module_leave_of_row(m) -> Optional[dict]:
    """The approved leave of a get_case_leave_query row (None when none)."""
    if m.get("leave_transaction_id") is None:
        return None
    return {
        "leave_transaction_id": m.get("leave_transaction_id"),
        "leave_type_id": m.get("leave_type_id"),
        "leave_type_name": m.get("leave_type_name"),
        "leave_from": m.get("leave_from_date"),
        "leave_to": m.get("leave_to_date"),
    }


def _apply_leave_plan(db: Session, c: JuteProdAbsentCase, upd: dict, events: list, today: date) -> None:
    """Write a plan on the case (no updated_by change) + its SYSTEM entries (no user)."""
    for k, v in upd.items():
        setattr(c, k, v)
    for text_value in events:
        add_event(db, c.case_id, "SYSTEM", text_value[:MAX_TEXT], None, event_date=today)


def refresh_leaves_branch(db: Session, co_id: int, branch_id: int, today: Optional[date] = None) -> int:
    """Leave refresh for every live case of the branch (after the rejoin refresh):
    promotes cases still at DETECTED, stores the approved leave of the leave
    module, parks / resumes on it (plan_leave_refresh). Commits only when
    something changed; returns the number of cases changed."""
    today = today or today_ist()
    rows = db.execute(
        get_case_leave_query(),
        {"co_id": int(co_id), "branch_id": int(branch_id), "case_id": None, "today": today},
    ).fetchall()
    changed = 0
    for r in rows:
        m = r._mapping
        upd, events = plan_leave_refresh(_leave_state_of_row(m), _module_leave_of_row(m), today)
        if not upd and not events:
            continue
        c = db.get(JuteProdAbsentCase, int(m["case_id"]))
        if c is None:
            continue
        _apply_leave_plan(db, c, upd, events, today)
        changed += 1
    if changed:
        db.commit()
    return changed


def refresh_leave_case(db: Session, c: JuteProdAbsentCase, today: Optional[date] = None) -> bool:
    """The leave refresh for one (live) case on the ORM row; commits only when it changed."""
    if int(c.status) == STATUS_CLOSED:
        return False
    today = today or today_ist()
    row = db.execute(
        get_case_leave_query(),
        {"co_id": int(c.co_id), "branch_id": int(c.branch_id), "case_id": int(c.case_id), "today": today},
    ).first()
    if not row:
        return False
    state = {k: getattr(c, k) for k in (*_LEAVE_STATE_FIELDS, *LEAVE_LINK_COLUMNS)}
    upd, events = plan_leave_refresh(state, _module_leave_of_row(row._mapping), today)
    if not upd and not events:
        return False
    _apply_leave_plan(db, c, upd, events, today)
    db.commit()
    return True


def _pending_leave_since(db: Session, eb_id: int, today: date) -> Optional[str]:
    """The pending-leave hint of a worker (ISO date or None; never stored)."""
    row = db.execute(get_pending_leave_query(), {"eb_id": int(eb_id), "today": today}).first()
    return _iso(row._mapping.get("pending_leave_since")) if row is not None else None


def _active_leave_type(db: Session, co_id: int, leave_type_id: int) -> dict:
    """An active leave type of the company (hrms_leave_types_mst), else 400."""
    row = db.execute(get_leave_type_query(), {"co_id": int(co_id), "leave_type_id": int(leave_type_id)}).first()
    if not row:
        raise HTTPException(status_code=400, detail="leave_type_id is not an active leave type of this company")
    return dict(row._mapping)


def _shape_event(m: dict) -> dict:
    d = event_to_dict(_orm(JuteProdAbsentEvent, m))
    d["attachment_id"] = m.get("attachment_id")
    d["attachment_filename"] = m.get("attachment_filename")
    return d


def _events_out(db: Session, co_id: int, case_id: int, event_id=None) -> list:
    rows = db.execute(
        get_events_query(),
        {"case_id": int(case_id), "co_id": int(co_id), "module": EVENT_ATTACHMENT_MODULE, "event_id": event_id},
    ).fetchall()
    return [_shape_event(dict(r._mapping)) for r in rows]


def _event_one(db: Session, co_id: int, case_id: int, event_id: int) -> dict:
    rows = _events_out(db, co_id, case_id, event_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    return rows[0]


def _load_event(db: Session, co_id: int, event_id: int) -> tuple:
    ev = db.get(JuteProdAbsentEvent, int(event_id))
    if ev is None or int(ev.active or 0) != 1:
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    c = load_case(db, co_id, int(ev.case_id))
    return ev, c


def _ts(v):
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else _iso(v)


def _letters_out(db: Session, co_id: int, case_id: int) -> list:
    """Active letters (letter_to_dict) + receipts[] (each with its latest
    union_receipt attachment) + attachments {signed_copy, ad_card:
    {attachment_id, filename} | None} — the same shape as the letters router's
    /letters."""
    letters = [dict(r._mapping) for r in db.execute(get_case_letters_query(), {"case_id": int(case_id)}).fetchall()]
    if not letters:
        return []
    atts: dict = {}
    for r in db.execute(
        get_case_letter_attachments_query(),
        {
            "case_id": int(case_id),
            "co_id": int(co_id),
            "letter_module": LETTER_ATTACHMENT_MODULE,
            "receipt_module": RECEIPT_ATTACHMENT_MODULE,
        },
    ).fetchall():
        m = r._mapping
        atts[(m["module"], int(m["entity_id"]), m["file_type"])] = {
            "attachment_id": int(m["attachment_id"]),
            "filename": m.get("filename"),
        }
    receipts: dict = {}
    for r in db.execute(get_case_receipts_query(), {"case_id": int(case_id)}).fetchall():
        m = r._mapping
        rid = int(m["receipt_id"])
        receipts.setdefault(int(m["letter_id"]), []).append(
            {
                "receipt_id": rid,
                "letter_id": int(m["letter_id"]),
                "union_name": m.get("union_name"),
                "received_by": m.get("received_by"),
                "received_date": _iso(m.get("received_date")),
                "remarks": m.get("remarks"),
                "active": m.get("active"),
                "updated_by": m.get("updated_by"),
                "updated_date_time": _ts(m.get("updated_date_time")),
                "attachment": atts.get((RECEIPT_ATTACHMENT_MODULE, rid, RECEIPT_ATTACHMENT_TYPE)),
            }
        )
    out = []
    for m in letters:
        d = letter_to_dict(_orm(JuteProdAbsentLetter, m))
        lid = int(m["letter_id"])
        d["receipts"] = receipts.get(lid, [])
        d["attachments"] = {ft: atts.get((LETTER_ATTACHMENT_MODULE, lid, ft)) for ft in LETTER_ATTACHMENT_TYPES}
        out.append(d)
    return out


def _other_cases_out(db: Session, c: JuteProdAbsentCase) -> list:
    rows = db.execute(
        get_other_cases_query(),
        {"co_id": int(c.co_id), "branch_id": int(c.branch_id), "eb_id": int(c.eb_id), "case_id": int(c.case_id)},
    ).fetchall()
    out = []
    for r in rows:
        m = r._mapping
        out.append(
            {
                "case_id": int(m["case_id"]),
                "case_no": m.get("case_no"),
                "detected_date": _iso(m.get("detected_date")),
                "stage_code": m.get("stage_code"),
                "stage_label": STAGE_LABELS.get(m.get("stage_code"), m.get("stage_code")),
                "status": int(m["status"]) if m.get("status") is not None else None,
                "exit_reason": m.get("exit_reason"),
                "exit_reason_label": EXIT_REASONS.get(m.get("exit_reason") or "", m.get("exit_reason")),
                "exit_date": _iso(m.get("exit_date")),
                "struck_off_date": _iso(m.get("struck_off_date")),
            }
        )
    return out


def _approval_pending(c: JuteProdAbsentCase) -> bool:
    return c.status_id is not None and int(c.status_id) == SO_PENDING


def _require_no_pending_approval(c: JuteProdAbsentCase) -> None:
    if _approval_pending(c):
        raise HTTPException(status_code=400, detail="Struck-off approval is pending — reject it first")


def _user_id(token_data: dict):
    return (token_data or {}).get("user_id")


def _q(request: Request):
    return request.query_params


# =============================================================================
# Endpoints — setup, candidates, list, case page
# =============================================================================


@router.get("/setup")
def la_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Code lists + the branch's departments + the rule + today (IST) + the
    caller's access level on the branch + the company's leave types
    (leave_types, leave_options, exit_options — TKT-000223)."""
    q = _q(request)
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    try:
        verify_branch(db, co_id, branch_id)
        level = lac.access_level(db, _user_id(token_data), branch_id)
        if level < LEVEL_VIEW:
            raise HTTPException(status_code=403, detail="You need view access to Long Absenteeism for this branch")
        depts = [
            {"dept_id": int(m["dept_id"]), "dept_desc": m.get("dept_desc")}
            for m in (r._mapping for r in db.execute(get_branch_depts_query(), {"branch_id": branch_id}).fetchall())
        ]
        leave_types = [
            {
                "leave_type_id": int(m["leave_type_id"]),
                "leave_type_code": m.get("leave_type_code"),
                "leave_type_description": m.get("leave_type_description"),
                "payable": m.get("payable"),
            }
            for m in (r._mapping for r in db.execute(get_leave_types_query(), {"co_id": co_id}).fetchall())
            if m.get("leave_type_id") is not None
        ]
        leave_options = build_leave_options(leave_types)
        return {
            "data": {
                **codes_payload(),
                "depts": depts,
                "rule": load_rule(db, co_id),
                "today": today_ist().isoformat(),
                "access_level": level,
                "leave_types": leave_types,
                "leave_options": leave_options,
                "exit_options": exit_options(leave_options),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("long absenteeism setup failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candidates")
def la_candidates(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Workers absent >= detect_days (seen within lookback_days; 0 = no limit) with no live case,
    no snoozed dismissal and no running ESI / STL style exit (spec 4.1)."""
    q = _q(request)
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    as_on = parse_date(q.get("as_on"), "as_on")
    dept_id = opt_int(q, "dept_id")
    try:
        require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        as_on = as_on or today_ist()
        rule = load_rule(db, co_id)
        lookback = int(rule["lookback_days"])
        params = {
            "co_id": co_id,
            "branch_id": branch_id,
            "from_date": as_on - timedelta(days=lookback) if lookback > 0 else None,   # 0 = no limit
            "as_on": as_on,
            "detect_days": int(rule["detect_days"]),
            "exclude_regex": exclude_prefix_regex(rule["exclude_prefixes"]),
            "dept_id": dept_id,
        }
        cache_key = (db_name(db), "candidates", int(co_id), int(branch_id), as_on.isoformat(), dept_id,
                     params["from_date"], params["detect_days"], params["exclude_regex"])
        cached = _CANDIDATES_CACHE.get(cache_key)
        if cached is not None:
            return {"data": cached}
        rows = db.execute(get_candidates_query(), params).fetchall()
        out = []
        for r in rows:
            m = r._mapping
            out.append(
                {
                    "eb_id": int(m["eb_id"]),
                    "emp_code": m.get("emp_code"),
                    "emp_name": m.get("emp_name"),
                    "dept_id": int(m["dept_id"]) if m.get("dept_id") is not None else None,
                    "dept_desc": m.get("dept_desc"),
                    "sub_dept_desc": m.get("sub_dept_desc"),
                    "designation": m.get("designation"),
                    "category": m.get("category"),
                    "last_seen": _iso(m.get("last_seen")),
                    "absent_days": int(m["absent_days"]) if m.get("absent_days") is not None else None,
                    "closed_cases": int(m.get("closed_cases") or 0),
                    "leave_transaction_id": _int_or_none(m.get("leave_transaction_id")),
                    "leave_type_id": _int_or_none(m.get("leave_type_id")),
                    "leave_type_name": m.get("leave_type_name"),
                    "leave_from": _iso(m.get("leave_from")),
                    "leave_to": _iso(m.get("leave_to")),
                    "pending_leave_since": _iso(m.get("pending_leave_since")),
                }
            )
        _CANDIDATES_CACHE.set(cache_key, out)
        return {"data": out}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("long absenteeism candidates failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def la_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Cases of a branch: case_to_dict + per-stage letter dates + due_band +
    next stage + pending_leave_since. view=due keeps the live cases that have a
    band. The branch's rejoin dates (4.6) and leave links (TKT-000223) are
    refreshed first."""
    q = _q(request)
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    view = (q.get("view") or "due").strip().lower()
    if view not in LIST_VIEWS:
        raise HTTPException(status_code=400, detail="view must be one of " + ", ".join(LIST_VIEWS))
    dept_id = opt_int(q, "dept_id")
    stage_code = req_code(q, "stage_code", STAGE_LABELS) if (q.get("stage_code") or "").strip() else None
    search = (q.get("q") or "").strip()
    if len(search) > MAX_SEARCH:
        raise HTTPException(status_code=400, detail=f"q must be at most {MAX_SEARCH} characters")
    try:
        require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        today = today_ist()
        if view != "closed":     # closed cases are never re-checked (4.6 is for live ones)
            refresh_rejoins_branch(db, co_id, branch_id)
            refresh_leaves_branch(db, co_id, branch_id, today)
        rows = db.execute(
            get_case_list_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "status_mode": _VIEW_STATUS_MODE[view],
                "dept_id": dept_id,
                "stage_code": stage_code,
                "q": f"%{search}%" if search else None,
                "today": today,
            },
        ).fetchall()
        out = []
        for r in rows:
            m = dict(r._mapping)
            d = case_out(_orm(JuteProdAbsentCase, m), today)
            if view == "due" and d["due_band"] == "NONE":
                continue
            for p in LETTER_COLUMNS.values():
                d[f"{p}_date"] = _iso(m.get(f"{p}_date"))
                d[f"{p}_sent"] = _iso(m.get(f"{p}_sent"))
            d["pending_leave_since"] = _iso(m.get("pending_leave_since"))
            out.append(d)
        return {"data": out}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism list failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get")
def la_get(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One case with its letters (receipts + attachments), timeline (newest first)
    and the worker's other cases in the branch. The rejoin date (4.6) and the
    leave link (TKT-000223) are refreshed first; the case carries
    pending_leave_since."""
    q = _q(request)
    co_id = req_int(q, "co_id")
    case_id = req_int(q, "case_id")
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_VIEW)
        today = today_ist()
        refresh_rejoin_case(db, c)
        refresh_leave_case(db, c, today)
        case_d = case_out(c, today)
        case_d["pending_leave_since"] = _pending_leave_since(db, int(c.eb_id), today)
        return {
            "data": {
                "case": case_d,
                "letters": _letters_out(db, co_id, int(c.case_id)),
                "events": _events_out(db, co_id, int(c.case_id)),
                "earlier_cases": _other_cases_out(db, c),
            }
        }
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism get failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — open / dismiss / verify / update
# =============================================================================


@router.post("/open")
def la_open(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Open a case for a worker of the branch (spec 4.2): snapshot the employee
    master + last attendance, last working date = body or last seen. No manual
    verification (owner 12.10.2026): the case opens at VERIFIED with the 1st
    warning due from the detection, then the leave refresh links (and parks on)
    an approved leave of the leave module.

    ``detected_date`` (optional, owner 2026-10-12 back-dated entries): when the
    absence was really picked up, <= today and >= the last working date; default
    today. It is the case's detected / stage / verified date and the base of
    absent_days_at_open, so letters issued before the case was entered can be
    recorded after it (/letter_record)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = req_int(body, "branch_id")
    eb_id = req_int(body, "eb_id")
    lwd_in = parse_date(body.get("last_working_date"), "last_working_date")
    detected_in = parse_date(body.get("detected_date"), "detected_date")
    remarks = opt_text(body, "remarks", MAX_REMARKS)
    today = today_ist()
    if lwd_in is not None and lwd_in > today:
        raise HTTPException(status_code=400, detail="last_working_date cannot be in the future")
    if detected_in is not None and detected_in > today:
        raise HTTPException(status_code=400, detail="detected_date cannot be in the future")
    if lwd_in is not None and detected_in is not None and detected_in < lwd_in:
        raise HTTPException(
            status_code=400, detail=f"detected_date cannot be before the last working date ({fmt_dmy(lwd_in)})"
        )
    detected = detected_in or today
    try:
        require_branch_access(db, token_data, co_id, branch_id, LEVEL_CREATE)
        invalidate_candidates(db, co_id, branch_id)
        snap = _snapshot(db, eb_id, branch_id)
        key = {"co_id": co_id, "branch_id": branch_id, "eb_id": eb_id}
        _guard_live(db, key)
        last_cols = _last_attendance_columns(db, eb_id, branch_id, today)
        lwd = _resolve_lwd(db, lwd_in, eb_id, branch_id, today)
        if detected < lwd:
            raise HTTPException(
                status_code=400,
                detail=f"detected_date cannot be before the last working date ({fmt_dmy(lwd)})",
            )
        days = max(0, (detected - lwd).days)
        user_id = _user_id(token_data)
        c = JuteProdAbsentCase(
            **key,
            case_no=None,       # LA/<branch>/<yy>/<case_id>, set once the row has its id
            **_snapshot_columns(snap),
            **last_cols,
            last_working_date=lwd,
            detected_date=detected,
            absent_days_at_open=days,
            **auto_verified_fields(detected),   # VERIFIED, 1st warning due from the detection
            status=STATUS_OPEN,
            habitual=0,
            esi_stl_result=None,
            timekeeper_confirmed=0,
            leave_parked=0,
            remarks=remarks,
            active=1,
            updated_by=user_id,
        )
        db.add(c)
        try:
            db.flush()
            _number_case(db, c, detected)
            opened = f"Case opened. Last working date {fmt_dmy(lwd)}, {days} days absent"
            if detected != today:
                opened += f" when detected on {fmt_dmy(detected)} (back-dated entry, entered {fmt_dmy(today)})"
            add_event(db, c.case_id, "SYSTEM", f"{opened}. 1st warning due now.", user_id, event_date=detected)
            db.commit()
        except IntegrityError as ie:
            if _is_live_unique_violation(ie):
                raise _live_conflict(db, key)
            raise
        refresh_leave_case(db, c, today)    # a worker already on leave carries it from the start
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism open failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/dismiss")
def la_dismiss(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """A candidate that should not become a case (spec 4.5): a CLOSED row with
    exit_reason DISMISSED that hides the worker from Candidates until
    snooze_until. No case number."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = req_int(body, "branch_id")
    eb_id = req_int(body, "eb_id")
    reason = opt_text(body, "reason", MAX_EXIT_REMARKS)
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    snooze_days = opt_int(body, "snooze_days")
    if snooze_days is not None and not (0 <= snooze_days <= MAX_SNOOZE_DAYS):
        raise HTTPException(status_code=400, detail=f"snooze_days must be between 0 and {MAX_SNOOZE_DAYS}")
    lwd_in = parse_date(body.get("last_working_date"), "last_working_date")
    today = today_ist()
    if lwd_in is not None and lwd_in > today:
        raise HTTPException(status_code=400, detail="last_working_date cannot be in the future")
    try:
        require_branch_access(db, token_data, co_id, branch_id, LEVEL_CREATE)
        invalidate_candidates(db, co_id, branch_id)
        snap = _snapshot(db, eb_id, branch_id)
        key = {"co_id": co_id, "branch_id": branch_id, "eb_id": eb_id}
        _guard_live(db, key)
        lwd = _resolve_lwd(db, lwd_in, eb_id, branch_id, today)
        if snooze_days is None:
            snooze_days = int(load_rule(db, co_id)["dismiss_snooze_days"])
        snooze_until = today + timedelta(days=snooze_days)
        user_id = _user_id(token_data)
        c = JuteProdAbsentCase(
            **key,
            case_no=None,
            **_snapshot_columns(snap, full=False),
            last_working_date=lwd,
            detected_date=today,
            absent_days_at_open=max(0, (today - lwd).days),
            stage_code="DETECTED",
            stage_date=today,
            next_due_date=None,
            status=STATUS_CLOSED,
            habitual=0,
            timekeeper_confirmed=0,
            exit_reason="DISMISSED",
            exit_date=today,
            exit_remarks=reason,
            snooze_until=snooze_until,
            active=1,
            updated_by=user_id,
        )
        db.add(c)
        db.flush()
        add_event(
            db, c.case_id, "DISMISS",
            f"Dismissed: {reason} (hidden from candidates until {fmt_dmy(snooze_until)})"[:MAX_TEXT],
            user_id, event_date=today,
        )
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism dismiss failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify")
def la_verify(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Record the ESI / STL check + time keeper confirmation (spec 4.3). A DETECTED
    case moves to VERIFIED (1st warning due today) when the worker is on neither
    and the time keeper confirmed; later stages only update the fields."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    esi = req_code(body, "esi_stl_result", ESI_STL_RESULTS)
    confirmed = opt_bool(body, "timekeeper_confirmed")
    note = opt_text(body, "note", MAX_VERIFY_NOTE)
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_CREATE)
        require_open(c)
        today = today_ist()
        user_id = _user_id(token_data)
        c.esi_stl_result = esi
        c.timekeeper_confirmed = 1 if confirmed else 0
        if "habitual" in body:
            c.habitual = 1 if opt_bool(body, "habitual") else 0
        c.verify_note = note
        c.verified_date = today
        moved = c.stage_code == "DETECTED" and esi == "NONE" and confirmed
        if moved:
            c.stage_code = "VERIFIED"
            c.stage_date = today
            c.next_due_date = today
        c.updated_by = user_id
        summary = (
            f"{ESI_STL_RESULTS[esi]}; time keeper confirmed: {_yes(confirmed)}; habitual: {_yes(c.habitual)}"
            + (". Verified — 1st warning due now" if moved else "")
            + (f". {note}" if note else "")
        )
        add_event(db, c.case_id, "VERIFY", summary[:MAX_TEXT], user_id, event_date=today)
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism verify failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
def la_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Excel-style cells (spec 4.4): remarks, extra_1..extra_5, habitual (create)
    and last_working_date (edit). Only the keys sent change; closed cases too."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    changes: dict = {}
    for k, width in _UPDATE_TEXT_FIELDS.items():
        if k in body:
            changes[k] = opt_text(body, k, width)
    if "habitual" in body:
        changes["habitual"] = 1 if opt_bool(body, "habitual") else 0
    new_lwd = None
    if "last_working_date" in body:
        new_lwd = parse_date(body.get("last_working_date"), "last_working_date", required=True)
    if not changes and new_lwd is None:
        raise HTTPException(
            status_code=400,
            detail="Nothing to update — send remarks, extra_1..extra_5, habitual or last_working_date",
        )
    need = LEVEL_EDIT if new_lwd is not None else LEVEL_CREATE
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, need)
        today = today_ist()
        user_id = _user_id(token_data)
        for k, v in changes.items():
            setattr(c, k, v)
        if new_lwd is not None and new_lwd != _as_date(c.last_working_date):
            detected = _as_date(c.detected_date)
            if new_lwd > detected:
                raise HTTPException(
                    status_code=400,
                    detail=f"last_working_date cannot be after the date the case was opened ({fmt_dmy(detected)})",
                )
            old = c.last_working_date
            c.last_working_date = new_lwd
            c.absent_days_at_open = max(0, (detected - new_lwd).days)
            if c.rejoined_seen_date is not None and _as_date(c.rejoined_seen_date) <= new_lwd:
                c.rejoined_seen_date = None   # re-detected on the next list / get
            add_event(
                db, c.case_id, "SYSTEM",
                f"Last working date changed from {fmt_dmy(old)} to {fmt_dmy(new_lwd)}",
                user_id, event_date=today,
            )
        c.updated_by = user_id
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism update failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — park / resume / exit / reopen
# =============================================================================


@router.post("/park")
def la_park(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """open -> parked while a reply is under review (the case stops ageing)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    note = opt_text(body, "note", MAX_NOTE)
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    try:
        _lock_case(db, case_id)
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_CREATE)
        _require_no_pending_approval(c)
        require_open(c)
        today = today_ist()
        user_id = _user_id(token_data)
        c.status = STATUS_PARKED
        c.updated_by = user_id
        add_event(db, c.case_id, "PARK", note, user_id, event_date=today)
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism park failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume")
def la_resume(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """parked -> open; next_due_date = max(next_due_date, today)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    note = opt_text(body, "note", MAX_NOTE)
    try:
        _lock_case(db, case_id)
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        if int(c.status) != STATUS_PARKED:
            raise HTTPException(status_code=400, detail="This case is not parked")
        today = today_ist()
        user_id = _user_id(token_data)
        c.status = STATUS_OPEN
        c.leave_parked = 0     # resumed by hand: the same running leave never re-parks it
        due = _as_date(c.next_due_date)
        if due is not None and due < today:
            c.next_due_date = today
        c.updated_by = user_id
        add_event(db, c.case_id, "RESUME", note or "Resumed", user_id, event_date=today)
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism resume failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/exit")
def la_exit(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """live -> closed with a user exit reason; the stage is kept. exit_reason
    LEAVE (TKT-000223) needs leave_type_id, an active leave type of the company
    (its name is kept on the case); other reasons leave the leave type alone."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    reason = req_code(body, "exit_reason", USER_EXIT_REASONS)
    exit_date = parse_date(body.get("exit_date"), "exit_date", required=True)
    remarks = opt_text(body, "exit_remarks", MAX_EXIT_REMARKS)
    expected = parse_date(body.get("expected_return_date"), "expected_return_date")
    leave_type_id = opt_int(body, "leave_type_id")
    today = today_ist()
    if exit_date > today:
        raise HTTPException(status_code=400, detail="exit_date cannot be in the future")
    if expected is not None and expected < exit_date:
        raise HTTPException(status_code=400, detail="expected_return_date cannot be before exit_date")
    if reason == "LEAVE" and leave_type_id is None:
        raise HTTPException(status_code=400, detail="leave_type_id is required when exit_reason is LEAVE")
    try:
        _lock_case(db, case_id)
        c = load_case(db, co_id, case_id)
        invalidate_candidates(db, co_id, c.branch_id)
        require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        require_live(c)
        _require_no_pending_approval(c)
        leave_type = _active_leave_type(db, co_id, leave_type_id) if reason == "LEAVE" else None
        user_id = _user_id(token_data)
        c.status = STATUS_CLOSED
        c.leave_parked = 0
        c.exit_reason = reason
        c.exit_date = exit_date
        c.exit_remarks = remarks
        c.expected_return_date = expected
        c.snooze_until = None
        if leave_type is not None:
            c.leave_type_id = int(leave_type_id)
            c.leave_type_name = leave_type_name_of(leave_type)
        c.updated_by = user_id
        label = on_leave_text(c.leave_type_name) if reason == "LEAVE" else EXIT_REASONS[reason]
        text_v = label + (f": {remarks}" if remarks else "")
        if expected is not None:
            text_v += f" (expected back {fmt_dmy(expected)})"
        add_event(db, c.case_id, "EXIT", text_v[:MAX_TEXT], user_id, event_date=today)
        db.commit()
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism exit failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen")
def la_reopen(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """closed -> open; the exit fields are cleared (the event keeps them). Never a
    struck-off case (HR reverses that in HRMS); 409 when the worker has another
    live case. A dismissed candidate gets the full snapshot + a case number, and
    a case still at DETECTED goes straight to VERIFIED (1st warning due now —
    the manual verification step is retired); the leave link is refreshed after
    the commit."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    note = opt_text(body, "note", MAX_NOTE)
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    try:
        _lock_case(db, case_id)
        c = load_case(db, co_id, case_id)
        invalidate_candidates(db, co_id, c.branch_id)
        require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        if int(c.status) != STATUS_CLOSED:
            raise HTTPException(status_code=400, detail="This case is already open")
        if c.exit_reason == "STRUCK_OFF":
            raise HTTPException(
                status_code=400,
                detail="This worker was struck off and has left the roll — HR reverses that in HRMS",
            )
        key = {"co_id": int(c.co_id), "branch_id": int(c.branch_id), "eb_id": int(c.eb_id)}
        _guard_live(db, key, exclude_id=int(c.case_id))
        today = today_ist()
        user_id = _user_id(token_data)
        was = EXIT_REASONS.get(c.exit_reason or "", c.exit_reason or "-")
        text_v = (
            f"Reopened. It was closed on {fmt_dmy(c.exit_date) or '?'} ({was})"
            + (f": {c.exit_remarks}" if c.exit_remarks else "")
            + f". {note}"
        )
        if c.exit_reason == "DISMISSED" or not c.case_no:
            # A dismissed candidate becomes a full case: the dismissal kept only
            # the minimal snapshot, so re-run the whole one (400 when the worker
            # has left the branch / the roll since) and give it a case number.
            snap = _snapshot(db, int(c.eb_id), int(c.branch_id))
            for k, v in _snapshot_columns(snap, full=True).items():
                setattr(c, k, v)
            for k, v in _last_attendance_columns(db, int(c.eb_id), int(c.branch_id), today).items():
                setattr(c, k, v)
            if not c.case_no:
                _number_case(db, c, _as_date(c.detected_date) or today)
        c.status = STATUS_OPEN
        c.leave_parked = 0
        c.exit_reason = None
        c.exit_date = None
        c.exit_remarks = None
        c.expected_return_date = None
        c.snooze_until = None
        due = _as_date(c.next_due_date)
        c.next_due_date = today if due is None or due < today else due
        if c.stage_code == "DETECTED":
            for k, v in auto_verified_fields(today).items():
                setattr(c, k, v)
            text_v = text_v.rstrip(". ") + ". 1st warning due now."
        c.updated_by = user_id
        add_event(db, c.case_id, "REOPEN", text_v[:MAX_TEXT], user_id, event_date=today)
        _commit_live(db, key, exclude_id=int(c.case_id))
        refresh_leave_case(db, c, today)
        return {"data": case_out(c, today)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism reopen failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — timeline
# =============================================================================


@router.get("/events")
def la_events(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = _q(request)
    co_id = req_int(q, "co_id")
    case_id = req_int(q, "case_id")
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_VIEW)
        return {"data": _events_out(db, co_id, int(c.case_id))}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("long absenteeism events failed")
        raise HTTPException(status_code=500, detail=str(e))


def _clean_channel(body: dict, event_type: str) -> Optional[str]:
    """REPLY only: a REPLY_CHANNELS code or None; other types never keep one."""
    if event_type != "REPLY":
        return None
    if body.get("channel") is None or str(body.get("channel")).strip() == "":
        return None
    return req_code(body, "channel", REPLY_CHANNELS)


@router.post("/event_create")
def la_event_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """User timeline entry (REMARK / FEEDBACK / REPLY). A REPLY parks an OPEN case
    unless ``park`` is false (never while the struck-off approval is pending)."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    event_type = req_code(body, "event_type", USER_EVENT_TYPES)
    event_date = parse_date(body.get("event_date"), "event_date", required=True)
    text_v = opt_text(body, "text", MAX_TEXT)
    if not text_v:
        raise HTTPException(status_code=400, detail="text is required")
    channel = _clean_channel(body, event_type)
    contact = opt_text(body, "contact_name", MAX_CONTACT)
    park = opt_bool(body, "park", default=True)
    today = today_ist()
    if event_date > today:
        raise HTTPException(status_code=400, detail="event_date cannot be in the future")
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_CREATE)
        user_id = _user_id(token_data)
        ev = add_event(
            db, c.case_id, event_type, text_v, user_id,
            event_date=event_date, channel=channel, contact_name=contact,
        )
        if event_type == "REPLY" and park and int(c.status) == STATUS_OPEN and not _approval_pending(c):
            c.status = STATUS_PARKED
            c.updated_by = user_id
            add_event(db, c.case_id, "PARK", "Parked: reply received, under review", user_id, event_date=today)
        db.flush()
        event_id = int(ev.event_id)
        db.commit()
        return {"data": {"event": _event_one(db, co_id, int(c.case_id), event_id), "case": case_out(c, today)}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism event create failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/event_update")
def la_event_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Edit a user entry: own entries need create, anyone else's need edit.
    System entries cannot be changed. Only the keys sent change."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    event_id = req_int(body, "event_id")
    today = today_ist()
    changes: dict = {}
    if "event_date" in body:
        d = parse_date(body.get("event_date"), "event_date", required=True)
        if d > today:
            raise HTTPException(status_code=400, detail="event_date cannot be in the future")
        changes["event_date"] = d
    if "contact_name" in body:
        changes["contact_name"] = opt_text(body, "contact_name", MAX_CONTACT)
    if "text" in body:
        t = opt_text(body, "text", MAX_TEXT)
        if not t:
            raise HTTPException(status_code=400, detail="text cannot be empty")
        changes["text"] = t
    try:
        ev, c = _load_event(db, co_id, event_id)
        user_id = _user_id(token_data)
        own = ev.updated_by is not None and user_id is not None and int(ev.updated_by) == int(user_id)
        require_access(db, token_data, c.branch_id, LEVEL_CREATE if own else LEVEL_EDIT)
        if ev.event_type not in USER_EVENT_TYPES:
            raise HTTPException(status_code=400, detail="System timeline entries cannot be changed")
        if "channel" in body:
            changes["channel"] = _clean_channel(body, ev.event_type)
        for k, v in changes.items():
            setattr(ev, k, v)
        ev.updated_by = user_id
        db.commit()
        return {"data": _event_one(db, co_id, int(c.case_id), int(ev.event_id))}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism event update failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/event_deactivate")
def la_event_deactivate(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Remove (soft-delete) a user entry. Edit level; system entries stay."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    event_id = req_int(body, "event_id")
    try:
        ev, c = _load_event(db, co_id, event_id)
        require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        if ev.event_type not in USER_EVENT_TYPES:
            raise HTTPException(status_code=400, detail="System timeline entries cannot be removed")
        ev.active = 0
        ev.updated_by = _user_id(token_data)
        db.commit()
        return {"data": {"event_id": int(event_id), "active": 0}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism event deactivate failed")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — performance (4.12)
# =============================================================================


@router.get("/performance")
def la_performance(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The worker's production in the 30 days up to the last working date, through
    the Low Producer fetch helpers (never re-derived). The summary is cached on
    the case (perf_* columns), so this is a view-level call."""
    q = _q(request)
    co_id = req_int(q, "co_id")
    case_id = req_int(q, "case_id")
    try:
        c = load_case(db, co_id, case_id)
        require_access(db, token_data, c.branch_id, LEVEL_VIEW)
        dept = perf_dept_of(c.designation, c.dept_desc, c.sub_dept_desc)
        if dept is None:
            return {
                "data": {
                    "available": False,
                    "reason": (
                        "Production figures exist only for weavers, spinners and winding workers "
                        f"(this worker: {c.designation or 'no designation'}, {c.dept_desc or 'no department'})"
                    ),
                }
            }
        d_from, d_to = perf_window(_as_date(c.last_working_date), dept)
        rows = fetch_dept_rows(db, dept, int(c.co_id), int(c.branch_id), d_from, d_to)
        person_rows = build_person_rows(rows, int(c.eb_id))
        summary = build_person_summary(person_rows)
        c.perf_avg_eff = summary["avg_eff"]
        c.perf_kg_per_hr = summary["kg_per_hr"]
        c.perf_days_worked = summary["days_worked"]
        c.perf_from = d_from
        c.perf_to = d_to
        c.perf_dept = dept
        db.commit()
        return {
            "data": {
                "available": True,
                "dept": dept,
                "date_from": d_from.isoformat(),
                "date_to": d_to.isoformat(),
                "summary": summary,
                "rows": person_rows,
            }
        }
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        rollback(db)
        logger.exception("long absenteeism performance failed")
        raise HTTPException(status_code=500, detail=str(e))
