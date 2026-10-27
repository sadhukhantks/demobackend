"""Long Absenteeism follow-up, letters side — /api/longAbsenteeism (BE-2 router).

Rule + templates (Settings / Templates tabs), letters (preview, issue one,
issue a batch, record sent date / A.D. / delivery), the dispatch register,
union receipts on the gate notice, the struck-off approval through the standard
approval engine (src/common/approval_utils.py) and the reports. The cases /
candidates / timeline side is long_absenteeism.py (BE-1); both share
long_absenteeism_common.py and the same URL prefix.

Spec: docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md
(sections 4.7-4.11, 5, 6 "BE-2 router", 7, 9).

Rules worth knowing before changing anything here:
  * The legal sequence counts from SENT dates: after a wait of N days from the
    previous letter's sent_date the next letter opens on day N + 1 (letter_block,
    long_absenteeism_common.due_date_for_next). Edit users may force past a
    missing sent date / an unfinished wait (forced = 1, reason in the STAGE
    event); never past a closed / parked case, a wrong stage or a worker who has
    attended since the last working date (REJOINED).
  * Struck off: sendable / datable from the day after the gate notice reply
    window (gate_notice_due = its last day); refused for a rejoined worker and,
    at the final approval, for a worker HR has already separated.
  * Letters are not edited by hand (owner 2026-10-11): /letter_issue and
    /letter_issue_batch always merge the company's template for the stage
    server-side (build_merge_fields + merge_text) and store that text; a
    subject / body sent by the client is ignored. The stored text is then
    frozen (print again uses it).
  * Templates: one per warning (WARNING_1 / _2 / _3), SHOW_CAUSE, GATE_NOTICE,
    STRUCK_OFF_MEMO; the retired single WARNING code is refused.
  * Struck off = one approval through process_approval(commit=False) + our own
    writes (case closed STRUCK_OFF, employee Terminated via
    src.hrms.employee.apply_lifecycle_status) in ONE commit.
  * Back-dated entries (owner 2026-10-12): /letter_record enters a letter that
    was issued outside VoWERP as the case's NEXT stage (order enforced, no wait
    / sent-date gate: historical facts), text merged from today's template,
    recorded = 1. /letters_print and /letters_pdf reprint any active letters
    of the company (recorded ones included).

Plain ``def`` handlers, ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, 400 validation / 403 access / 404 not found /
409 duplicate letter or concurrent change / 500 with logger.exception.
"""

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.approval_query import check_approval_mst_exists, get_max_approval_level
from src.common.approval_utils import (
    calculate_approval_permissions,
    process_approval,
    process_rejection,
)
from src.common.utils import now_ist
from src.config.db import get_tenant_db
from src.hrms.constants import EMPLOYEE_LIFECYCLE_STATUS, RESIGN_DETAIL_STATUSES
from src.hrms.employee import apply_lifecycle_status
from src.juteProduction import long_absenteeism_common as lac
from src.juteProduction.long_absenteeism_common import (
    invalidate_rule,
    DELIVERY_STATUSES,
    DISPATCH_MODES,
    EXIT_REASONS,
    LEGACY_TEMPLATE_LABELS,
    LETTER_NO_OF_STAGE,
    LETTER_STAGES,
    LEVEL_CREATE,
    LEVEL_EDIT,
    LEVEL_VIEW,
    MAX_NOTE,
    MAX_TEXT,
    ORDINALS,
    PLACEHOLDERS,
    PREVIOUS_STAGE,
    RULE_INT_FIELDS,
    RULE_TEXT_FIELDS,
    SO_APPROVED,
    SO_PENDING,
    SO_REJECTED,
    STAGE_LABELS,
    STAGE_ORDER,
    STAGES,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_PARKED,
    TEMPLATE_CODES,
    TEMPLATE_DEFAULTS,
    TEMPLATE_LABELS,
    TEMPLATE_OF_STAGE,
    USER_EVENT_TYPES,
    add_event,
    case_out,
    case_to_dict,
    clean_exclude_prefixes,
    due_date_for_next,
    earlier_cases_sentence,
    first_allowed_day,
    fresh_rejoin_date,
    fmt_dmy,
    habitual_note_sentence,
    json_object,
    letter_to_dict,
    letters_issued_sentence,
    load_case,
    load_letter,
    load_rule,
    merge_text,
    next_stage_of,
    model_to_dict,
    opt_bool,
    opt_int,
    opt_text,
    parse_date,
    previous_notices_sentence,
    rejoined_message,
    reply_days_for,
    req_code,
    req_int,
    require_live,
    require_open,
    rollback,
    template_label,
    today_ist,
    wait_days_for,
)
from src.juteProduction.long_absenteeism_letters_query import (
    EVENT_MODULE,
    LETTER_FILE_TYPES,
    LETTER_MODULE,
    RECEIPT_MODULE,
    delete_template_query,
    get_branch_open_case_letters_query,
    get_branch_open_cases_query,
    get_case_approval_doc_query,
    get_case_attachments_query,
    get_case_id_by_no_query,
    get_case_letters_query,
    get_case_receipts_query,
    get_company_access_query,
    get_company_letterhead_query,
    get_dispatch_query,
    get_employee_status_query,
    get_event_scope_query,
    get_letter_scope_query,
    get_letters_by_ids_query,
    get_monthly_trend_query,
    get_pdf_letterhead_query,
    get_receipt_scope_query,
    get_stage_summary_exits_query,
    get_stage_summary_live_query,
    get_struck_off_register_query,
    get_template_id_query,
    get_templates_query,
    get_union_names_query,
    lock_case_query,
    update_case_approval_status_query,
)
from src.juteProduction.long_absenteeism_query import get_other_cases_query
from src.juteProduction.long_absenteeism_pdf import build_letters_pdf, letters_pdf_filename
from src.juteProduction.long_absenteeism_models import (
    JuteProdAbsentCase,
    JuteProdAbsentLetter,
    JuteProdAbsentRule,
    JuteProdAbsentTemplate,
    JuteProdAbsentUnionReceipt,
)

logger = logging.getLogger(__name__)

router = APIRouter()
long_absenteeism_letters_router = router

MAX_BODY = 20000
MAX_SUBJECT = 300
MAX_AD_NO = 50
MAX_UNION = 100
MAX_RECEIVED_BY = 100
MAX_RECEIPT_REMARKS = 300
MAX_BATCH = 200
MAX_BATCH_REF = 30
MAX_RULE_DAYS = 365
MAX_LOOKBACK_DAYS = 3650   # lookback_days: 0 = no limit, else detect_days..3650 (amendment 11.18)
MAX_RANGE_DAYS = 1100
DISPATCH_DEFAULT_DAYS = 30
EXITS_WINDOW_DAYS = 90
MAX_TREND_MONTHS = 36
MAX_PRINT_LETTERS = 200   # /letters_print, /letters_pdf

TERMINATED_STATUS_ID = EMPLOYEE_LIFECYCLE_STATUS["TERMINATED"]  # 41
STRUCK_OFF_REASON = "Struck off from muster roll, long absence"

# How a letter goes out unless the user says otherwise (previous_notices_sentence
# quotes it: "sent on dd.mm.yyyy by registered post with A.D.").
DEFAULT_DISPATCH = {
    "WARNING_1": "REG_POST_AD",
    "WARNING_2": "REG_POST_AD",
    "WARNING_3": "REG_POST_AD",
    "SHOW_CAUSE": "REG_POST_AD",
    "GATE_NOTICE": "GATE_BOARD",
}

# Any {...} in a template; anything that is not a known placeholder is refused.
_ANY_TOKEN_RE = re.compile(r"\{([^{}\n]*)\}")

LETTER_UPDATE_DATE_FIELDS = ("sent_date", "delivery_date", "gate_display_date", "dept_display_date")


# =============================================================================
# Small helpers
# =============================================================================


def _get(obj, key):
    """Attribute of an ORM row / SimpleNamespace, or key of a dict."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _iso(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _val(v):
    """None for a missing / blank snapshot value (so the letter shows '________')."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return v


def _dmy(v) -> Optional[str]:
    return fmt_dmy(v) or None


def _iso_row(m: dict) -> dict:
    return {k: _iso(v) for k, v in m.items()}


def _rows(db: Session, stmt, params: dict) -> list:
    return [dict(r._mapping) for r in db.execute(stmt, params).fetchall()]


def _opt_code(src: dict, key: str, allowed) -> Optional[str]:
    v = src.get(key)
    if v is None or str(v).strip() == "":
        return None
    return req_code(src, key, allowed)


def _internal_error(db: Session, what: str, exc: Exception) -> HTTPException:
    rollback(db)
    logger.exception("long absenteeism %s failed", what)
    return HTTPException(status_code=500, detail=str(exc))


def _user_id(token_data: dict):
    return (token_data or {}).get("user_id")


# =============================================================================
# Access (company-wide endpoints, levels)
# =============================================================================


def company_access_level(db: Session, user_id, co_id: int) -> int:
    """Highest level on the Long Absenteeism menu over all branches of co_id."""
    if not user_id:
        return 0
    mid = lac.menu_id(db)
    if mid is None:
        return 0
    row = db.execute(
        get_company_access_query(), {"user_id": int(user_id), "co_id": int(co_id), "menu_id": mid}
    ).first()
    level = row[0] if row else None
    try:
        return int(level) if level is not None else 0
    except (TypeError, ValueError):
        return 0


def require_scope_access(db: Session, token_data: dict, co_id: int, branch_id: Optional[int], level: int) -> None:
    """Company-wide settings: with branch_id the branch's level (the pair must
    match); without it the user's best level in any branch of the company."""
    if branch_id is not None:
        lac.require_branch_access(db, token_data, co_id, branch_id, level)
        return
    if company_access_level(db, _user_id(token_data), co_id) < level:
        raise HTTPException(
            status_code=403,
            detail=f"You need {lac._LEVEL_NAMES.get(level, str(level))} access to Long Absenteeism",
        )


