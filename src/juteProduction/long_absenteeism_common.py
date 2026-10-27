"""Long Absenteeism follow-up — shared codes, defaults, merge and access helpers.

Imported by both routers (long_absenteeism.py = cases / events / candidates,
long_absenteeism_letters.py = templates / letters / dispatch / approval / reports)
so neither depends on the other. Pure functions here have no DB or request
dependency and are unit-tested without a DB.

Spec: docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md
Pattern: src/juteProduction/low_performer_followup.py (access, parsing, errors).
"""

import re
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException, Request
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.common.approval_query import get_user_edit_access
from src.common.utils import now_ist, parse_json_body
from src.juteProduction.long_absenteeism_models import (
    JuteProdAbsentCase,
    JuteProdAbsentEvent,
    JuteProdAbsentLetter,
    JuteProdAbsentRule,
)

class TtlCache:
    """Tiny process-local cache with a TTL, keyed by hashable tuples. Thread-safe.

    Why (2026-10-12): the tenant MySQL is remote — one round trip costs ~63 ms —
    so every query a request can avoid is felt directly. Entries are small and
    short-lived; ``invalidate(prefix)`` drops every key starting with ``prefix``
    (used after writes that change what a cached read would return)."""

    def __init__(self, ttl_seconds: float):
        self.ttl = float(ttl_seconds)
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            exp, value = hit
            if exp < now:
                self._d.pop(key, None)
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            self._d[key] = (time.monotonic() + self.ttl, value)

    def invalidate(self, prefix: tuple) -> None:
        n = len(prefix)
        with self._lock:
            for k in [k for k in self._d if k[:n] == prefix]:
                self._d.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


def db_name(db: Session) -> str:
    """The tenant database this session talks to (cache key component). Empty for
    anything that is not a real engine (MagicMock sessions in tests), which turns
    every cache off for that session."""
    try:
        name = db.get_bind().url.database
    except Exception:  # pragma: no cover - mocked sessions in tests
        return ""
    return name if isinstance(name, str) else ""


from src.juteProduction.long_absenteeism_query import get_rejoin_query

# =============================================================================
# Codes (labels are served by /setup so the UI never hardcodes them twice)
# =============================================================================

# The page lives in the HRMS module (owner 2026-10-12, migration
# 20261012_long_absenteeism_menu_to_hrms.sql); MENU_PATH names it in messages /
# docs. menu_id() resolves the first ACTIVE menu row of MENU_PATHS in this order,
# so the access checks keep working on a tenant whose menu row has not moved yet.
MENU_PATH = "hrms/longAbsenteeism"
LEGACY_MENU_PATH = "juteProduction/report/longAbsenteeism"
MENU_PATHS = (MENU_PATH, LEGACY_MENU_PATH)
LEVEL_VIEW = 1
LEVEL_CREATE = 3
LEVEL_EDIT = 4
_LEVEL_NAMES = {LEVEL_VIEW: "view", LEVEL_CREATE: "create", LEVEL_EDIT: "edit"}

# Ladder, in order. stage_code on the case = the LAST step completed.
STAGES = [
    ("DETECTED", "Detected"),
    ("VERIFIED", "Verified"),
    ("WARNING_1", "1st warning"),
    ("WARNING_2", "2nd warning"),
    ("WARNING_3", "3rd & last warning"),
    ("SHOW_CAUSE", "Show cause"),
    ("GATE_NOTICE", "Gate notice"),
    ("STRUCK_OFF", "Struck off"),
]
STAGE_LABELS = dict(STAGES)
STAGE_ORDER = {code: i for i, (code, _) in enumerate(STAGES)}

# Which letter is issued to move INTO a stage, and which template it uses.
LETTER_STAGES = ["WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE", "STRUCK_OFF"]
TEMPLATE_OF_STAGE = {
    "WARNING_1": "WARNING_1",
    "WARNING_2": "WARNING_2",
    "WARNING_3": "WARNING_3",
    "SHOW_CAUSE": "SHOW_CAUSE",
    "GATE_NOTICE": "GATE_NOTICE",
    "STRUCK_OFF": "STRUCK_OFF_MEMO",
}
LETTER_NO_OF_STAGE = {"WARNING_1": 1, "WARNING_2": 2, "WARNING_3": 3}
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}
# The stage a letter may be issued FROM (the case's current stage_code).
PREVIOUS_STAGE = {
    "WARNING_1": "VERIFIED",
    "WARNING_2": "WARNING_1",
    "WARNING_3": "WARNING_2",
    "SHOW_CAUSE": "WARNING_3",
    "GATE_NOTICE": "SHOW_CAUSE",
    "STRUCK_OFF": "GATE_NOTICE",
}

# Owner 2026-10-11 (later): one wording per warning (mild / firmer / last and
# final) instead of one numbered WARNING text.
TEMPLATE_CODES = [
    ("WARNING_1", "1st warning letter"),
    ("WARNING_2", "2nd warning letter"),
    ("WARNING_3", "3rd & last warning letter"),
    ("SHOW_CAUSE", "Show cause letter"),
    ("GATE_NOTICE", "Gate notice (final)"),
    ("STRUCK_OFF_MEMO", "Struck-off memo"),
]
TEMPLATE_LABELS = dict(TEMPLATE_CODES)
# Retired codes: only labelled on letters issued before the split, never
# accepted by /template_save or /template_reset.
LEGACY_TEMPLATE_LABELS = {"WARNING": "Warning letter (1st / 2nd / 3rd)"}


def template_label(code: Optional[str]) -> Optional[str]:
    """Label of a template code, retired codes included (the code itself when unknown)."""
    return TEMPLATE_LABELS.get(code or "") or LEGACY_TEMPLATE_LABELS.get(code or "", code)

STATUS_OPEN = 1
STATUS_PARKED = 2
STATUS_CLOSED = 0
STATUS_LABELS = {STATUS_OPEN: "Open", STATUS_PARKED: "Parked", STATUS_CLOSED: "Closed"}

EXIT_REASONS = {
    "REJOINED": "Rejoined",
    "ESI": "On ESI",
    "STL": "On STL",
    "MEDICAL": "Medical report submitted",
    "MATERNITY": "Maternity leave",
    "LEAVE": "On leave",
    "RESIGNED": "Resigned",
    "EXPIRED": "Expired",
    "STRUCK_OFF": "Struck off",
    "DISMISSED": "Dismissed (not a case)",
    "OTHER": "Other",
}
# Exits the user may choose on /exit. STRUCK_OFF only via the approval flow,
# DISMISSED only via /dismiss (a candidate that never became a case).
USER_EXIT_REASONS = [k for k in EXIT_REASONS if k not in ("STRUCK_OFF", "DISMISSED")]

# /verify is kept for API compatibility only (owner 12.10.2026: no manual
# verification step — cases open at VERIFIED and the leave module is the source).
ESI_STL_RESULTS = {"NONE": "Not on ESI / STL", "ESI": "On ESI", "STL": "On STL"}

# Exits that hide the worker from Candidates while expected_return_date >= as_on
# (get_candidates_query's NOT EXISTS lists the same codes literally). LEAVE = a
# type of the HRMS leave master (leave_type_id / leave_type_name on the case);
# ESI / STL / MATERNITY are the legacy codes, offered only when no master type of
# the company stands for them (TKT-000223).
RETURN_DATE_EXIT_REASONS = ("ESI", "STL", "MATERNITY", "MEDICAL", "LEAVE")

# Legacy leave codes and the pattern that finds the company's master type standing
# for each (tested on the type's code and description, case-insensitive), in order.
LEGACY_LEAVE_CODES = (("ESI", r"\bESI\b"), ("STL", r"\bSTL\b"), ("MATERNITY", r"\bMATERN"))
LEAVE_TYPE_NAME_MAX = 100   # jute_prod_absent_case.leave_type_name