def require_level(db: Session, token_data: dict, branch_id, level: int) -> int:
    """lac.require_access that also returns the caller's level (for can_force / can_send)."""
    have = lac.access_level(db, _user_id(token_data), branch_id)
    if have < level:
        raise HTTPException(
            status_code=403,
            detail=f"You need {lac._LEVEL_NAMES.get(level, str(level))} access to Long Absenteeism for this branch",
        )
    return have


_SCOPE_OF_MODULE = {
    LETTER_MODULE: (get_letter_scope_query, "Letter"),
    RECEIPT_MODULE: (get_receipt_scope_query, "Union receipt"),
    EVENT_MODULE: (get_event_scope_query, "Timeline entry"),
}


def check_absent_attachment_access(
    db: Session, token_data: dict, co_id: int, entity_id: int, action: str, module: str
) -> None:
    """Gate of the generic attachment API for the three Long Absenteeism modules
    (called by src/common/attachments/router.py).

    The entity (letter / receipt / timeline event) must be active and belong to
    an active case of co_id (404 otherwise). read (list / download) needs view on
    the case's branch; upload needs create for timeline events and edit for
    letters / receipts; delete needs edit. Timeline uploads are accepted only on
    the user entries (REMARK / FEEDBACK / REPLY), never on system rows."""
    if module not in _SCOPE_OF_MODULE:
        raise HTTPException(status_code=400, detail=f"Invalid module: {module}")
    if action not in ("read", "upload", "delete"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    query_fn, label = _SCOPE_OF_MODULE[module]
    row = db.execute(query_fn(), {"entity_id": int(entity_id)}).first()
    m = row._mapping if row is not None else None
    if (
        m is None
        or int(m["co_id"]) != int(co_id)
        or int(m["case_active"] or 0) != 1
        or int(m["entity_active"] or 0) != 1
    ):
        raise HTTPException(status_code=404, detail=f"{label} not found")
    if module == EVENT_MODULE and action == "upload" and m.get("event_type") not in USER_EVENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Files can be attached only to remarks, feedback and replies",
        )
    if action == "read":
        level = LEVEL_VIEW
    elif action == "delete":
        level = LEVEL_EDIT
    else:
        level = LEVEL_CREATE if module == EVENT_MODULE else LEVEL_EDIT
    lac.require_access(db, token_data, int(m["branch_id"]), level)


# =============================================================================
# Pure letter logic (unit-tested without a DB)
# =============================================================================


def letters_by_stage(letters: list) -> dict:
    """{stage_code: letter} of the active letters (the last one wins)."""
    out = {}
    for l in letters or []:
        out[_get(l, "stage_code")] = l
    return out


def default_letter_stage(case) -> Optional[str]:
    """The letter a case would get next: the 1st warning for a DETECTED case (it
    is blocked until verified), the memo once struck off, else the next rung."""
    st = _get(case, "stage_code")
    if st == "DETECTED":
        return "WARNING_1"
    if st == "STRUCK_OFF":
        return "STRUCK_OFF"
    for stage, prev in PREVIOUS_STAGE.items():
        if prev == st:
            return stage
    return None


def letter_due_date(case, letters: list, rule: dict, stage_code: str) -> Optional[date]:
    """The FIRST day the stage's letter may be issued — the same rule as the
    case's next_due_date (long_absenteeism_common.due_date_for_next): a wait of
    N days after the previous letter's sent_date (issue_date until it is
    recorded) opens on day N + 1. The 1st warning is due from the verification
    (verified_date, else the VERIFIED stage date); the memo has no wait."""
    if stage_code == "STRUCK_OFF":
        return None
    if stage_code == "WARNING_1":
        if _get(case, "stage_code") != "VERIFIED":
            return None
        return _as_date(_get(case, "verified_date")) or _as_date(_get(case, "stage_date"))
    prev = letters_by_stage(letters).get(PREVIOUS_STAGE[stage_code])
    base = None
    if prev is not None:
        base = _as_date(_get(prev, "sent_date")) or _as_date(_get(prev, "issue_date"))
    base = base or _as_date(_get(case, "stage_date"))
    if base is None:
        return None
    return first_allowed_day(base, wait_days_for(stage_code, rule))


def _block(code: str, reason: str, forceable: bool) -> dict:
    return {"code": code, "reason": reason, "forceable": forceable}


def letter_block(case, letters: list, rule: dict, stage_code: str, on_date: date,
                 rejoined_on: Optional[date] = None) -> Optional[dict]:
    """Why the stage's letter cannot be issued on ``on_date`` (the letter date),
    or None. Gates in order (spec 4.7 + amendments 11.1 / 11.13): case not open
    (NOT_OPEN), the worker has attended since the last working date (REJOINED;
    ``rejoined_on`` = fresh_rejoin_date of the case), wrong stage (WRONG_STAGE),
    letter already issued (ALREADY_ISSUED), the 1st warning dated before the
    verification (NOT_DUE), previous letter has no sent date (NOT_SENT), not yet
    due (NOT_DUE: ``on_date`` before the first allowed day). Only NOT_SENT /
    NOT_DUE may be forced. The struck-off memo only needs the case to be struck
    off."""
    stage_label = STAGE_LABELS.get(stage_code, stage_code)
    cur = _get(case, "stage_code")
    by_stage = letters_by_stage(letters)
    if stage_code == "STRUCK_OFF":
        if cur != "STRUCK_OFF":
            return _block(
                "WRONG_STAGE", "The struck-off memo can only be issued after the struck-off approval", False
            )
    else:
        status = int(_get(case, "status"))
        if status != STATUS_OPEN:
            return _block(
                "NOT_OPEN",
                "This case is parked (reply under review) — resume it first" if status == STATUS_PARKED
                else "This case is closed — reopen it to change it",
                False,
            )
        if rejoined_on is not None:
            return _block("REJOINED", rejoined_message(rejoined_on), False)
        required = PREVIOUS_STAGE[stage_code]
        if cur != required:
            return _block(
                "WRONG_STAGE",
                f"The {stage_label} can only follow the {STAGE_LABELS.get(required, required)} stage "
                f"(the case is at {STAGE_LABELS.get(cur, cur)})",
                False,
            )
    issued = by_stage.get(stage_code)
    if issued is not None:
        return _block(
            "ALREADY_ISSUED",
            f"The {stage_label} has already been issued (letter dated {fmt_dmy(_get(issued, 'issue_date'))})",
            False,
        )
    if stage_code == "STRUCK_OFF":
        return None
    if stage_code == "WARNING_1":
        floor = letter_due_date(case, letters, rule, stage_code)
        if floor is not None and on_date < floor:
            return _block(
                "NOT_DUE", f"The 1st warning cannot be dated before the verification ({fmt_dmy(floor)})", True
            )
        return None
    prev = by_stage.get(PREVIOUS_STAGE[stage_code])
    if prev is not None and not _get(prev, "sent_date"):
        prev_label = STAGE_LABELS.get(PREVIOUS_STAGE[stage_code])
        return _block("NOT_SENT", f"Record the sent date of the {prev_label} first", True)
    due = letter_due_date(case, letters, rule, stage_code)
    if due is not None and on_date < due:
        return _block("NOT_DUE", f"Due on {fmt_dmy(due)}", True)
    return None


def letter_block_reason(case, letters: list, rule: dict, stage_code: str, today: date,
                        rejoined_on: Optional[date] = None) -> Optional[str]:
    """letter_block's message only (None = the letter may be issued)."""
    b = letter_block(case, letters, rule, stage_code, today, rejoined_on)
    return b["reason"] if b else None


def check_issue_date_order(case, letters: list, stage_code: str, issue_date: date) -> None:
    """Even a forced letter may not be dated before the last working date or
    before an earlier letter of the case."""
    lwd = _as_date(_get(case, "last_working_date"))
    if lwd is not None and issue_date < lwd:
        raise HTTPException(
            status_code=400, detail=f"issue_date cannot be before the last working date ({fmt_dmy(lwd)})"
        )
    order = STAGE_ORDER.get(stage_code, len(STAGES))
    earlier = [
        _as_date(_get(l, "issue_date")) for l in letters or []
        if STAGE_ORDER.get(_get(l, "stage_code"), len(STAGES)) < order
    ]
    earlier = [d for d in earlier if d is not None]
    if earlier and issue_date < max(earlier):
        raise HTTPException(
            status_code=400,
            detail=f"issue_date cannot be before the previous letter's date ({fmt_dmy(max(earlier))})",
        )


def company_address(company: Optional[dict]) -> Optional[str]:
    company = company or {}
    parts = [
        str(company.get(k)).strip() for k in ("co_address1", "co_address2")
        if company.get(k) is not None and str(company.get(k)).strip()
    ]
    addr = ", ".join(parts)
    zipcode = company.get("co_zipcode")
    if zipcode not in (None, "", 0):
        addr = f"{addr} - {zipcode}" if addr else str(zipcode)
    return addr or None