def _clean_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def leave_option_label(row) -> str:
    """Dropdown label of a leave master type (a dict / row mapping with
    leave_type_code, leave_type_description): "<description> (<code>)" when both
    are present and differ (case-insensitive), else whichever is present (stripped;
    the code as stored). "Leave type <id>" when the row has neither."""
    desc = _clean_str(row.get("leave_type_description"))
    code = _clean_str(row.get("leave_type_code"))
    if desc and code and desc.upper() != code.upper():
        return f"{desc} ({code})"
    if desc or code:
        return desc or code
    ltid = row.get("leave_type_id")
    return f"Leave type {ltid}" if ltid is not None else ""


def leave_type_name_of(row) -> Optional[str]:
    """The name a case keeps of a leave type (leave_type_name): the description, the
    code when the description is blank (the SQL rule of the leave-link queries),
    cut to the column width. None when the row has neither."""
    name = _clean_str(row.get("leave_type_description")) or _clean_str(row.get("leave_type_code"))
    return name[:LEAVE_TYPE_NAME_MAX] if name else None


def build_leave_options(leave_types: list) -> list[dict]:
    """The leave choices of a company for /verify and /exit.

    ``leave_types``: the company's active master types ({leave_type_id,
    leave_type_code, leave_type_description}), already ordered. Out, in order:
    one entry per master type {"code": "LEAVE", "leave_type_id", "label",
    "legacy_code", "source": "master"}, then one entry per legacy code that no
    master type stands for {"code": <legacy>, "leave_type_id": None, "label":
    EXIT_REASONS[<legacy>], "legacy_code": <legacy>, "source": "legacy"}.

    A legacy code (LEGACY_LEAVE_CODES, in order) is carried by the FIRST master
    type, in input order, whose code equals it (case-insensitive) or whose code /
    description matches its pattern; each legacy code goes to at most one type
    and each type carries at most one legacy code. sls company 2: STL LEAVE (L)
    carries STL, so the list ends with the ESI and MATERNITY legacy entries."""
    types = [t for t in (leave_types or []) if t.get("leave_type_id") is not None]
    carried: dict[int, str] = {}   # index in types -> legacy code
    for legacy, pattern in LEGACY_LEAVE_CODES:
        rx = re.compile(pattern, re.I)
        for i, t in enumerate(types):
            if i in carried:
                continue
            code = _clean_str(t.get("leave_type_code")) or ""
            desc = _clean_str(t.get("leave_type_description")) or ""
            if code.upper() == legacy or rx.search(code) or rx.search(desc):
                carried[i] = legacy
                break
    out = [
        {
            "code": "LEAVE",
            "leave_type_id": int(t["leave_type_id"]),
            "label": leave_option_label(t),
            "legacy_code": carried.get(i),
            "source": "master",
        }
        for i, t in enumerate(types)
    ]
    matched = set(carried.values())
    for legacy, _ in LEGACY_LEAVE_CODES:
        if legacy not in matched:
            out.append(
                {"code": legacy, "leave_type_id": None, "label": EXIT_REASONS[legacy],
                 "legacy_code": legacy, "source": "legacy"}
            )
    return out


def _builtin_option(code: str, label: str) -> dict:
    return {"code": code, "leave_type_id": None, "label": label, "legacy_code": None, "source": "builtin"}


def exit_options(leave_options: list) -> list[dict]:
    """/exit choices: Rejoined, the leave options (master types labelled
    "On leave: <label>", legacy codes unchanged), then Medical, Resigned,
    Expired, Other."""
    out = [_builtin_option("REJOINED", EXIT_REASONS["REJOINED"])]
    for o in leave_options or []:
        o = dict(o)
        if o.get("source") == "master":
            o["label"] = f"On leave: {o['label']}"
        out.append(o)
    out += [_builtin_option(k, EXIT_REASONS[k]) for k in ("MEDICAL", "RESIGNED", "EXPIRED", "OTHER")]
    return out


def on_leave_text(leave_type_name) -> str:
    """'On leave: <type name>' ('On leave' when the name is unknown)."""
    name = _clean_str(leave_type_name)
    return f"{EXIT_REASONS['LEAVE']}: {name}" if name else EXIT_REASONS["LEAVE"]


def esi_stl_result_label(code) -> Optional[str]:
    """Label of a (legacy) verification result; None when not verified."""
    if code is None:
        return None
    return ESI_STL_RESULTS.get(code, code)


def exit_reason_label(code, leave_type_name=None) -> Optional[str]:
    """Label of an exit reason ("On leave: <type name>" for LEAVE)."""
    if code == "LEAVE":
        return on_leave_text(leave_type_name)
    return EXIT_REASONS.get(code or "", code)


def leave_covers(leave_from, leave_to, day) -> bool:
    """A leave span covers ``day`` (both ends known, inclusive)."""
    f, t, d = as_date(leave_from), as_date(leave_to), as_date(day)
    return f is not None and t is not None and d is not None and f <= d <= t


def on_leave_label(c, today: Optional[date] = None) -> Optional[str]:
    """One line about the worker's leave on a case: the leave module's approved
    leave ("STL LEAVE 01.10.2026 – 20.10.2026 (leave module)"; a leave not
    started yet: "STL LEAVE from 01.11.2026 to 20.11.2026 (leave module)"), else
    a manual LEAVE exit ("SICK LEAVE till 25.10.2026"), else None."""
    if c.leave_transaction_id is not None:
        name = c.leave_type_name or "Leave"
        start = as_date(c.leave_from)
        if start is not None and start > (today or today_ist()):
            return f"{name} from {fmt_dmy(start)} to {fmt_dmy(c.leave_to)} (leave module)"
        return f"{name} {fmt_dmy(c.leave_from)} – {fmt_dmy(c.leave_to)} (leave module)"
    if c.leave_type_id is not None and c.exit_reason == "LEAVE":
        till = f" till {fmt_dmy(c.expected_return_date)}" if c.expected_return_date else ""
        return f"{c.leave_type_name or 'Leave'}{till}"
    return None

EVENT_TYPES = {
    "REMARK": "Remark",
    "FEEDBACK": "Feedback",
    "REPLY": "Reply / contact",
    "VERIFY": "Verification",
    "STAGE": "Letter issued",
    "PARK": "Parked",
    "RESUME": "Resumed",
    "EXIT": "Exit",
    "REOPEN": "Reopened",
    "DISMISS": "Dismissed",
    "APPROVAL": "Struck-off approval",
    "SYSTEM": "System",
}
USER_EVENT_TYPES = ["REMARK", "FEEDBACK", "REPLY"]

REPLY_CHANNELS = {
    "IN_PERSON": "In person",
    "UNION_REP": "Union representative",
    "LETTER": "Letter",
    "PHONE": "Phone",
    "OTHER": "Other",
}
DISPATCH_MODES = {"REG_POST_AD": "Registered post with AD", "HAND": "By hand", "GATE_BOARD": "Gate board"}
# Wording used inside letters when citing how an earlier letter went out.
SENT_HOW = {
    "REG_POST_AD": "by registered post with A.D.",
    "HAND": "by hand",
    "GATE_BOARD": "by display on the gate board",
}
DELIVERY_STATUSES = {"PENDING": "Pending", "DELIVERED": "Delivered", "RETURNED": "Returned", "UNKNOWN": "Unknown"}

# Struck-off approval (standard engine; NULL = not started)
SO_PENDING = 20
SO_APPROVED = 3
SO_REJECTED = 4

MAX_REMARKS = 1000
MAX_EXTRA = 200
MAX_TEXT = 1000
MAX_NOTE = 500

# =============================================================================
# Rule defaults (apply when the company has no jute_prod_absent_rule row)
# =============================================================================