def build_merge_fields(case_dict: dict, letters: list, rule: dict, company: Optional[dict],
                       stage_code: str, issue_date, earlier_cases: Optional[list] = None) -> dict:
    """Every PLACEHOLDERS key for one letter of a case.

    ``case_dict``: case_to_dict(case) (ISO dates are fine). ``letters``: the
    case's active letters (dicts: stage_code, letter_no, issue_date, sent_date,
    ad_no, dispatch_mode); only the ones BEFORE ``stage_code`` are cited.
    ``company``: the letterhead row (co_name, co_address1/2, co_zipcode).
    ``earlier_cases``: the worker's other cases in the branch
    (fetch_earlier_cases: BE-1's /get query) for {earlier_cases}.
    Dates are dd.mm.yyyy; absent_days runs to ``issue_date`` (or the rejoin date).
    A missing value is None (merge_text prints '________'); previous_notices /
    letters_issued / habitual_note / earlier_cases are '' when there is nothing
    to say (the paragraph drops out).
    previous_notices addresses the worker ("our ... letter ... to you", for the
    letters he receives); letters_issued is the neutral list (the memo)."""
    issue = _as_date(issue_date)
    order = STAGE_ORDER.get(stage_code, len(STAGES))
    earlier = [l for l in letters or [] if STAGE_ORDER.get(_get(l, "stage_code"), len(STAGES)) < order]
    earlier = [l if isinstance(l, dict) else model_to_dict(l) for l in earlier]
    by_stage = letters_by_stage(earlier)

    lwd = _as_date(case_dict.get("last_working_date"))
    end = issue
    rejoined = _as_date(case_dict.get("rejoined_seen_date"))
    if rejoined is not None and end is not None and rejoined < end:
        end = rejoined
    absent_days = max(0, (end - lwd).days) if (lwd is not None and end is not None) else None

    letter_no = LETTER_NO_OF_STAGE.get(stage_code)
    reply_days = reply_days_for(stage_code, rule)
    reply_due = issue + timedelta(days=reply_days) if (issue is not None and reply_days) else None

    def letter_date(stage: str, key: str) -> Optional[str]:
        l = by_stage.get(stage)
        return _dmy(l.get(key)) if l else None

    company = company or {}
    return {
        "name": _val(case_dict.get("emp_name")),
        "eb_no": _val(case_dict.get("emp_code")),
        "dept": _val(case_dict.get("dept_desc")),
        "designation": _val(case_dict.get("designation")),
        "father_name": _val(case_dict.get("father_name")),
        "address": _val(case_dict.get("address")),
        "mobile": _val(case_dict.get("mobile")),
        "case_no": _val(case_dict.get("case_no")),
        "last_working_date": _dmy(lwd),
        "absent_days": absent_days,
        "letter_date": _dmy(issue),
        "letter_no": letter_no,
        "letter_ordinal": ORDINALS.get(letter_no) if letter_no else None,
        "reply_days": reply_days or None,
        "reply_due_date": _dmy(reply_due),
        "previous_notices": previous_notices_sentence(earlier),
        "letters_issued": letters_issued_sentence(earlier),
        "habitual_note": habitual_note_sentence(case_dict.get("habitual")),
        "earlier_cases": earlier_cases_sentence(earlier_cases or [], before=case_dict.get("detected_date")),
        "warning1_date": letter_date("WARNING_1", "issue_date"),
        "warning2_date": letter_date("WARNING_2", "issue_date"),
        "warning3_date": letter_date("WARNING_3", "issue_date"),
        "warning1_sent": letter_date("WARNING_1", "sent_date"),
        "warning2_sent": letter_date("WARNING_2", "sent_date"),
        "warning3_sent": letter_date("WARNING_3", "sent_date"),
        "show_cause_date": letter_date("SHOW_CAUSE", "issue_date"),
        "show_cause_sent": letter_date("SHOW_CAUSE", "sent_date"),
        "gate_notice_date": letter_date("GATE_NOTICE", "issue_date"),
        "gate_notice_sent": letter_date("GATE_NOTICE", "sent_date"),
        "struck_off_date": _dmy(case_dict.get("struck_off_date")),
        "sc_reply_days": rule.get("sc_reply_days"),
        "clause_text": _val(rule.get("clause_text")),
        "company_name": _val(company.get("co_name")),
        "company_address": company_address(company),
        "signatory": _val(rule.get("signatory")),
    }


def render_letter(template: dict, fields: dict) -> tuple:
    """(subject, body) merged with merge_text: a None field prints '________' so
    missing data stays visible; '' (previous_notices on the 1st warning) prints
    nothing."""
    return merge_text(template.get("subject") or "", fields), merge_text(template.get("body") or "", fields)


def unknown_tokens(text_value: Optional[str]) -> list:
    """Tokens in braces that are not placeholders, in order, without repeats."""
    out = []
    for m in _ANY_TOKEN_RE.finditer(text_value or ""):
        tok = m.group(1)
        if tok not in PLACEHOLDERS and tok not in out:
            out.append(tok)
    return out


def gate_notice_due(gate_letter, notice_reply_days: Optional[int] = None) -> Optional[date]:
    """The LAST day of the gate notice reply window (struck off may be sent /
    dated from the day after). Counted from the SENT date (owner rule: the legal
    sequence counts from when letters went out) when it is recorded and the
    rule's notice_reply_days is known; else the reply_due_date printed on the
    letter (issue_date + window, also a last day)."""
    if gate_letter is None:
        return None
    sent = _as_date(_get(gate_letter, "sent_date"))
    if sent is not None and notice_reply_days is not None:
        return sent + timedelta(days=int(notice_reply_days))
    return _as_date(_get(gate_letter, "reply_due_date"))


def struck_off_send_block(case, gate_letter, today: date, notice_reply_days: Optional[int] = None,
                          rejoined_on: Optional[date] = None) -> Optional[str]:
    """Why the case cannot be sent for struck-off approval today (None = it can):
    open, the worker has not attended since the last working date
    (``rejoined_on`` = fresh_rejoin_date), at GATE_NOTICE, not already pending /
    approved, the gate notice has a sent date and its reply window (sent_date +
    notice_reply_days, else the printed reply_due_date) has run out — blocked
    while today <= the window's last day."""
    status = int(_get(case, "status"))
    if status != STATUS_OPEN:
        return (
            "This case is parked (reply under review) — resume it first" if status == STATUS_PARKED
            else "This case is closed — reopen it to change it"
        )
    if rejoined_on is not None:
        return rejoined_message(rejoined_on)
    stage = _get(case, "stage_code")
    if stage == "STRUCK_OFF":
        return "This worker is already struck off"
    if stage != "GATE_NOTICE":
        return f"Struck off can only be sent after the gate notice (the case is at {STAGE_LABELS.get(stage, stage)})"
    sid = _get(case, "status_id")
    if sid == SO_PENDING:
        return "Already sent for struck-off approval"
    if sid == SO_APPROVED:
        return "Struck off is already approved"
    if sid not in (None, SO_REJECTED):
        return f"Unexpected struck-off approval status {sid}"
    if gate_letter is None:
        return "The gate notice letter was not found"
    if not _get(gate_letter, "sent_date"):
        return "Record the sent date of the gate notice first"
    due = gate_notice_due(gate_letter, notice_reply_days)
    if due is not None and today <= due:
        return (
            f"Struck off can be sent from {fmt_dmy(due + timedelta(days=1))} (the gate notice reply window, "
            f"counted from its sent date, runs to {fmt_dmy(due)})"
        )
    return None


def clean_rule(current: dict, body: dict) -> dict:
    """The rule to store: ints from the body (else the current value), 0..365,
    detect_days >= 1, lookback_days 0 (no limit) or detect_days..3650; texts
    only when sent.
    exclude_prefixes is stored canonical ('T,C,R,L'; blank / null -> NULL = the
    default) and must be single letters (amendment 11.17). The retired
    exclude_t_codes flag is ignored if an older client still sends it."""
    out = {}
    for key in RULE_INT_FIELDS:
        v = body[key] if key in body else current.get(key)
        if isinstance(v, bool) or v is None or (isinstance(v, str) and v.strip() == ""):
            raise HTTPException(status_code=400, detail=f"{key} must be a whole number of days")
        if isinstance(v, float):
            if not v.is_integer():
                raise HTTPException(status_code=400, detail=f"{key} must be a whole number of days")
            v = int(v)
        try:
            n = int(str(v).strip()) if isinstance(v, str) else int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be a whole number of days")
        max_days = MAX_LOOKBACK_DAYS if key == "lookback_days" else MAX_RULE_DAYS
        if n < 0 or n > max_days:
            raise HTTPException(status_code=400, detail=f"{key} must be between 0 and {max_days}")
        out[key] = n
    if out["detect_days"] < 1:
        raise HTTPException(status_code=400, detail="detect_days must be at least 1")
    if out["lookback_days"] != 0 and out["lookback_days"] < out["detect_days"]:
        raise HTTPException(status_code=400, detail="lookback_days must be 0 (no limit) or at least detect_days")
    for key, max_len in RULE_TEXT_FIELDS.items():
        if key not in body:
            continue
        if key == "exclude_prefixes":
            out[key] = clean_exclude_prefixes(body[key])
        else:
            out[key] = opt_text(body, key, max_len)
    return out


def shape_letter_row(m: dict) -> dict:
    """A letter row (SQL mapping) in letter_to_dict's shape."""
    d = _iso_row(m)
    d["stage_label"] = STAGE_LABELS.get(m.get("stage_code"), m.get("stage_code"))
    d["template_label"] = template_label(m.get("template_code"))
    d["dispatch_mode_label"] = DISPATCH_MODES.get(m.get("dispatch_mode") or "", m.get("dispatch_mode"))
    d["delivery_status_label"] = DELIVERY_STATUSES.get(m.get("delivery_status") or "", m.get("delivery_status"))
    return d


def template_dict(code: str, saved: Optional[dict]) -> dict:
    default = TEMPLATE_DEFAULTS[code]
    return {
        "template_code": code,
        "label": TEMPLATE_LABELS.get(code, code),
        "lang": "en",
        "subject": (saved or {}).get("subject") if saved else default["subject"],
        "body": saved["body"] if saved else default["body"],
        "is_default": saved is None,
        "updated_date_time": _iso((saved or {}).get("updated_date_time")),
    }