RULE_DEFAULTS: dict[str, Any] = {
    "detect_days": 10,
    # 0 = no look-back limit (owner 2026-10-11, amendment 11.18): every worker on
    # the roll absent >= detect_days is a candidate, however long ago last seen.
    "lookback_days": 0,
    "wait_w2_days": 10,
    "wait_w3_days": 10,
    "wait_sc_days": 15,
    "sc_reply_days": 3,
    "notice_reply_days": 7,
    "dismiss_snooze_days": 30,
    # Retired (amendment 11.17): still in the response for older clients, never
    # read from the row nor used; exclude_prefixes replaces it.
    "exclude_t_codes": 1,
    # Workers whose EB No (emp_code) starts with one of these letters are not
    # long-absence candidates. NULL on the row = this default.
    "exclude_prefixes": "T,C,R,L",
    "clause_text": (
        "Clause 14(C)(V): Habitual absence without leave or absent without leave "
        "for more than 10 days."
    ),
    "signatory": "Personnel Manager",
    "extra_1_label": None,
    "extra_2_label": None,
    "extra_3_label": None,
    "extra_4_label": None,
    "extra_5_label": None,
}
RULE_INT_FIELDS = [
    "detect_days", "lookback_days", "wait_w2_days", "wait_w3_days", "wait_sc_days",
    "sc_reply_days", "notice_reply_days", "dismiss_snooze_days",
]
RULE_TEXT_FIELDS = {
    "clause_text": 500, "signatory": 100,
    "extra_1_label": 50, "extra_2_label": 50, "extra_3_label": 50, "extra_4_label": 50, "extra_5_label": 50,
    "exclude_prefixes": 50,
}

# =============================================================================
# Excluded EB-code prefixes (amendment 11.17)
# =============================================================================

_PREFIX_SEP_RE = re.compile(r"[,\s]+")
_PREFIX_LETTER_RE = re.compile(r"[A-Za-z]")
EXCLUDE_PREFIXES_MAX = RULE_TEXT_FIELDS["exclude_prefixes"]


def parse_exclude_prefixes(value) -> list[str]:
    """The single ASCII letters of a comma / space separated list, upper-cased and
    de-duplicated in first-seen order. Any other token is dropped (the strict
    check for user input is clean_exclude_prefixes)."""
    if value is None:
        return []
    out: list[str] = []
    for tok in _PREFIX_SEP_RE.split(str(value)):
        if _PREFIX_LETTER_RE.fullmatch(tok):
            letter = tok.upper()
            if letter not in out:
                out.append(letter)
    return out


def canonical_exclude_prefixes(value) -> Optional[str]:
    """The stored form: 'T,C,R,L' (upper case, comma separated, no spaces, no repeats); None when no letter."""
    letters = parse_exclude_prefixes(value)
    return ",".join(letters) if letters else None


def exclude_prefix_regex(value: Optional[str]) -> Optional[str]:
    """MySQL REGEXP for the candidates query (e.g. '^[TCRL]'), None when the list
    has no letter. Built ONLY from validated A-Z letters and always passed as a
    bind (:exclude_regex), never interpolated into the SQL."""
    letters = parse_exclude_prefixes(value)
    return "^[" + "".join(letters) + "]" if letters else None