def shape_stage_summary(live_rows: list, exit_rows: list) -> dict:
    codes = [c for c, _ in STAGES]
    depts: dict = {}
    for m in live_rows:
        key = (m.get("dept_id"), m.get("dept_desc") or "")
        d = depts.get(key)
        if d is None:
            d = depts[key] = {
                "dept_id": m.get("dept_id"),
                "dept_desc": m.get("dept_desc") or "(No department)",
                "counts": {c: 0 for c in codes},
                "parked": 0,
                "total_live": 0,
            }
        n = int(m.get("n") or 0)
        stage = m.get("stage_code")
        d["counts"][stage] = d["counts"].get(stage, 0) + n
        d["parked"] += int(m.get("parked") or 0)
        d["total_live"] += n
    by_dept = sorted(depts.values(), key=lambda d: str(d["dept_desc"]).lower())
    totals = {"counts": {c: 0 for c in codes}, "parked": 0, "total_live": 0}
    for d in by_dept:
        for c, n in d["counts"].items():
            totals["counts"][c] = totals["counts"].get(c, 0) + n
        totals["parked"] += d["parked"]
        totals["total_live"] += d["total_live"]
    exits = [
        {
            "exit_reason": m.get("exit_reason"),
            "label": EXIT_REASONS.get(m.get("exit_reason") or "", m.get("exit_reason")),
            "count": int(m.get("n") or 0),
        }
        for m in exit_rows
    ]
    return {"by_dept": by_dept, "totals": totals, "exits": exits}


def month_keys(today: date, months: int) -> list:
    """'YYYY-MM' of the last ``months`` months, oldest first, ending this month."""
    y, m = today.year, today.month
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def build_monthly_trend(rows: list, keys: list) -> list:
    base = {k: {"month": k, "opened": 0, "struck_off": 0, "rejoined": 0, "other_exits": 0} for k in keys}
    for m in rows:
        slot = base.get(m.get("month"))
        kind = m.get("kind")
        if slot is not None and kind in slot and kind != "month":
            slot[kind] += int(m.get("n") or 0)
    return [base[k] for k in keys]


# =============================================================================
# DB-backed helpers
# =============================================================================


def fetch_case_letters(db: Session, case_id: int) -> list:
    return _rows(db, get_case_letters_query(), {"case_id": int(case_id)})


def load_company(db: Session, branch_id: int) -> dict:
    row = db.execute(get_company_letterhead_query(), {"branch_id": int(branch_id)}).first()
    return dict(row._mapping) if row is not None else {}


def fetch_earlier_cases(db: Session, case) -> list:
    """The worker's other case rows in the company + branch (the query BE-1's
    /get uses for earlier_cases); earlier_cases_sentence picks the closed ones."""
    return _rows(db, get_other_cases_query(), {
        "co_id": int(_get(case, "co_id")), "branch_id": int(_get(case, "branch_id")),
        "eb_id": int(_get(case, "eb_id")), "case_id": int(_get(case, "case_id")),
    })


def merge_case_letter(db: Session, case, letters: list, rule: dict, company: Optional[dict], template: dict,
                      stage: str, issue_date: date) -> tuple:
    """(fields, subject, body) of a case's letter merged from ``template`` —
    the one path preview / issue / batch share, so what is stored is exactly
    what the preview showed for the same date."""
    fields = build_merge_fields(case_to_dict(case), letters, rule, company, stage, issue_date,
                                earlier_cases=fetch_earlier_cases(db, case))
    subject, text_body = render_letter(template, fields)
    return fields, subject, text_body


def load_templates(db: Session, co_id: int) -> dict:
    """{template_code: {subject, body, is_default}} — saved row else the default."""
    saved = {m["template_code"]: m for m in _rows(db, get_templates_query(), {"co_id": int(co_id)})}
    return {code: template_dict(code, saved.get(code)) for code, _ in TEMPLATE_CODES}


def letters_payload(db: Session, co_id: int, case_id: int) -> list:
    """The case's active letters (letter_to_dict shape) with their active union
    receipts and the latest active attachment per slot."""
    letters = fetch_case_letters(db, case_id)
    receipts = _rows(db, get_case_receipts_query(), {"case_id": int(case_id)})
    atts = _rows(
        db,
        get_case_attachments_query(),
        {"co_id": int(co_id), "case_id": int(case_id), "letter_module": LETTER_MODULE,
         "receipt_module": RECEIPT_MODULE},
    )
    att = {
        (a["module"], int(a["entity_id"]), a["file_type"]): {
            "attachment_id": a["attachment_id"], "filename": a.get("original_filename"),
        }
        for a in atts
    }
    by_letter = defaultdict(list)
    for r in receipts:
        rd = _iso_row(r)
        rd["attachment"] = att.get((RECEIPT_MODULE, int(r["receipt_id"]), "union_receipt"))
        by_letter[int(r["letter_id"])].append(rd)
    out = []
    for l in letters:
        d = shape_letter_row(l)
        lid = int(l["letter_id"])
        d["receipts"] = by_letter.get(lid, [])
        d["attachments"] = {ft: att.get((LETTER_MODULE, lid, ft)) for ft in LETTER_FILE_TYPES}
        out.append(d)
    return out


def _insert_letter(db: Session, case, stage: str, subject, body: str, issue_date: date, dispatch_mode,
                   rule: dict, user_id, forced: bool, batch_ref: Optional[str],
                   **extra) -> JuteProdAbsentLetter:
    """Insert one letter. ``extra``: columns set only by /letter_record
    (sent_date, ad_no, delivery_status, delivery_date, recorded) — the issue
    paths never name ``recorded``, so they do not need migration 20261012b."""
    reply_days = reply_days_for(stage, rule)
    cols = dict(
        case_id=int(case.case_id),
        stage_code=stage,
        letter_no=LETTER_NO_OF_STAGE.get(stage),
        template_code=TEMPLATE_OF_STAGE[stage],
        issue_date=issue_date,
        subject=subject,
        body=body,
        reply_due_date=issue_date + timedelta(days=reply_days) if reply_days else None,
        dispatch_mode=dispatch_mode or DEFAULT_DISPATCH.get(stage),
        sent_date=None,
        batch_ref=batch_ref,
        forced=1 if forced else 0,
        active=1,
        updated_by=user_id,
    )
    cols.update(extra)
    letter = JuteProdAbsentLetter(**cols)
    db.add(letter)
    db.flush()
    return letter


def _advance_case(case: JuteProdAbsentCase, letter: JuteProdAbsentLetter, stage: str, issue_date: date,
                  rule: dict, user_id) -> None:
    """The case enters the letter's stage (the memo leaves a struck-off case as is)."""
    case.updated_by = user_id
    if stage == "STRUCK_OFF":
        return
    case.stage_code = stage
    case.stage_date = issue_date
    case.next_due_date = due_date_for_next(case, letter, rule)


def _stage_event_text(stage: str, issue_date: date, batch_ref: Optional[str] = None,
                      force_reason: Optional[str] = None) -> str:
    label = STAGE_LABELS.get(stage, stage)
    if stage == "STRUCK_OFF":
        label = "Struck-off memo"
    s = f"{label} issued, letter dated {fmt_dmy(issue_date)}"
    if batch_ref:
        s += f" (batch {batch_ref})"
    if force_reason:
        s += f". FORCED: {force_reason}"
    return s[:MAX_TEXT]


def _case_in_company(db: Session, co_id: int, case_id: Optional[int], case_no: Optional[str]) -> JuteProdAbsentCase:
    if case_id is None:
        row = db.execute(get_case_id_by_no_query(), {"co_id": int(co_id), "case_no": case_no}).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Case not found")
        case_id = int(row._mapping["case_id"])
    return load_case(db, co_id, case_id)


def _gate_letter(db: Session, case_id: int):
    return letters_by_stage(fetch_case_letters(db, case_id)).get("GATE_NOTICE")


def _template_code(body: dict) -> str:
    """template_code of /template_save and /template_reset; the retired single
    WARNING code gets a 400 that names its replacements."""
    raw = body.get("template_code")
    if isinstance(raw, str) and raw.strip().upper() in LEGACY_TEMPLATE_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"template_code {raw.strip().upper()} is no longer used — the warnings are WARNING_1, "
                   "WARNING_2 and WARNING_3",
        )
    return req_code(body, "template_code", TEMPLATE_LABELS)


# =============================================================================
# Rule
# =============================================================================