def clean_exclude_prefixes(value) -> Optional[str]:
    """/rule_save input -> the canonical stored form. Blank / null -> None (the
    code default); 400 naming the field for anything but single letters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="exclude_prefixes must be letters separated by commas, e.g. T,C,R,L")
    s = value.strip()
    if s == "":
        return None
    if len(s) > EXCLUDE_PREFIXES_MAX:
        raise HTTPException(status_code=400, detail=f"exclude_prefixes must be at most {EXCLUDE_PREFIXES_MAX} characters")
    bad = [tok for tok in _PREFIX_SEP_RE.split(s) if tok and not _PREFIX_LETTER_RE.fullmatch(tok)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"exclude_prefixes must be single letters separated by commas, e.g. T,C,R,L (not '{bad[0][:20]}')",
        )
    return canonical_exclude_prefixes(s)


# The rule row changes only through /rule_save (which calls invalidate_rule); 60 s TTL
# saves one remote round trip on every list / candidates / letter call.
_RULE_CACHE = TtlCache(60)


def invalidate_rule(db: Session, co_id: int) -> None:
    _RULE_CACHE.invalidate((db_name(db), "rule", int(co_id)))


def load_rule(db: Session, co_id: int) -> dict:
    """The company's rule merged over RULE_DEFAULTS (a NULL text column keeps the
    default). exclude_prefixes comes back in its canonical form ('T,C,R,L').
    Cached per tenant DB + company for 60 s (see invalidate_rule)."""
    key = (db_name(db), "rule", int(co_id))
    cached = _RULE_CACHE.get(key) if key[0] else None
    if cached is not None:
        return dict(cached)
    out = dict(RULE_DEFAULTS)
    row = db.get(JuteProdAbsentRule, int(co_id))
    if row is not None:
        for k in RULE_INT_FIELDS:
            v = getattr(row, k, None)
            if v is not None:
                out[k] = int(v)
        for k in RULE_TEXT_FIELDS:
            v = getattr(row, k, None)
            if v is not None and str(v).strip() != "":
                out[k] = str(v)
    out["exclude_prefixes"] = canonical_exclude_prefixes(out["exclude_prefixes"]) or RULE_DEFAULTS["exclude_prefixes"]
    out["co_id"] = int(co_id)
    if key[0]:
        _RULE_CACHE.set(key, dict(out))
    return out


# Wait (days) that must pass after the PREVIOUS letter's sent_date before a stage's
# letter may be issued; reply window (days) printed on the letter.
def wait_days_for(stage_code: str, rule: dict) -> int:
    return {
        "WARNING_1": 0,
        "WARNING_2": int(rule["wait_w2_days"]),
        "WARNING_3": int(rule["wait_w3_days"]),
        "SHOW_CAUSE": int(rule["wait_sc_days"]),
        "GATE_NOTICE": int(rule["sc_reply_days"]),
        "STRUCK_OFF": int(rule["notice_reply_days"]),
    }.get(stage_code, 0)


def reply_days_for(stage_code: str, rule: dict) -> int:
    return {
        "WARNING_1": 7, "WARNING_2": 7, "WARNING_3": 7,
        "SHOW_CAUSE": int(rule["sc_reply_days"]),
        "GATE_NOTICE": int(rule["notice_reply_days"]),
    }.get(stage_code, 0)


# =============================================================================
# Letter templates (defaults from the owner's Word files, 2026-10-11)
# =============================================================================

PLACEHOLDERS = {
    "name": "Worker's name",
    "eb_no": "EB number",
    "dept": "Department",
    "designation": "Designation / occupation",
    "father_name": "Father's / spouse's name",
    "address": "Correspondence address",
    "mobile": "Mobile number",
    "case_no": "Case number",
    "last_working_date": "Last working date (dd.mm.yyyy)",
    "absent_days": "Days absent as on the letter date",
    "letter_date": "Letter date (dd.mm.yyyy)",
    "letter_no": "Warning letter number (1, 2, 3)",
    "letter_ordinal": "1st / 2nd / 3rd",
    "reply_days": "Days given to reply / report",
    "reply_due_date": "Last date to reply (dd.mm.yyyy)",
    "previous_notices": "Sentence listing the earlier letters with their dates and sent dates (empty on the 1st warning)",
    "letters_issued": "Neutral list of the letters issued with dates and sent dates",
    "habitual_note": "Sentence saying the worker is habitually absent (empty unless the case is marked habitual)",
    "earlier_cases": "Sentence listing the worker's earlier long-absence cases in the branch and how each ended (empty when none)",
    "warning1_date": "1st warning letter date",
    "warning2_date": "2nd warning letter date",
    "warning3_date": "3rd warning letter date",
    "warning1_sent": "1st warning sent date",
    "warning2_sent": "2nd warning sent date",
    "warning3_sent": "3rd warning sent date",
    "show_cause_date": "Show cause letter date",
    "show_cause_sent": "Show cause sent date",
    "gate_notice_date": "Gate notice date",
    "gate_notice_sent": "Gate notice sent / displayed date",
    "struck_off_date": "Date the name was struck off (memo)",
    "sc_reply_days": "Days given in the show cause",
    "clause_text": "Standing Order clause text (Settings)",
    "company_name": "Company name",
    "company_address": "Company address",
    "signatory": "Signatory (Settings)",
}

# Shared by the three warnings: date / reference / addressee / worker block and
# the paragraph citing the earlier letters ({previous_notices}, empty on the 1st).
_WARNING_SUBJECT = "NOTICE UNDER RULE 14(C)(V) OF STANDING ORDER FOR LONG ABSENCE"
_WARNING_HEAD = (
    "Date: {letter_date}\n"
    "Ref: {case_no} / {letter_ordinal} warning letter\n"
    "\n"
    "To,\n"
    "{name}\n"
    "{address}\n"
    "\n"
    "NAME: {name}\n"
    "EB NO: {eb_no}\n"
    "DEPT: {dept}\n"
    "DESIGNATION: {designation}\n"
    "\n"
    "{previous_notices}\n"
    "\n"
)
_SIGNATURE = (
    "\n"
    "\n"
    "{signatory}\n"
    "{company_name}"
)

TEMPLATE_DEFAULTS: dict[str, dict[str, str]] = {
    # 1st warning: mild — records the absence, cites the clause, asks the worker
    # to report or explain.
    "WARNING_1": {
        "subject": _WARNING_SUBJECT,
        "body": (
            _WARNING_HEAD
            + "It is noticed from the records that you have been absent from duty since {last_working_date} "
            "({absent_days} days as on {letter_date}) without any leave or information to the Personnel Office. "
            "Absence without leave for more than 10 days is misconduct as per clause 14(C)(V) of the Standing "
            "Order of the company mentioned below:\n"
            "\n"
            "{clause_text}\n"
            "\n"
            "You are therefore advised to report for duty within {reply_days} days from the receipt of this "
            "letter, or to inform the Personnel Office of the reason for your absence with valid proof, failing "
            "which further action will be taken against you as per the company norms.\n"
            + _SIGNATURE
        ),
    },
    # 2nd warning: firmer — the earlier letter went unanswered; a final warning
    # and then the struck-off follow.
    "WARNING_2": {
        "subject": _WARNING_SUBJECT,
        "body": (
            _WARNING_HEAD
            + "Despite our earlier letter you have neither reported for duty nor informed the Personnel Office "
            "of the reason for your absence. You have remained absent from duty since {last_working_date} "
            "({absent_days} days as on {letter_date}) without leave or information, which is misconduct as per "
            "clause 14(C)(V) of the Standing Order of the company mentioned below:\n"
            "\n"
            "{clause_text}\n"
            "\n"
            "This is a second and serious warning. You are hereby directed to report for duty within {reply_days} "
            "days from the receipt of this letter, or to explain the reason for your absence to the Personnel "
            "Office with valid proof, failing which a final warning will be issued to you and thereafter your "
            "name will be liable to be struck off from the muster roll of the company.\n"
            + _SIGNATURE
        ),
    },
    # 3rd & last warning: the owner's own text (Word file), plus the habitual
    # note and the earlier cases (empty paragraphs drop out).
    "WARNING_3": {
        "subject": _WARNING_SUBJECT,
        "body": (
            _WARNING_HEAD
            + "{habitual_note}\n"
            "\n"
            "{earlier_cases}\n"
            "\n"
            "It reveals from the record that you are absenting for long time since {last_working_date} "
            "({absent_days} days as on {letter_date}) without any prior information. This is gross negligence "
            "on your part and the act alleged to have been committed by you is misconduct as per clause 14(C)(V) "
            "of the Standing Order of the company mentioned below:\n"
            "\n"
            "{clause_text}\n"
            "\n"
            "Therefore it is presumed that you are no more interested to continue your work and left the service "
            "at your own accord; accordingly your name will be struck off from the muster roll for committing "
            "gross misconduct mentioned above.\n"
            "\n"
            "However for the sake of natural justice, you are hereby provided last and final opportunity to report "
            "to your duty within {reply_days} days from the receipt of this letter failing which your name will be "
            "struck off from the muster roll of the company.\n"
            + _SIGNATURE
        ),
    },
    "SHOW_CAUSE": {
        "subject": "SHOW CAUSE NOTICE FOR UNAUTHORISED ABSENCE FROM DUTY",
        "body": (
            "Date: {letter_date}\n"
            "Ref: {case_no}\n"
            "\n"
            "To,\n"
            "{name}\n"
            "{address}\n"
            "\n"
            "NAME: {name}\n"
            "EB NO: {eb_no}\n"
            "DEPT: {dept}\n"
            "DESIGNATION: {designation}\n"
            "\n"
            "{previous_notices}\n"
            "\n"
            "{habitual_note}\n"
            "\n"
            "{earlier_cases}\n"
            "\n"
            "You have remained absent from duty since {last_working_date} ({absent_days} days as on {letter_date}) "
            "without leave or any information, and you have not replied to any of the warning letters referred to "
            "above. Habitual absence without leave or absence without leave for more than 10 days is misconduct "
            "under clause 14(C)(V) of the Standing Order of the company.\n"
            "\n"
            "You are hereby called upon to show cause, within {reply_days} days of the receipt of this notice, why "
            "your name should not be struck off from the muster roll of the company. You may contact the Personnel "
            "Office in person, through your union representative, by written letter or by phone, with valid proof "
            "of the reason for your absence.\n"
            "\n"
            "Failing which, a notice will be displayed on the front gate board and in your department, a copy will "
            "be served on each working union, and thereafter your name will be struck off from the muster roll "
            "without any further reference to you.\n"
            "\n"
            "\n"
            "{signatory}\n"
            "{company_name}"
        ),
    },
    "GATE_NOTICE": {
        "subject": "NOTICE",
        "body": (
            "Dated: {letter_date}\n"
            "\n"
            "{company_name}\n"
            "\n"
            "NOTICE\n"
            "\n"
            "With reference to the Show Cause Letter for unauthorised leave from duty dated {show_cause_date} "
            "served on {name} (EB No. {eb_no}) of {dept} department, it is hereby notified to all the working "
            "unions and general workmen of the mill that after issuing all three warning letters as per the company "
            "norms the said workman failed to report to duty. The dates of issue of the warning letters are as "
            "follows: (1) 1st warning dated {warning1_date}, sent on {warning1_sent}; (2) 2nd warning dated "
            "{warning2_date}, sent on {warning2_sent}; (3) 3rd warning dated {warning3_date}, sent on {warning3_sent}.\n"
            "\n"
            "As per the Show Cause letter dated {show_cause_date}, {sc_reply_days} more days were given to the "
            "workman to report to the Personnel Office and he failed to do so again, nor sent any information by "
            "phone or union representative.\n"
            "\n"
            "The management, being lenient, is providing another opportunity to {name} for {reply_days} days from "
            "the notice date to present himself at the Personnel Office or present a valid proof for his absence, "
            "failing which the management regrets to inform that his name will be struck off from the Muster Roll "
            "and no other requests for his rejoining will be entertained from the union representatives.\n"
            "\n"
            "\n"
            "{signatory}\n"
            "{company_name}"
        ),
    },
    "STRUCK_OFF_MEMO": {
        "subject": "NAME STRUCK OFF FROM MUSTER ROLL",
        "body": (
            "Date: {letter_date}\n"
            "Ref: {case_no}\n"
            "\n"
            "{name} (EB No. {eb_no}), {designation}, {dept} department, has remained absent from duty since "
            "{last_working_date} without leave or information.\n"
            "\n"
            "Letters issued: {letters_issued}.\n"
            "\n"
            "No reply or valid proof having been received within the time given, the name of the said workman "
            "stands struck off from the muster roll of the company with effect from {struck_off_date} under clause "
            "14(C)(V) of the Standing Order.\n"
            "\n"
            "\n"
            "{signatory}\n"
            "{company_name}"
        ),
    },
}

_TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def fmt_dmy(d) -> str:
    """dd.mm.yyyy (the mill's letter style); blank-safe. Accepts date / datetime / ISO string."""
    if d is None or d == "":
        return ""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%d.%m.%Y")


# A line that holds nothing but one {placeholder} (spaces / tabs around it allowed).
_TOKEN_LINE_RE = re.compile(r"^[ \t]*\{([a-z][a-z0-9_]*)\}[ \t]*(?:\r\n|\n|\r)?$")


def _renders_empty(fields: dict, key: str) -> bool:
    """A known field that merges to nothing ('' / blank; None prints a blank to fill)."""
    if key not in fields:
        return False
    v = fields[key]
    return isinstance(v, str) and v.strip() == ""


def merge_text(template: str, fields: dict) -> str:
    """Replace {placeholder} tokens.

    * A known field that is None becomes '________' (missing data stays
      visible on the letter).
    * A known field that is an empty / blank string renders as nothing. When
      such a token sits ALONE on its line (e.g. {previous_notices} on the 1st
      warning), that whole line is removed together with its one newline; if
      a blank line precedes it (an emptied paragraph) that blank line goes
      too, so the surrounding paragraphs keep a single gap. Every other line,
      blank lines included, is kept exactly as the template has it (the
      "\\n\\n\\n{signatory}" signature gap survives).
    * An unknown token is left as typed so a typo stays visible."""
    def sub(m):
        key = m.group(1)
        if key not in fields:
            return m.group(0)
        v = fields[key]
        if v is None:
            return "________"
        if isinstance(v, str) and v.strip() == "":
            return ""
        return str(v)

    kept = []
    skip_blank = False
    for line in (template or "").splitlines(keepends=True):
        if skip_blank:
            skip_blank = False
            if line.strip() == "":
                continue
        m = _TOKEN_LINE_RE.match(line)
        if m and _renders_empty(fields, m.group(1)):
            if line == line.rstrip("\r\n") and kept:
                # the emptied token was the last line (no newline of its own):
                # drop the newline that ended the line before it instead
                kept[-1] = kept[-1].rstrip("\r\n")
                if kept[-1].strip() == "":
                    kept.pop()
                    if kept:
                        kept[-1] = kept[-1].rstrip("\r\n")
            elif kept and kept[-1].strip() == "":
                # an emptied PARAGRAPH (blank line above it): drop that blank
                # line too, so the paragraphs around it keep a single gap
                kept.pop()
            else:
                # first line of the text (or no blank above): drop the blank
                # line that follows it instead
                skip_blank = True
            continue
        kept.append(line)
    return _TOKEN_RE.sub(sub, "".join(kept))


def previous_notices_sentence(letters: list[dict]) -> str:
    """The paragraph that cites the earlier letters of the case, newest last.

    ``letters``: dicts with stage_code, letter_no, issue_date, sent_date, ad_no,
    dispatch_mode (only active letters, ordered by issue_date / letter_id).
    Empty when there is nothing earlier (the 1st warning)."""
    parts = []
    for l in letters:
        code = l.get("stage_code")
        if code in LETTER_NO_OF_STAGE:
            label = f"{ORDINALS[LETTER_NO_OF_STAGE[code]]} warning letter"
        elif code == "SHOW_CAUSE":
            label = "show cause notice"
        elif code == "GATE_NOTICE":
            label = "gate notice"
        else:
            continue
        s = f"our {label} dated {fmt_dmy(l.get('issue_date'))}"
        sent = l.get("sent_date")
        if sent:
            how = SENT_HOW.get(l.get("dispatch_mode") or "", "")
            s += f", sent on {fmt_dmy(sent)}" + (f" {how}" if how else "")
            if l.get("ad_no"):
                s += f" (A.D. No. {l['ad_no']})"
        parts.append(s)
    if not parts:
        return ""
    n = len(parts)
    if n == 1:
        joined = parts[0]
    else:
        joined = ", ".join(parts[:-1]) + " and " + parts[-1]
    nxt = ORDINALS.get(n + 1, f"{n + 1}th")
    return (
        f"This is in continuation of {joined}, to which no reply has been received from you. "
        f"This is the {nxt} notice being issued to you on the same matter."
    )


def letters_issued_sentence(letters: list[dict]) -> str:
    """Neutral (third-person) list of the letters issued on the case, oldest
    first, for documents not addressed to the worker (the struck-off memo):
    "1st warning letter dated 01.09.2026, sent 03.09.2026 (A.D. No. RA123);
    ...; gate notice dated 01.10.2026, displayed 02.10.2026".

    ``letters``: dicts with stage_code, issue_date, sent_date, ad_no and, for
    the gate notice, gate_display_date (shown instead of sent_date when
    recorded). The memo itself is never listed. Empty when nothing was issued."""
    parts = []
    for l in letters:
        code = l.get("stage_code")
        if code in LETTER_NO_OF_STAGE:
            label = f"{ORDINALS[LETTER_NO_OF_STAGE[code]]} warning letter"
        elif code == "SHOW_CAUSE":
            label = "show cause notice"
        elif code == "GATE_NOTICE":
            label = "gate notice"
        else:
            continue
        s = f"{label} dated {fmt_dmy(l.get('issue_date'))}"
        if code == "GATE_NOTICE":
            shown = l.get("gate_display_date") or l.get("sent_date")
            if shown:
                s += f", displayed {fmt_dmy(shown)}"
        elif l.get("sent_date"):
            s += f", sent {fmt_dmy(l.get('sent_date'))}"
            if l.get("ad_no"):
                s += f" (A.D. No. {l['ad_no']})"
        parts.append(s)
    return "; ".join(parts)


HABITUAL_NOTE = "Your attendance record also shows that you have been habitually absent without leave."


def habitual_note_sentence(habitual) -> str:
    """{habitual_note}: the sentence when the case is marked habitual, else ''
    (merge_text then drops the paragraph)."""
    try:
        return HABITUAL_NOTE if int(habitual or 0) == 1 else ""
    except (TypeError, ValueError):
        return HABITUAL_NOTE if habitual is True else ""


def earlier_cases_sentence(rows: list, before=None) -> str:
    """{earlier_cases}: the worker's earlier long-absence cases, oldest first.

    "Earlier long-absence cases on your record: LA/2/25/0012 (detected
    05.08.2025, rejoined); LA/2/26/0003 (detected 02.02.2026, on ESI)."

    ``rows``: the worker's other cases in the branch (dicts / row mappings in
    long_absenteeism_query.get_other_cases_query's shape: case_id, case_no,
    detected_date, status, exit_reason). Only CLOSED cases count; a DISMISSED
    one (a candidate that never became a case) is left out, and so is any
    case detected after ``before`` (the case the letter is for). '' when none."""
    limit = as_date(before)
    picked = []
    for r in rows or []:
        status = r.get("status")
        if status is None or int(status) != STATUS_CLOSED or r.get("exit_reason") == "DISMISSED":
            continue
        detected = as_date(r.get("detected_date"))
        if limit is not None and detected is not None and detected > limit:
            continue
        picked.append((detected or date.min, int(r.get("case_id") or 0), r))
    parts = []
    for detected, case_id, r in sorted(picked, key=lambda t: (t[0], t[1])):
        bits = []
        if detected != date.min:
            bits.append(f"detected {fmt_dmy(detected)}")
        reason = r.get("exit_reason")
        label = EXIT_REASONS.get(reason or "", reason)
        if label:
            bits.append(label[:1].lower() + label[1:])
        ref = r.get("case_no") or f"case {case_id}"
        parts.append(f"{ref} ({', '.join(bits)})" if bits else str(ref))
    if not parts:
        return ""
    return "Earlier long-absence cases on your record: " + "; ".join(parts) + "."


# =============================================================================
# Request parsing (same helpers as the follow-up router)
# =============================================================================


def today_ist() -> date:
    """Mill date (IST), not the UTC server date."""
    return now_ist().date()


def json_object(request: Request) -> dict:
    """The POST body, which must be a JSON object (400 otherwise, never 500)."""
    try:
        body = parse_json_body(request)
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def req_int(src: dict, key: str) -> int:
    v = src.get(key)
    if v is None or v == "":
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {key} format")


def opt_int(src: dict, key: str) -> Optional[int]:
    v = src.get(key)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {key} format")


def opt_bool(src: dict, key: str, default: bool = False) -> bool:
    v = src.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def parse_date(value, field: str, required: bool = False) -> Optional[date]:
    if value is None or value == "":
        if required:
            raise HTTPException(status_code=400, detail=f"{field} is required")
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a date (YYYY-MM-DD)")


def opt_text(src: dict, key: str, max_len: int) -> Optional[str]:
    v = src.get(key)
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{key} must be at most {max_len} characters")
    return s


def req_code(src: dict, key: str, allowed) -> str:
    v = src.get(key)
    if v is None or str(v).strip() == "":
        raise HTTPException(status_code=400, detail=f"{key} is required")
    code = str(v).strip().upper()
    if code not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {key}: {v}")
    return code


def rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:  # pragma: no cover - best effort
        pass


# =============================================================================
# Access (server-side half of the sidebar's hasMenuAccess on this page)
# =============================================================================


def get_menu_id_query():
    """Active menu rows of this page under either path (HRMS first, then the
    old Jute Production > Reports one). Binds: :menu_path, :legacy_menu_path."""
    return text(
        """
        SELECT menu_id, TRIM(BOTH '/' FROM menu_path) AS menu_path
        FROM menu_mst
        WHERE active = 1 AND TRIM(BOTH '/' FROM menu_path) IN (:menu_path, :legacy_menu_path)
        ORDER BY menu_id
        """
    )


def get_branch_in_company_query():
    return text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id LIMIT 1")


def pick_menu_id(rows) -> Optional[int]:
    """menu_id of the first path of MENU_PATHS that has an active row (the
    lowest menu_id of that path); None when neither has one. ``rows``:
    (menu_id, menu_path) pairs or mappings."""
    by_path: dict = {}
    for r in rows or []:
        m = getattr(r, "_mapping", r)
        mid, path = (m[0], m[1]) if isinstance(m, (tuple, list)) else (m["menu_id"], m["menu_path"])
        if mid is None or path is None:
            continue
        path = str(path).strip("/")
        if path not in by_path or int(mid) < by_path[path]:
            by_path[path] = int(mid)
    for path in MENU_PATHS:
        if path in by_path:
            return by_path[path]
    return None


# menu_id per tenant DB: the menu row only changes by migration, 10 minutes is safe.
_MENU_ID_CACHE = TtlCache(600)


def menu_id(db: Session) -> Optional[int]:
    """menu_id of the Long Absenteeism page: the active HRMS row, else the
    active legacy Jute Production row (until the menu migration has run).
    Cached per tenant DB for 10 minutes (one remote round trip per request saved)."""
    key = (db_name(db), "menu_id")
    cached = _MENU_ID_CACHE.get(key)
    if cached is not None:
        return cached or None
    rows = db.execute(
        get_menu_id_query(), {"menu_path": MENU_PATHS[0], "legacy_menu_path": MENU_PATHS[1]}
    ).fetchall()
    mid = pick_menu_id(rows)
    if key[0]:
        _MENU_ID_CACHE.set(key, mid or 0)
    return mid


def access_level(db: Session, user_id, branch_id) -> int:
    """Highest access_type_id the user holds on the Long Absenteeism menu for the
    branch (1 view .. 4 edit; 0 = no grant / no menu row)."""
    if not user_id or branch_id is None:
        return 0
    mid = menu_id(db)
    if mid is None:
        return 0
    row = db.execute(
        get_user_edit_access(),
        {"user_id": int(user_id), "branch_id": int(branch_id), "menu_id": mid},
    ).first()
    level = row[0] if row else None
    try:
        return int(level) if level is not None else 0
    except (TypeError, ValueError):
        return 0


def require_access(db: Session, token_data: dict, branch_id, level: int) -> None:
    """403 unless the caller holds at least ``level`` on the menu for the branch."""
    user_id = (token_data or {}).get("user_id")
    if access_level(db, user_id, branch_id) < level:
        raise HTTPException(
            status_code=403,
            detail=f"You need {_LEVEL_NAMES.get(level, str(level))} access to Long Absenteeism for this branch",
        )


# A branch's company never changes in practice; 10 minutes saves one remote round trip per request.
_BRANCH_CACHE = TtlCache(600)


def verify_branch(db: Session, co_id: int, branch_id: int) -> None:
    key = (db_name(db), "branch", int(co_id), int(branch_id))
    if key[0] and _BRANCH_CACHE.get(key):
        return
    if not db.execute(get_branch_in_company_query(), {"branch_id": int(branch_id), "co_id": int(co_id)}).first():
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")
    if key[0]:
        _BRANCH_CACHE.set(key, True)


def require_branch_access(db: Session, token_data: dict, co_id: int, branch_id: int, level: int) -> None:
    """Client-sent co_id + branch_id: the pair must match, then the level is checked."""
    verify_branch(db, co_id, branch_id)
    require_access(db, token_data, branch_id, level)


# =============================================================================
# Loaders and shaping shared by both routers
# =============================================================================


def load_case(db: Session, co_id: int, case_id: int) -> JuteProdAbsentCase:
    c = db.get(JuteProdAbsentCase, int(case_id))
    if c is None or int(c.active or 0) != 1 or int(c.co_id) != int(co_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return c


def load_letter(db: Session, letter_id: int) -> JuteProdAbsentLetter:
    l = db.get(JuteProdAbsentLetter, int(letter_id))
    if l is None or int(l.active or 0) != 1:
        raise HTTPException(status_code=404, detail="Letter not found")
    return l


def require_live(c: JuteProdAbsentCase) -> None:
    if int(c.status) == STATUS_CLOSED:
        raise HTTPException(status_code=400, detail="This case is closed — reopen it to change it")


def require_open(c: JuteProdAbsentCase) -> None:
    if int(c.status) != STATUS_OPEN:
        raise HTTPException(
            status_code=400,
            detail="This case is parked (reply under review) — resume it first" if int(c.status) == STATUS_PARKED
            else "This case is closed — reopen it to change it",
        )


def add_event(
    db: Session,
    case_id: int,
    event_type: str,
    text_value: Optional[str],
    user_id,
    event_date: Optional[date] = None,
    channel: Optional[str] = None,
    contact_name: Optional[str] = None,
    ref_letter_id: Optional[int] = None,
) -> JuteProdAbsentEvent:
    """Append a timeline row (no commit — the caller commits with its own write)."""
    ev = JuteProdAbsentEvent(
        case_id=int(case_id),
        event_type=event_type,
        event_date=event_date or today_ist(),
        channel=channel,
        contact_name=contact_name,
        text=(text_value or None),
        ref_letter_id=ref_letter_id,
        active=1,
        updated_by=user_id,
    )
    db.add(ev)
    return ev


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


def model_to_dict(obj) -> dict:
    """Plain dict of an ORM row's columns, dates as ISO strings, Decimal as float.
    A DEFERRED column (the letter's ``recorded``) is included only when it is
    already loaded — never fetched here, so a tenant without migration 20261012b
    still works (the key is then simply absent)."""
    mapper = sa_inspect(type(obj))
    loaded = sa_inspect(obj).dict
    out = {}
    for c in obj.__table__.columns:
        prop = mapper.column_attrs.get(c.key)
        if prop is not None and prop.deferred:
            if c.key in loaded:
                out[c.name] = _iso(loaded[c.key])
            continue
        out[c.name] = _iso(getattr(obj, c.name))
    return out


def absent_days_live(c: JuteProdAbsentCase, as_on: Optional[date] = None) -> int:
    """Days absent as on ``as_on`` (today): the day after the last working date counts as day 1."""
    end = c.rejoined_seen_date or (as_on or today_ist())
    return max(0, (end - c.last_working_date).days)


def case_to_dict(c: JuteProdAbsentCase, as_on: Optional[date] = None) -> dict:
    """Case columns + labels. Leave link (TKT-000223): ``exit_reason_label``
    reads "On leave: <type>" for LEAVE; ``esi_stl_result_label`` labels legacy
    verification rows; ``on_leave_now`` = the linked leave-module leave covers
    ``as_on`` (today); ``on_leave_label`` = on_leave_label(c, as_on);
    ``leave_parked`` comes with the columns."""
    day = as_on or today_ist()
    d = model_to_dict(c)
    d["stage_label"] = STAGE_LABELS.get(c.stage_code, c.stage_code)
    d["stage_order"] = STAGE_ORDER.get(c.stage_code, -1)
    d["status_label"] = STATUS_LABELS.get(int(c.status), str(c.status))
    d["exit_reason_label"] = exit_reason_label(c.exit_reason, c.leave_type_name)
    d["esi_stl_result_label"] = esi_stl_result_label(c.esi_stl_result)
    d["absent_days"] = absent_days_live(c, as_on)
    d["on_leave_now"] = c.leave_transaction_id is not None and leave_covers(c.leave_from, c.leave_to, day)
    d["on_leave_label"] = on_leave_label(c, day)
    return d


def letter_to_dict(l: JuteProdAbsentLetter) -> dict:
    d = model_to_dict(l)
    d["stage_label"] = STAGE_LABELS.get(l.stage_code, l.stage_code)
    d["template_label"] = template_label(l.template_code)
    d["dispatch_mode_label"] = DISPATCH_MODES.get(l.dispatch_mode or "", l.dispatch_mode)
    d["delivery_status_label"] = DELIVERY_STATUSES.get(l.delivery_status or "", l.delivery_status)
    return d


def event_to_dict(e: JuteProdAbsentEvent) -> dict:
    d = model_to_dict(e)
    d["event_type_label"] = EVENT_TYPES.get(e.event_type, e.event_type)
    d["channel_label"] = REPLY_CHANNELS.get(e.channel or "", e.channel)
    return d


def codes_payload() -> dict:
    """Code lists for /setup (each [{code,label}] in display order)."""
    def lst(d):
        return [{"code": k, "label": v} for k, v in d.items()]
    return {
        "stages": [{"code": k, "label": v, "order": STAGE_ORDER[k]} for k, v in STAGES],
        "letter_stages": LETTER_STAGES,
        "template_codes": [{"code": k, "label": v} for k, v in TEMPLATE_CODES],
        "statuses": [{"code": k, "label": v} for k, v in STATUS_LABELS.items()],
        "exit_reasons": [{"code": k, "label": EXIT_REASONS[k]} for k in USER_EXIT_REASONS],
        "all_exit_reasons": lst(EXIT_REASONS),
        "esi_stl_results": lst(ESI_STL_RESULTS),
        "event_types": lst(EVENT_TYPES),
        "user_event_types": USER_EVENT_TYPES,
        "reply_channels": lst(REPLY_CHANNELS),
        "dispatch_modes": lst(DISPATCH_MODES),
        "delivery_statuses": lst(DELIVERY_STATUSES),
        "placeholders": [{"code": k, "label": v} for k, v in PLACEHOLDERS.items()],
    }


def next_stage_of(c: JuteProdAbsentCase) -> Optional[str]:
    """The letter stage the case would move into next (None once struck off)."""
    for stage, prev in PREVIOUS_STAGE.items():
        if prev == c.stage_code:
            return stage
    return None


def first_allowed_day(base: date, wait: int) -> date:
    """The day the next step opens after a wait of ``wait`` days counted from
    ``base`` (see due_date_for_next for the rule)."""
    wait = int(wait or 0)
    return base + timedelta(days=wait + 1) if wait > 0 else base


def due_date_for_next(c: JuteProdAbsentCase, last_letter: Optional[JuteProdAbsentLetter], rule: dict) -> Optional[date]:
    """When the next step becomes possible (stored as the case's next_due_date):
    the FIRST day it is allowed. None when nothing is next.

    Counting rule (owner's procedure: "wait N days ... if no reply", lead
    decision 2026-10-11): a wait of N days runs from the previous letter's
    sent_date (issue_date until the sent date is recorded) and covers N full
    days AFTER it, so the next step opens on base + N + 1. Example: 1st warning
    sent 03.09, 10-day wait -> the 2nd warning is due from 14.09. A zero wait
    opens on the base day itself. The 1st warning has no wait: it is due from
    the verification (the case's stage_date at VERIFIED). For a GATE_NOTICE
    case the next step is the struck-off request, due the day after the gate
    notice reply window (sent_date + notice_reply_days) ends."""
    nxt = next_stage_of(c)
    if nxt is None:
        return None
    if nxt == "WARNING_1":
        return c.stage_date
    base = None
    if last_letter is not None:
        base = last_letter.sent_date or last_letter.issue_date
    base = base or c.stage_date
    return first_allowed_day(base, wait_days_for(nxt, rule))


# =============================================================================
# Case shape served by BOTH routers (case + next step + watchlist band)
# =============================================================================

# Watchlist bands, in the order due_band tests them (the UI groups by these).
DUE_BANDS = ("MARKED_PRESENT", "ON_LEAVE", "REPLY_REVIEW", "APPROVAL", "VERIFY", "STRUCK_OFF_DUE", "LETTER_DUE")


def as_date(v) -> Optional[date]:
    """date of a date / datetime / ISO string (None / '' -> None)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def due_band(c, today: date) -> str:
    """Watchlist band of a case, first match in the order MARKED_PRESENT,
    ON_LEAVE, REPLY_REVIEW, APPROVAL, VERIFY, STRUCK_OFF_DUE, LETTER_DUE; NONE
    otherwise (closed cases always NONE).

    ON_LEAVE (TKT-000223) = the linked leave-module leave (approved) covers
    today (leave_from <= today <= leave_to), whatever the live status — a case
    parked by its leave is ON_LEAVE, not REPLY_REVIEW.

    STRUCK_OFF_DUE = an open GATE_NOTICE case whose reply window has ended
    (today >= next_due_date) and that has not been sent for approval yet
    (status_id NULL, or 4 = rejected and reopened). A GATE_NOTICE case is never
    LETTER_DUE (its next step is the approval request, not a letter)."""
    status = int(c.status)
    if status == STATUS_CLOSED:
        return "NONE"
    sid = int(c.status_id) if c.status_id is not None else None
    if c.rejoined_seen_date is not None:
        return "MARKED_PRESENT"
    if c.leave_transaction_id is not None and leave_covers(c.leave_from, c.leave_to, today):
        return "ON_LEAVE"
    if status == STATUS_PARKED:
        return "REPLY_REVIEW"
    if sid == SO_PENDING:
        return "APPROVAL"
    if status != STATUS_OPEN:
        return "NONE"
    if c.stage_code == "DETECTED":
        return "VERIFY"
    due = as_date(c.next_due_date)
    if due is None or due > today:
        return "NONE"
    if c.stage_code == "GATE_NOTICE":
        return "STRUCK_OFF_DUE" if sid in (None, SO_REJECTED) else "NONE"
    if next_stage_of(c) is not None:
        return "LETTER_DUE"
    return "NONE"


def case_out(c: JuteProdAbsentCase, today: Optional[date] = None) -> dict:
    """The case as every endpoint of both routers returns it: case_to_dict +
    next_stage_code / next_stage_label (live cases only; "Struck off" for a
    gate-notice case) + due_band."""
    today = today or today_ist()
    d = case_to_dict(c, today)
    nxt = next_stage_of(c) if int(c.status) != STATUS_CLOSED else None
    d["next_stage_code"] = nxt
    d["next_stage_label"] = STAGE_LABELS.get(nxt) if nxt else None
    d["due_band"] = due_band(c, today)
    return d


# =============================================================================
# No manual verification + automatic leave (owner ruling 12.10.2026, TKT-000223)
# =============================================================================

AUTO_VERIFY_NOTE = "Leave module checked automatically"
AUTO_VERIFY_EVENT = "Verification step removed — 1st warning due now (leave module checked automatically)"
LEAVE_WITHDRAWN_EVENT = "Leave record withdrawn in the leave module; case resumed"
LEAVE_LINK_COLUMNS = ("leave_transaction_id", "leave_type_id", "leave_type_name", "leave_from", "leave_to")


def auto_verified_fields(today: date, parked: bool = False) -> dict:
    """The columns that put a case at VERIFIED without the retired manual step
    (1st warning due today; a parked case keeps its next_due_date)."""
    out = {"stage_code": "VERIFIED", "stage_date": today, "verified_date": today, "verify_note": AUTO_VERIFY_NOTE}
    if not parked:
        out["next_due_date"] = today
    return out


def leave_parked_event(leave_type_name, leave_from, leave_to) -> str:
    """'On STL LEAVE 01.10.2026 to 20.10.2026 (leave module)' — "<name> leave"
    unless the type's name already ends with 'leave'; 'On leave ...' unnamed."""
    name = _clean_str(leave_type_name)
    what = "leave" if not name else (name if name.lower().endswith("leave") else f"{name} leave")
    return f"On {what} {fmt_dmy(leave_from)} to {fmt_dmy(leave_to)} (leave module)"


def leave_ended_event(leave_to) -> str:
    return f"Leave ended {fmt_dmy(leave_to)}, still absent"


def _opt_int(v) -> Optional[int]:
    return int(v) if v is not None and v != "" else None


def plan_leave_refresh(cs: dict, lv: Optional[dict], today: date) -> tuple:
    """What the leave refresh does to ONE live case: (column updates, SYSTEM
    event texts). Pure; refresh_leaves_branch / refresh_leave_case apply it on
    the ORM row.

    ``cs``: the case state — status, status_id, stage_code, next_due_date,
    rejoined_seen_date (after the rejoin refresh), leave_parked and the stored
    LEAVE_LINK_COLUMNS. ``lv``: the worker's approved leave (status 3) after the
    last working date {leave_transaction_id, leave_type_id, leave_type_name,
    leave_from, leave_to}, None / no id when there is none.

    0. A case still at DETECTED is promoted to VERIFIED (once; a parked case
       keeps its next_due_date) with AUTO_VERIFY_EVENT.
    a. lv covers today, not rejoined, no struck-off approval pending: store the
       columns; a NEW leave transaction (id differs from the stored one) parks
       an OPEN case (leave_parked = 1) and logs leave_parked_event — once per
       leave transaction, so a case resumed by hand is not parked again.
    b. lv ended (leave_to < today), not rejoined: store the columns; a case
       parked by its leave resumes (leave_parked 0, next_due_date = max(current
       or leave_to + 1, leave_to + 1)) with leave_ended_event.
    c. lv not started yet: store the columns only. (A case still parked by a
       leave that no longer covers today resumes as in d.)
    d. no lv but a stored link: clear the five columns; a case parked by its
       leave resumes (next_due_date = max(current, today)) with
       LEAVE_WITHDRAWN_EVENT.
    e. rejoined (Marked present wins): columns only, never a status change."""
    upd: dict = {}
    events: list = []
    status = int(cs.get("status") or 0)
    if status == STATUS_CLOSED:
        return upd, events
    if cs.get("stage_code") == "DETECTED":
        upd.update(auto_verified_fields(today, parked=status == STATUS_PARKED))
        events.append(AUTO_VERIFY_EVENT)
    rejoined = cs.get("rejoined_seen_date") is not None
    sid = _opt_int(cs.get("status_id"))
    stored_ltid = _opt_int(cs.get("leave_transaction_id"))
    leave_parked = _opt_int(cs.get("leave_parked")) == 1
    due_now = as_date(upd.get("next_due_date", cs.get("next_due_date")))
    can_resume = not rejoined and status == STATUS_PARKED and leave_parked

    def resume(next_due: date, text_value: str) -> None:
        upd.update(status=STATUS_OPEN, leave_parked=0, next_due_date=next_due)
        events.append(text_value)

    lv_id = _opt_int((lv or {}).get("leave_transaction_id"))
    if lv_id is None:
        if stored_ltid is None:
            return upd, events
        upd.update({k: None for k in LEAVE_LINK_COLUMNS})
        if can_resume:
            resume(max(due_now or today, today), LEAVE_WITHDRAWN_EVENT)
        return upd, events

    name = _clean_str(lv.get("leave_type_name"))
    new = {
        "leave_transaction_id": lv_id,
        "leave_type_id": _opt_int(lv.get("leave_type_id")),
        "leave_type_name": name[:LEAVE_TYPE_NAME_MAX] if name else None,
        "leave_from": as_date(lv.get("leave_from")),
        "leave_to": as_date(lv.get("leave_to")),
    }
    old_name = _clean_str(cs.get("leave_type_name"))
    old = {
        "leave_transaction_id": stored_ltid,
        "leave_type_id": _opt_int(cs.get("leave_type_id")),
        "leave_type_name": old_name[:LEAVE_TYPE_NAME_MAX] if old_name else None,
        "leave_from": as_date(cs.get("leave_from")),
        "leave_to": as_date(cs.get("leave_to")),
    }
    if new != old:
        upd.update(new)
    if rejoined:
        return upd, events
    l_from, l_to = new["leave_from"], new["leave_to"]
    if leave_covers(l_from, l_to, today):
        if sid != SO_PENDING and lv_id != stored_ltid:
            if status == STATUS_OPEN:
                upd.update(status=STATUS_PARKED, leave_parked=1)
            events.append(leave_parked_event(new["leave_type_name"], l_from, l_to))
    elif can_resume:
        if l_to is not None and l_to < today:
            back = l_to + timedelta(days=1)
            resume(max(due_now or back, back), leave_ended_event(l_to))
        else:
            # the leave that parked the case no longer covers today (withdrawn
            # or moved; only a later one is approved): resume like d.
            resume(max(due_now or today, today), LEAVE_WITHDRAWN_EVENT)
    return upd, events


def fresh_rejoin_date(db: Session, c: JuteProdAbsentCase) -> Optional[date]:
    """Rejoin detection (spec 4.6) for one LIVE case, read fresh and never
    stored: the first active, non-rejected attendance of the worker after the
    last working date (the query /list and /get use), else None. Closed cases:
    None. Used to refuse letters / the struck-off request for a worker who has
    come back."""
    if int(c.status) == STATUS_CLOSED:
        return None
    row = db.execute(
        get_rejoin_query(), {"co_id": int(c.co_id), "branch_id": int(c.branch_id), "case_id": int(c.case_id)}
    ).first()
    if row is None:
        return None
    return as_date(row._mapping["first_seen"])


def rejoined_message(seen: date) -> str:
    return f"Worker attended on {fmt_dmy(seen)} — exit the case as Rejoined instead"