@router.get("/rule")
def la_rule(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = opt_int(q, "branch_id")
    try:
        require_scope_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        return {"data": load_rule(db, co_id)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "rule", e)


@router.post("/rule_save")
def la_rule_save(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = opt_int(body, "branch_id")
    try:
        require_scope_access(db, token_data, co_id, branch_id, LEVEL_EDIT)
        cleaned = clean_rule(load_rule(db, co_id), body)
        row = db.get(JuteProdAbsentRule, co_id)
        if row is None:
            row = JuteProdAbsentRule(co_id=co_id)
            db.add(row)
        for key in RULE_INT_FIELDS:
            setattr(row, key, cleaned[key])
        for key in RULE_TEXT_FIELDS:
            if key in cleaned:
                setattr(row, key, cleaned[key])
        row.updated_by = _user_id(token_data)
        try:
            db.commit()
            invalidate_rule(db, co_id)
        except IntegrityError:
            rollback(db)
            raise HTTPException(status_code=409, detail="The settings were saved by someone else — reload and try again")
        return {"data": load_rule(db, co_id)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "rule_save", e)


# =============================================================================
# Templates
# =============================================================================


@router.get("/templates")
def la_templates(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = opt_int(q, "branch_id")
    try:
        require_scope_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        templates = load_templates(db, co_id)
        return {
            "data": {
                "templates": [templates[code] for code, _ in TEMPLATE_CODES],
                "placeholders": [{"code": k, "label": v} for k, v in PLACEHOLDERS.items()],
            }
        }
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "templates", e)


@router.post("/template_save")
def la_template_save(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = opt_int(body, "branch_id")
    code = _template_code(body)
    subject = opt_text(body, "subject", MAX_SUBJECT)
    text_body = body.get("body")
    if not isinstance(text_body, str) or text_body.strip() == "":
        raise HTTPException(status_code=400, detail="body is required")
    if len(text_body) > MAX_BODY:
        raise HTTPException(status_code=400, detail=f"body must be at most {MAX_BODY} characters")
    bad = unknown_tokens((subject or "") + "\n" + text_body)
    if bad:
        listed = ", ".join("{" + t + "}" for t in bad)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown placeholder {listed} — use only the listed placeholders",
        )
    try:
        require_scope_access(db, token_data, co_id, branch_id, LEVEL_EDIT)
        found = db.execute(get_template_id_query(), {"co_id": co_id, "template_code": code}).first()
        row = db.get(JuteProdAbsentTemplate, int(found._mapping["template_id"])) if found is not None else None
        if row is None:
            row = JuteProdAbsentTemplate(co_id=co_id, template_code=code, lang="en")
            db.add(row)
        row.subject = subject
        row.body = text_body
        row.active = 1
        row.updated_by = _user_id(token_data)
        try:
            db.commit()
        except IntegrityError:
            rollback(db)
            raise HTTPException(status_code=409, detail="This template was saved by someone else — reload and try again")
        return {"data": template_dict(code, {"subject": subject, "body": text_body})}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "template_save", e)


@router.post("/template_reset")
def la_template_reset(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = opt_int(body, "branch_id")
    code = _template_code(body)
    try:
        require_scope_access(db, token_data, co_id, branch_id, LEVEL_EDIT)
        db.execute(delete_template_query(), {"co_id": co_id, "template_code": code})
        db.commit()
        return {"data": template_dict(code, None)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "template_reset", e)


# =============================================================================
# Letters
# =============================================================================


@router.get("/letters")
def la_letters(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    case_id = req_int(q, "case_id")
    try:
        c = load_case(db, co_id, case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_VIEW)
        return {"data": letters_payload(db, co_id, case_id)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letters", e)


@router.get("/letters_due")
def la_letters_due(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    stage_filter = _opt_code(q, "stage_code", LETTER_STAGES)
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        today = today_ist()
        rule = load_rule(db, co_id)
        params = {"co_id": co_id, "branch_id": branch_id}
        cases = _rows(db, get_branch_open_cases_query(), params)
        letters_of = defaultdict(list)
        for l in _rows(db, get_branch_open_case_letters_query(), params):
            letters_of[int(l["case_id"])].append(l)
        out = []
        for m in cases:
            if m.get("rejoined_seen_date") is not None:
                continue  # marked present: a person reviews the case, no batch letter
            nxt = default_letter_stage(m)
            if nxt is None or nxt == "STRUCK_OFF" or (stage_filter and nxt != stage_filter):
                continue
            letters = letters_of.get(int(m["case_id"]), [])
            if letter_block(m, letters, rule, nxt, today) is not None:
                continue
            lwd = _as_date(m.get("last_working_date"))
            out.append({
                "case_id": m["case_id"],
                "case_no": m.get("case_no"),
                "eb_id": m.get("eb_id"),
                "emp_code": m.get("emp_code"),
                "emp_name": m.get("emp_name"),
                "dept_id": m.get("dept_id"),
                "dept_desc": m.get("dept_desc"),
                "designation": m.get("designation"),
                "last_working_date": _iso(lwd),
                "absent_days": max(0, (today - lwd).days) if lwd else None,
                "stage_code": m.get("stage_code"),
                "stage_label": STAGE_LABELS.get(m.get("stage_code"), m.get("stage_code")),
                "next_stage_code": nxt,
                "next_stage_label": STAGE_LABELS.get(nxt, nxt),
                "due_date": _iso(letter_due_date(m, letters, rule, nxt)),
            })
        return {"data": out}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letters_due", e)


@router.post("/letter_preview")
def la_letter_preview(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = opt_int(body, "case_id")
    case_no = opt_text(body, "case_no", 30)
    if case_id is None and case_no is None:
        raise HTTPException(status_code=400, detail="case_id is required")
    stage_in = _opt_code(body, "stage_code", LETTER_STAGES)
    issue_date = parse_date(body.get("issue_date"), "issue_date") or today_ist()
    subject_override = body.get("template_subject")
    body_override = body.get("template_body")
    try:
        c = _case_in_company(db, co_id, case_id, case_no)
        level = require_level(db, token_data, c.branch_id, LEVEL_VIEW)
        stage = stage_in or default_letter_stage(c)
        if stage is None:
            raise HTTPException(status_code=400, detail="This case has no letter to issue")
        letters = fetch_case_letters(db, c.case_id)
        rule = load_rule(db, co_id)
        company = load_company(db, c.branch_id)
        tpl = dict(load_templates(db, co_id)[TEMPLATE_OF_STAGE[stage]])
        if isinstance(subject_override, str):
            tpl["subject"] = subject_override
        if isinstance(body_override, str) and body_override.strip():
            tpl["body"] = body_override
        fields, subject, text_body = merge_case_letter(db, c, letters, rule, company, tpl, stage, issue_date)
        rejoined_on = fresh_rejoin_date(db, c) if stage != "STRUCK_OFF" else None
        block = letter_block(c, letters, rule, stage, issue_date, rejoined_on)
        reply_days = reply_days_for(stage, rule)
        letter_no = LETTER_NO_OF_STAGE.get(stage)
        return {
            "data": {
                "case_id": c.case_id,
                "case_no": c.case_no,
                "stage_code": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "template_code": TEMPLATE_OF_STAGE[stage],
                "template_label": template_label(TEMPLATE_OF_STAGE[stage]),
                "letter_no": letter_no,
                "issue_date": issue_date.isoformat(),
                "reply_days": reply_days or None,
                "reply_due_date": _iso(issue_date + timedelta(days=reply_days)) if reply_days else None,
                "due_date": _iso(letter_due_date(c, letters, rule, stage)),
                "dispatch_mode": DEFAULT_DISPATCH.get(stage),
                "subject": subject,
                "body": text_body,
                # the issued letter is this text, merged again at issue: never edited by hand
                "editable": False,
                "fields": fields,
                "blocked_reason": block["reason"] if block else None,
                "blocked_code": block["code"] if block else None,
                "can_force": level >= LEVEL_EDIT and (block is None or bool(block["forceable"])),
            }
        }
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letter_preview", e)


@router.post("/letter_issue")
def la_letter_issue(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    stage = req_code(body, "stage_code", LETTER_STAGES)
    # "subject" / "body" may still be sent by an older client: ignored, the
    # letter is always the company's template merged here (owner 2026-10-11).
    today = today_ist()
    issue_date = parse_date(body.get("issue_date"), "issue_date") or today
    if issue_date > today:
        raise HTTPException(status_code=400, detail="issue_date cannot be in the future")
    dispatch_mode = _opt_code(body, "dispatch_mode", DISPATCH_MODES)
    force = opt_bool(body, "force")
    force_reason = opt_text(body, "force_reason", MAX_NOTE)
    if force and not force_reason:
        raise HTTPException(status_code=400, detail="force_reason is required to force a letter")
    user_id = _user_id(token_data)
    try:
        # First statement of the transaction: serialise letters on this case
        # (a double click / a batch running at the same time).
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        if stage != "STRUCK_OFF":
            require_open(c)
        letters = fetch_case_letters(db, case_id)
        dup = letters_by_stage(letters).get(stage)
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"The {STAGE_LABELS.get(stage, stage)} has already been issued for this case "
                       f"(letter dated {fmt_dmy(dup.get('issue_date'))})",
            )
        rule = load_rule(db, co_id)
        rejoined_on = fresh_rejoin_date(db, c) if stage != "STRUCK_OFF" else None
        block = letter_block(c, letters, rule, stage, issue_date, rejoined_on)
        forced = False
        if block is not None:
            if not (force and block["forceable"]):
                raise HTTPException(status_code=400, detail=block["reason"])
            forced = True
        check_issue_date_order(c, letters, stage, issue_date)
        template = load_templates(db, co_id)[TEMPLATE_OF_STAGE[stage]]
        _, subject, text_body = merge_case_letter(db, c, letters, rule, load_company(db, c.branch_id), template,
                                                  stage, issue_date)
        letter = _insert_letter(db, c, stage, subject[:MAX_SUBJECT] or None, text_body, issue_date,
                                dispatch_mode, rule, user_id, forced, None)
        _advance_case(c, letter, stage, issue_date, rule, user_id)
        add_event(
            db, c.case_id, "STAGE",
            _stage_event_text(stage, issue_date, force_reason=force_reason if forced else None),
            user_id, event_date=issue_date, ref_letter_id=letter.letter_id,
        )
        db.commit()
        return {"data": {"letter": letter_to_dict(letter), "case": case_out(c, today)}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letter_issue", e)


@router.post("/letter_issue_batch")
def la_letter_issue_batch(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    branch_id = req_int(body, "branch_id")
    raw_ids = body.get("case_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=400, detail="case_ids is required (a non-empty list)")
    case_ids = []
    for v in raw_ids:
        try:
            cid = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid case_ids format")
        if cid not in case_ids:
            case_ids.append(cid)
    if len(case_ids) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"At most {MAX_BATCH} cases per batch")
    today = today_ist()
    issue_date = parse_date(body.get("issue_date"), "issue_date") or today
    if issue_date > today:
        raise HTTPException(status_code=400, detail="issue_date cannot be in the future")
    dispatch_mode = _opt_code(body, "dispatch_mode", DISPATCH_MODES)
    user_id = _user_id(token_data)
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_EDIT)
        rule = load_rule(db, co_id)
        company = load_company(db, branch_id)
        templates = load_templates(db, co_id)
        batch_ref = f"B{today:%Y%m%d}-{now_ist():%H%M}-{user_id}"[:MAX_BATCH_REF]
        issued, skipped = [], []
        for cid in case_ids:
            locked = db.execute(lock_case_query(), {"case_id": cid}).first()
            c = db.get(JuteProdAbsentCase, cid)
            if (
                c is None or int(c.active or 0) != 1 or int(c.co_id) != co_id
                or int(c.branch_id) != branch_id or locked is None
            ):
                skipped.append({"case_id": cid, "reason": "Case not found in this branch"})
                continue
            lm = locked._mapping
            if lm["stage_code"] != c.stage_code or int(lm["status"]) != int(c.status):
                skipped.append({"case_id": cid, "reason": "The case was changed by another user — reload"})
                continue
            if c.rejoined_seen_date is not None:
                skipped.append({
                    "case_id": cid,
                    "reason": f"Marked present on {fmt_dmy(c.rejoined_seen_date)} — review the case first",
                })
                continue
            if c.stage_code == "STRUCK_OFF":
                skipped.append({"case_id": cid, "reason": "The struck-off memo is issued from the case page only"})
                continue
            stage = default_letter_stage(c)
            if stage is None:
                skipped.append({"case_id": cid, "reason": "This case has no letter to issue"})
                continue
            letters = fetch_case_letters(db, cid)
            block = letter_block(c, letters, rule, stage, issue_date, fresh_rejoin_date(db, c))
            if block is not None:
                skipped.append({"case_id": cid, "reason": block["reason"]})
                continue
            try:
                check_issue_date_order(c, letters, stage, issue_date)
            except HTTPException as he:
                skipped.append({"case_id": cid, "reason": he.detail})
                continue
            _, subject, text_body = merge_case_letter(db, c, letters, rule, company,
                                                      templates[TEMPLATE_OF_STAGE[stage]], stage, issue_date)
            letter = _insert_letter(db, c, stage, subject[:MAX_SUBJECT] or None, text_body, issue_date,
                                    dispatch_mode, rule, user_id, False, batch_ref)
            _advance_case(c, letter, stage, issue_date, rule, user_id)
            add_event(db, c.case_id, "STAGE", _stage_event_text(stage, issue_date, batch_ref=batch_ref),
                      user_id, event_date=issue_date, ref_letter_id=letter.letter_id)
            issued.append({
                "case_id": cid,
                "letter_id": letter.letter_id,
                "stage_code": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "letter_no": letter.letter_no,
                "case_no": c.case_no,
                "emp_code": c.emp_code,
                "emp_name": c.emp_name,
                "issue_date": issue_date.isoformat(),
                "subject": letter.subject,
                "body": letter.body,
            })
        db.commit()
        return {
            "data": {
                "batch_ref": batch_ref,
                "issue_date": issue_date.isoformat(),
                "issued": issued,
                "skipped": skipped,
            }
        }
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letter_issue_batch", e)


@router.post("/letter_update")
def la_letter_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    letter_id = req_int(body, "letter_id")
    changes = {}
    for key in LETTER_UPDATE_DATE_FIELDS:
        if key in body:
            changes[key] = parse_date(body.get(key), key)
    if "dispatch_mode" in body:
        changes["dispatch_mode"] = _opt_code(body, "dispatch_mode", DISPATCH_MODES)
    if "delivery_status" in body:
        changes["delivery_status"] = _opt_code(body, "delivery_status", DELIVERY_STATUSES)
    if "ad_no" in body:
        changes["ad_no"] = opt_text(body, "ad_no", MAX_AD_NO)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update")
    today = today_ist()
    user_id = _user_id(token_data)
    try:
        l = load_letter(db, letter_id)
        c = load_case(db, co_id, l.case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        for key in LETTER_UPDATE_DATE_FIELDS:
            d = changes.get(key)
            if d is None:
                continue
            if d > today:
                raise HTTPException(status_code=400, detail=f"{key} cannot be in the future")
            if d < l.issue_date:
                raise HTTPException(
                    status_code=400, detail=f"{key} cannot be before the letter date ({fmt_dmy(l.issue_date)})"
                )
        sent_after = changes["sent_date"] if "sent_date" in changes else l.sent_date
        delivered = changes["delivery_date"] if "delivery_date" in changes else l.delivery_date
        if sent_after is not None and delivered is not None and delivered < sent_after:
            raise HTTPException(status_code=400, detail="delivery_date cannot be before sent_date")
        old_sent = l.sent_date
        for key, v in changes.items():
            setattr(l, key, v)
        l.updated_by = user_id
        if "sent_date" in changes and changes["sent_date"] != old_sent:
            if l.stage_code == c.stage_code and int(c.status) != STATUS_CLOSED:
                c.next_due_date = due_date_for_next(c, l, load_rule(db, co_id))
                c.updated_by = user_id
            label = STAGE_LABELS.get(l.stage_code, l.stage_code)
            if l.sent_date is not None:
                ev = f"{label} sent on {fmt_dmy(l.sent_date)}"
                if l.ad_no:
                    ev += f" (A.D. No. {l.ad_no})"
            else:
                ev = f"{label}: sent date cleared"
            add_event(db, c.case_id, "SYSTEM", ev[:MAX_TEXT], user_id,
                      event_date=l.sent_date or today, ref_letter_id=l.letter_id)
        db.commit()
        return {"data": {"letter": letter_to_dict(l), "case": case_out(c, today)}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letter_update", e)


@router.get("/dispatch")
def la_dispatch(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    today = today_ist()
    date_to = parse_date(q.get("date_to"), "date_to") or today
    date_from = parse_date(q.get("date_from"), "date_from") or (date_to - timedelta(days=DISPATCH_DEFAULT_DAYS))
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from cannot be after date_to")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"The range may cover at most {MAX_RANGE_DAYS} days")
    batch_ref = opt_text(q, "batch_ref", MAX_BATCH_REF)
    stage_code = _opt_code(q, "stage_code", LETTER_STAGES)
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        rows = _rows(db, get_dispatch_query(), {
            "co_id": co_id, "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
            "batch_ref": batch_ref, "stage_code": stage_code,
        })
        return {"data": [shape_letter_row(m) for m in rows]}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "dispatch", e)


# =============================================================================
# Reprint, PDF and letters issued outside VoWERP (owner 2026-10-12)
# =============================================================================


def parse_letter_ids(raw) -> list:
    """``letter_ids=1,2,3`` -> [1, 2, 3] in the given order, repeats dropped.
    400 when blank, not positive integers or more than MAX_PRINT_LETTERS."""
    if raw is None or str(raw).strip() == "":
        raise HTTPException(status_code=400, detail="letter_ids is required")
    ids: list = []
    seen: set = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part == "":
            continue
        try:
            v = int(part)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid letter_ids format")
        if v <= 0:
            raise HTTPException(status_code=400, detail="Invalid letter_ids format")
        if v not in seen:
            seen.add(v)
            ids.append(v)
    if not ids:
        raise HTTPException(status_code=400, detail="letter_ids is required")
    if len(ids) > MAX_PRINT_LETTERS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_PRINT_LETTERS} letters at a time")
    return ids


def shape_print_letter(m: dict) -> dict:
    """A letter as /letters_print returns it (what a reprint needs)."""
    stage = m.get("stage_code")
    return {
        "letter_id": int(m["letter_id"]),
        "case_id": int(m["case_id"]),
        "branch_id": int(m["branch_id"]) if m.get("branch_id") is not None else None,
        "case_no": m.get("case_no"),
        "emp_code": m.get("emp_code"),
        "emp_name": m.get("emp_name"),
        "stage_code": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "subject": m.get("subject"),
        "body": m.get("body") or "",
        "issue_date": _iso(m.get("issue_date")),
        "sent_date": _iso(m.get("sent_date")),
        "recorded": int(m.get("recorded") or 0),
    }


def load_print_letters(db: Session, token_data: dict, co_id: int, letter_ids: list) -> list:
    """The requested letters in the requested order. 404 when one is unknown,
    inactive, of an inactive case or of another company; then view access on
    every branch the letters belong to (403 on the first one missing)."""
    rows = {
        int(m["letter_id"]): m
        for m in _rows(db, get_letters_by_ids_query(), {"co_id": int(co_id), "letter_ids": list(letter_ids)})
    }
    missing = [i for i in letter_ids if i not in rows]
    if missing:
        shown = ", ".join(str(i) for i in missing[:10]) + (" …" if len(missing) > 10 else "")
        raise HTTPException(status_code=404, detail=f"Letter not found: {shown}")
    for branch_id in sorted({int(m["branch_id"]) for m in rows.values()}):
        lac.require_access(db, token_data, branch_id, LEVEL_VIEW)
    return [shape_print_letter(rows[i]) for i in letter_ids]


def load_pdf_letterhead(db: Session, branch_ids: set) -> dict:
    """Letterhead row of the PDF (get_pdf_letterhead_query) for the letters'
    branch; with letters of several branches the company part only (the branch
    GSTIN is left out)."""
    ids = sorted(int(b) for b in branch_ids if b is not None)
    if not ids:
        return {}
    row = db.execute(get_pdf_letterhead_query(), {"branch_id": ids[0]}).first()
    company = dict(row._mapping) if row is not None else {}
    if len(ids) > 1:
        company["gst_no"] = None
    return company


@router.get("/letters_print")
def la_letters_print(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Bulk reprint (Dispatch tab): the stored subject / body of the given
    letters, in the requested order. View access on each letter's branch."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    letter_ids = parse_letter_ids(q.get("letter_ids"))
    try:
        return {"data": load_print_letters(db, token_data, co_id, letter_ids)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letters_print", e)


@router.get("/letters_pdf")
def la_letters_pdf(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The given letters as one A4 PDF (one letter per page, letterhead,
    stored text) — long_absenteeism_pdf.build_letters_pdf. Same access as
    /letters_print."""
    q = request.query_params
    co_id = req_int(q, "co_id")
    letter_ids = parse_letter_ids(q.get("letter_ids"))
    try:
        letters = load_print_letters(db, token_data, co_id, letter_ids)
        company = load_pdf_letterhead(db, {l["branch_id"] for l in letters})
        pdf = build_letters_pdf(letters, company)
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letters_pdf", e)
    filename = letters_pdf_filename(today_ist())
    return StreamingResponse(
        BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def record_stage_block(case) -> Optional[str]:
    """Why no letter can be recorded on the case now, else None (the stage
    itself is checked against next_stage_of by the caller)."""
    status = int(_get(case, "status"))
    if status != STATUS_OPEN:
        return (
            "This case is parked (reply under review) — resume it first" if status == STATUS_PARKED
            else "This case is closed — reopen it to change it"
        )
    return None


def record_order_error(case, stage: str) -> Optional[str]:
    """None when ``stage`` is the case's next letter, else the reason (letters
    are recorded in order; the memo follows the approval, never recorded)."""
    if stage == "STRUCK_OFF":
        return "The struck-off memo follows the struck-off approval — it cannot be recorded"
    cur = _get(case, "stage_code")
    nxt = next_stage_of(case)
    if nxt == stage:
        return None
    cur_label = STAGE_LABELS.get(cur, cur)
    if nxt is None:
        return f"No letter can be recorded for this case (it is at the {cur_label} stage)"
    if nxt == "STRUCK_OFF":
        return "The gate notice is the last letter — the case goes for struck-off approval next"
    return (
        f"The next letter of this case is the {STAGE_LABELS.get(nxt, nxt)} (the case is at {cur_label}) — "
        "record the letters in order"
    )


def recorded_event_text(stage: str, issue_date: date, sent_date: Optional[date], note: Optional[str]) -> str:
    """'1st warning recorded as issued 05.08.2026, sent 06.08.2026 (back-dated entry): <note>'."""
    s = f"{STAGE_LABELS.get(stage, stage)} recorded as issued {fmt_dmy(issue_date)}"
    if sent_date is not None:
        s += f", sent {fmt_dmy(sent_date)}"
    s += " (back-dated entry)"
    if note:
        s += f": {note}"
    return s[:MAX_TEXT]


@router.post("/letter_record")
def la_letter_record(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Enter a letter that was already issued outside VoWERP (back-dated entry,
    owner 2026-10-12). The case must be open and ``stage_code`` its NEXT stage
    (order enforced, no wait / previous-sent-date gate: these are historical
    facts). ``issue_date`` <= today, >= the last working date and >= the
    previous letter's date; ``sent_date`` between issue_date and today. The
    text is the company's template for the stage merged now (so the record
    carries it); recorded = 1, forced = 0; the case advances exactly as
    /letter_issue does."""
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    stage = req_code(body, "stage_code", LETTER_STAGES)
    today = today_ist()
    issue_date = parse_date(body.get("issue_date"), "issue_date", required=True)
    if issue_date > today:
        raise HTTPException(status_code=400, detail="issue_date cannot be in the future")
    sent_date = parse_date(body.get("sent_date"), "sent_date")
    if sent_date is not None:
        if sent_date > today:
            raise HTTPException(status_code=400, detail="sent_date cannot be in the future")
        if sent_date < issue_date:
            raise HTTPException(
                status_code=400, detail=f"sent_date cannot be before the letter date ({fmt_dmy(issue_date)})"
            )
    delivery_date = parse_date(body.get("delivery_date"), "delivery_date")
    if delivery_date is not None:
        if delivery_date > today:
            raise HTTPException(status_code=400, detail="delivery_date cannot be in the future")
        if delivery_date < (sent_date or issue_date):
            raise HTTPException(
                status_code=400,
                detail="delivery_date cannot be before sent_date" if sent_date
                else f"delivery_date cannot be before the letter date ({fmt_dmy(issue_date)})",
            )
    dispatch_mode = _opt_code(body, "dispatch_mode", DISPATCH_MODES)
    delivery_status = _opt_code(body, "delivery_status", DELIVERY_STATUSES)
    ad_no = opt_text(body, "ad_no", MAX_AD_NO)
    note = opt_text(body, "note", MAX_NOTE)
    if stage == "STRUCK_OFF":
        raise HTTPException(status_code=400, detail=record_order_error(None, stage))
    user_id = _user_id(token_data)
    try:
        # First statement: serialise with letter issue / batch on the same case.
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        blocked = record_stage_block(c) or record_order_error(c, stage)
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)
        letters = fetch_case_letters(db, case_id)
        dup = letters_by_stage(letters).get(stage)
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"The {STAGE_LABELS.get(stage, stage)} has already been issued for this case "
                       f"(letter dated {fmt_dmy(dup.get('issue_date'))})",
            )
        check_issue_date_order(c, letters, stage, issue_date)
        rule = load_rule(db, co_id)
        template = load_templates(db, co_id)[TEMPLATE_OF_STAGE[stage]]
        _, subject, text_body = merge_case_letter(db, c, letters, rule, load_company(db, c.branch_id), template,
                                                  stage, issue_date)
        letter = _insert_letter(
            db, c, stage, subject[:MAX_SUBJECT] or None, text_body, issue_date, dispatch_mode, rule, user_id,
            False, None,
            sent_date=sent_date, ad_no=ad_no, delivery_status=delivery_status, delivery_date=delivery_date,
            recorded=1,
        )
        _advance_case(c, letter, stage, issue_date, rule, user_id)
        add_event(
            db, c.case_id, "STAGE", recorded_event_text(stage, issue_date, sent_date, note),
            user_id, event_date=issue_date, ref_letter_id=letter.letter_id,
        )
        db.commit()
        out = letter_to_dict(letter)
        out["recorded"] = 1
        return {"data": {"letter": out, "case": case_out(c, today)}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "letter_record", e)


# =============================================================================
# Union receipts (gate notice)
# =============================================================================


def _receipt_letter_and_case(db: Session, co_id: int, letter_id: int):
    l = load_letter(db, letter_id)
    c = load_case(db, co_id, l.case_id)
    return l, c


def _require_receipt_target(l: JuteProdAbsentLetter, c: JuteProdAbsentCase) -> None:
    if l.stage_code != "GATE_NOTICE":
        raise HTTPException(status_code=400, detail="Union receipts belong to the gate notice")
    if int(c.status) == STATUS_CLOSED and c.exit_reason != "STRUCK_OFF":
        raise HTTPException(status_code=400, detail="This case is closed — reopen it to change it")


def _received_date(body: dict, l: JuteProdAbsentLetter, today: date) -> Optional[date]:
    d = parse_date(body.get("received_date"), "received_date")
    if d is not None:
        if d > today:
            raise HTTPException(status_code=400, detail="received_date cannot be in the future")
        if d < l.issue_date:
            raise HTTPException(
                status_code=400,
                detail=f"received_date cannot be before the gate notice date ({fmt_dmy(l.issue_date)})",
            )
    return d


def _receipt_out(r: JuteProdAbsentUnionReceipt) -> dict:
    d = model_to_dict(r)
    d["attachment"] = None
    return d


@router.post("/receipt_create")
def la_receipt_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    letter_id = req_int(body, "letter_id")
    union_name = opt_text(body, "union_name", MAX_UNION)
    if not union_name:
        raise HTTPException(status_code=400, detail="union_name is required")
    received_by = opt_text(body, "received_by", MAX_RECEIVED_BY)
    remarks = opt_text(body, "remarks", MAX_RECEIPT_REMARKS)
    try:
        l, c = _receipt_letter_and_case(db, co_id, letter_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        _require_receipt_target(l, c)
        received_date = _received_date(body, l, today_ist())
        r = JuteProdAbsentUnionReceipt(
            letter_id=l.letter_id, union_name=union_name, received_by=received_by,
            received_date=received_date, remarks=remarks, active=1, updated_by=_user_id(token_data),
        )
        db.add(r)
        db.flush()
        db.commit()
        return {"data": _receipt_out(r)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "receipt_create", e)


def _load_receipt(db: Session, co_id: int, receipt_id: int):
    r = db.get(JuteProdAbsentUnionReceipt, int(receipt_id))
    if r is None or int(r.active or 0) != 1:
        raise HTTPException(status_code=404, detail="Union receipt not found")
    l, c = _receipt_letter_and_case(db, co_id, r.letter_id)
    return r, l, c


@router.post("/receipt_update")
def la_receipt_update(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    receipt_id = req_int(body, "receipt_id")
    changes = {}
    if "union_name" in body:
        changes["union_name"] = opt_text(body, "union_name", MAX_UNION)
        if not changes["union_name"]:
            raise HTTPException(status_code=400, detail="union_name is required")
    if "received_by" in body:
        changes["received_by"] = opt_text(body, "received_by", MAX_RECEIVED_BY)
    if "remarks" in body:
        changes["remarks"] = opt_text(body, "remarks", MAX_RECEIPT_REMARKS)
    if not changes and "received_date" not in body:
        raise HTTPException(status_code=400, detail="Nothing to update")
    try:
        r, l, c = _load_receipt(db, co_id, receipt_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        _require_receipt_target(l, c)
        if "received_date" in body:
            changes["received_date"] = _received_date(body, l, today_ist())
        for key, v in changes.items():
            setattr(r, key, v)
        r.updated_by = _user_id(token_data)
        db.commit()
        return {"data": _receipt_out(r)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "receipt_update", e)


@router.post("/receipt_deactivate")
def la_receipt_deactivate(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    receipt_id = req_int(body, "receipt_id")
    try:
        r, l, c = _load_receipt(db, co_id, receipt_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        _require_receipt_target(l, c)
        r.active = 0
        r.updated_by = _user_id(token_data)
        db.commit()
        return {"data": {"receipt_id": receipt_id, "active": 0}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "receipt_deactivate", e)


@router.get("/union_names")
def la_union_names(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        rows = _rows(db, get_union_names_query(), {"co_id": co_id, "branch_id": branch_id})
        return {"data": [m["union_name"] for m in rows if m.get("union_name")]}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "union_names", e)


# =============================================================================
# Struck-off approval (standard engine)
# =============================================================================


def _engine_menu_id(db: Session) -> int:
    mid = lac.menu_id(db)
    if mid is None:
        raise HTTPException(status_code=400, detail="Long Absenteeism menu not found")
    return mid


_LIFECYCLE_LABELS = {v: k.replace("_", " ").title() for k, v in EMPLOYEE_LIFECYCLE_STATUS.items()}


def _require_not_separated(db: Session, eb_id: int) -> None:
    """400 when HR has already recorded a separation (resigned / terminated /
    retired / blacklisted) for the worker: struck off must not overwrite it."""
    row = db.execute(get_employee_status_query(), {"eb_id": int(eb_id)}).first()
    raw = row._mapping.get("status_id") if row is not None else None
    try:
        sid = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        sid = None
    if sid in RESIGN_DETAIL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Already separated in HRMS (status {_LIFECYCLE_LABELS.get(sid, sid)}) — exit the case instead",
        )


def _require_not_rejoined(db: Session, c: JuteProdAbsentCase) -> None:
    seen = fresh_rejoin_date(db, c)
    if seen is not None:
        raise HTTPException(status_code=400, detail=rejoined_message(seen))


@router.post("/struck_off/send")
def la_struck_off_send(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    user_id = _user_id(token_data)
    try:
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        today = today_ist()
        reason = struck_off_send_block(
            c, _gate_letter(db, case_id), today, load_rule(db, co_id)["notice_reply_days"],
            fresh_rejoin_date(db, c),
        )
        if reason:
            raise HTTPException(status_code=400, detail=reason)
        c.status_id = SO_PENDING
        c.approval_level = 1
        c.updated_by = user_id
        add_event(db, c.case_id, "APPROVAL", "Sent for struck-off approval", user_id)
        db.commit()
        return {"data": {"case": case_out(c, today)}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "struck_off/send", e)


@router.post("/struck_off/approve")
def la_struck_off_approve(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    today = today_ist()
    so_date = parse_date(body.get("struck_off_date"), "struck_off_date") or today
    if so_date > today:
        raise HTTPException(status_code=400, detail="struck_off_date cannot be in the future")
    user_id = _user_id(token_data)
    try:
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        require_live(c)
        if c.status_id != SO_PENDING:
            raise HTTPException(status_code=400, detail="This case is not pending struck-off approval")
        mid = _engine_menu_id(db)
        _require_not_rejoined(db, c)
        gate = _gate_letter(db, case_id)
        gate_due = gate_notice_due(gate, load_rule(db, co_id)["notice_reply_days"]) if gate else None
        if gate_due is not None and so_date <= gate_due:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"struck_off_date must be after the gate notice reply window (it runs to "
                    f"{fmt_dmy(gate_due)}) — struck off can be dated from {fmt_dmy(gate_due + timedelta(days=1))}"
                ),
            )
        level_before = c.approval_level or 1
        result = process_approval(
            doc_id=int(case_id),
            user_id=int(user_id),
            menu_id=mid,
            db=db,
            get_doc_fn=get_case_approval_doc_query,
            update_status_fn=update_case_approval_status_query,
            id_param_name="case_id",
            doc_name="Struck-off approval",
            commit=False,
        )
        if result["new_status_id"] == SO_APPROVED:
            # HR may have separated the worker meanwhile: never overwrite that
            # (the HTTPException rolls the engine's status update back).
            _require_not_separated(db, c.eb_id)
        c.status_id = result["new_status_id"]
        c.approval_level = result["new_approval_level"]
        c.updated_by = user_id
        if result["new_status_id"] == SO_APPROVED:
            c.stage_code = "STRUCK_OFF"
            c.stage_date = so_date
            c.struck_off_date = so_date
            c.status = STATUS_CLOSED
            c.exit_reason = "STRUCK_OFF"
            c.exit_date = so_date
            c.next_due_date = None
            apply_lifecycle_status(
                db, c.eb_id, TERMINATED_STATUS_ID, so_date, STRUCK_OFF_REASON, f"Case {c.case_no}", user_id,
            )
            add_event(db, c.case_id, "APPROVAL", "Struck off approved; employee marked Terminated", user_id,
                      event_date=so_date)
        else:
            add_event(db, c.case_id, "APPROVAL", f"Approved at level {level_before}", user_id)
        db.commit()
        return {"data": {"case": case_out(c, today), "approval": result}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "struck_off/approve", e)


@router.post("/struck_off/reject")
def la_struck_off_reject(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    reason = opt_text(body, "reason", MAX_NOTE)
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    user_id = _user_id(token_data)
    try:
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        if c.status_id != SO_PENDING:
            raise HTTPException(status_code=400, detail="This case is not pending struck-off approval")
        mid = _engine_menu_id(db)
        # Added first: process_rejection commits the session (event + status
        # together) and rolls back both if the caller may not reject.
        add_event(db, c.case_id, "APPROVAL", f"Struck-off rejected: {reason}"[:MAX_TEXT], user_id)
        result = process_rejection(
            doc_id=int(case_id),
            user_id=int(user_id),
            menu_id=mid,
            db=db,
            get_doc_fn=get_case_approval_doc_query,
            update_status_fn=update_case_approval_status_query,
            id_param_name="case_id",
            doc_name="Struck-off approval",
            reason=reason,
        )
        c.status_id = result["new_status_id"]
        c.approval_level = result["new_approval_level"]
        return {"data": {"case": case_out(c, today_ist()), "approval": result}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "struck_off/reject", e)


@router.post("/struck_off/reopen")
def la_struck_off_reopen(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    body = json_object(request)
    co_id = req_int(body, "co_id")
    case_id = req_int(body, "case_id")
    user_id = _user_id(token_data)
    try:
        db.execute(lock_case_query(), {"case_id": case_id})
        c = load_case(db, co_id, case_id)
        lac.require_access(db, token_data, c.branch_id, LEVEL_EDIT)
        if c.status_id != SO_REJECTED:
            raise HTTPException(status_code=400, detail="Only a rejected struck-off request can be reopened")
        c.status_id = None
        c.approval_level = None
        c.updated_by = user_id
        add_event(db, c.case_id, "APPROVAL", "Rejected struck-off request reopened; it can be sent again", user_id)
        db.commit()
        return {"data": {"case": case_out(c, today_ist())}}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "struck_off/reopen", e)


@router.get("/struck_off/permissions")
def la_struck_off_permissions(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    case_id = req_int(q, "case_id")
    user_id = _user_id(token_data)
    try:
        c = load_case(db, co_id, case_id)
        level = require_level(db, token_data, c.branch_id, LEVEL_VIEW)
        mid = _engine_menu_id(db)
        status_id = c.status_id
        perms = calculate_approval_permissions(
            int(user_id), mid, int(c.branch_id), status_id if status_id is not None else 1, c.approval_level, db,
        )
        scope = {"menu_id": mid, "branch_id": int(c.branch_id)}
        hier_row = db.execute(check_approval_mst_exists(), scope).first()
        hierarchy_exists = bool(hier_row is not None and int(hier_row._mapping.get("count") or 0) > 0)
        max_level = None
        if hierarchy_exists:
            mx = db.execute(get_max_approval_level(), scope).first()
            max_level = mx._mapping.get("max_level") if mx is not None else None
        has_edit = level >= LEVEL_EDIT
        block = struck_off_send_block(
            c, _gate_letter(db, case_id), today_ist(), load_rule(db, co_id)["notice_reply_days"],
            fresh_rejoin_date(db, c),
        )
        can_send = has_edit and block is None
        if block is None and not has_edit:
            block = "You need edit access to send a case for struck-off approval"
        out = dict(perms)
        # The case is not an engine document before it is sent: no direct
        # approve from "open", no draft / save / clone buttons.
        out.update({"canSave": False, "canOpen": False, "canCancelDraft": False, "canClone": False})
        if status_id is None:
            out.update({"canApprove": False, "canReject": False})
        out["canSendForApproval"] = can_send
        out["canReopen"] = status_id == SO_REJECTED and has_edit
        out.update({
            "status_id": status_id,
            "approval_level": c.approval_level,
            "max_approval_level": max_level,
            "hierarchy_exists": hierarchy_exists,
            "can_send": can_send,
            "send_blocked_reason": None if can_send else block,
            "menu_id": mid,
        })
        return {"data": out}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "struck_off/permissions", e)


# =============================================================================
# Reports (view)
# =============================================================================


def _report_range(q, today: date, default_days: int) -> tuple:
    date_to = parse_date(q.get("date_to"), "date_to") or today
    date_from = parse_date(q.get("date_from"), "date_from") or (date_to - timedelta(days=default_days))
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from cannot be after date_to")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"The range may cover at most {MAX_RANGE_DAYS} days")
    return date_from, date_to


@router.get("/report/struck_off_register")
def la_report_struck_off_register(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    dept_id = opt_int(q, "dept_id")
    date_from, date_to = _report_range(q, today_ist(), 365)
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        rows = _rows(db, get_struck_off_register_query(), {
            "co_id": co_id, "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
            "dept_id": dept_id,
        })
        return {"data": [_iso_row(m) for m in rows]}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "report/struck_off_register", e)


@router.get("/report/stage_summary")
def la_report_stage_summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    as_on = parse_date(q.get("as_on"), "as_on") or today_ist()
    exits_from = as_on - timedelta(days=EXITS_WINDOW_DAYS - 1)
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        scope = {"co_id": co_id, "branch_id": branch_id}
        live = _rows(db, get_stage_summary_live_query(), scope)
        exits = _rows(db, get_stage_summary_exits_query(), {**scope, "date_from": exits_from, "as_on": as_on})
        out = shape_stage_summary(live, exits)
        out["as_on"] = as_on.isoformat()
        out["exits_from"] = exits_from.isoformat()
        return {"data": out}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "report/stage_summary", e)


@router.get("/report/monthly_trend")
def la_report_monthly_trend(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    q = request.query_params
    co_id = req_int(q, "co_id")
    branch_id = req_int(q, "branch_id")
    months = opt_int(q, "months")
    months = 12 if months is None else months
    if months < 1 or months > MAX_TREND_MONTHS:
        raise HTTPException(status_code=400, detail=f"months must be between 1 and {MAX_TREND_MONTHS}")
    today = today_ist()
    keys = month_keys(today, months)
    date_from = date.fromisoformat(f"{keys[0]}-01")
    try:
        lac.require_branch_access(db, token_data, co_id, branch_id, LEVEL_VIEW)
        rows = _rows(db, get_monthly_trend_query(), {
            "co_id": co_id, "branch_id": branch_id, "date_from": date_from, "date_to": today,
        })
        return {"data": build_monthly_trend(rows, keys)}
    except HTTPException:
        rollback(db)
        raise
    except Exception as e:
        raise _internal_error(db, "report/monthly_trend", e)
