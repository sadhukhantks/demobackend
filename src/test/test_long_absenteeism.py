"""Tests for the Long Absenteeism cases router (src/juteProduction/long_absenteeism.py)
and the pure helpers of long_absenteeism_common.py.

Portal persona: DB + auth mocked via app.dependency_overrides on the exact symbols
the router imports (the router is mounted on its own app, like the Low Performer
Follow-up tests). The DB fake routes ``execute`` by a SQL substring and records
every call, so the tests assert BIND VALUES, never call order. The access level
is patched on long_absenteeism_common (require_access / require_branch_access and
/setup all resolve it there).

Covered (spec section 9, BE-1):
  * common helpers — fmt_dmy, merge_text, previous_notices_sentence, wait / reply
    day tables, due_date_for_next, next_stage_of, load_rule (fake db.get),
    absent_days_live, case_to_dict, codes_payload
  * router helpers — compose_address, make_case_no, due_band (each band + order),
    case_out, perf_dept_of, perf_window, the uq_jpac_live IntegrityError test
  * endpoints — validation 400 before the DB, happy paths, 404 other company,
    409 duplicate live case (SELECT guard AND IntegrityError) on open / reopen,
    rejoin refresh on list + get, candidates / list bind values, REPLY parks,
    own vs other's timeline entries, performance through the Low Performer helpers
  * leave link (TKT-000223, spec 11.19) — build_leave_options / exit_options / labels, plan_leave_refresh
    (one test per rule), the leave SQL (approved status 3 only, pending 20 is a hint), /setup leave
    types, /candidates + /list + /get + /open + /reopen + /exit LEAVE + /resume with the automatic
    parking and no manual verification step
  * access — every endpoint's level (403 below it, checked on the stored case's
    branch for id-based calls), branch of another company 400 before access
"""

import inspect
import re
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pymysql
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from src.juteProduction import long_absenteeism as la
from src.juteProduction import long_absenteeism_common as lac
from src.juteProduction import long_absenteeism_query as laq
from src.juteProduction.long_absenteeism import (
    case_out,
    compose_address,
    due_band,
    get_current_user_with_refresh,
    get_tenant_db,
    long_absenteeism_router,
    make_case_no,
    perf_dept_of,
    perf_window,
)
from src.juteProduction.long_absenteeism_common import (
    LEVEL_CREATE,
    LEVEL_EDIT,
    LEVEL_VIEW,
    RULE_DEFAULTS,
    absent_days_live,
    canonical_exclude_prefixes,
    case_to_dict,
    codes_payload,
    due_date_for_next,
    exclude_prefix_regex,
    fmt_dmy,
    load_rule,
    parse_exclude_prefixes,
    merge_text,
    next_stage_of,
    previous_notices_sentence,
    reply_days_for,
    wait_days_for,
)
from src.juteProduction.long_absenteeism_models import (
    JuteProdAbsentCase,
    JuteProdAbsentEvent,
    JuteProdAbsentLetter,
    JuteProdAbsentRule,
)

PREFIX = "/api/longAbsenteeism"
app = FastAPI()
app.include_router(long_absenteeism_router, prefix=PREFIX)
client = TestClient(app)

TODAY = date(2026, 10, 11)


# =============================================================================
# Fakes
# =============================================================================


class Row(tuple):
    """A result row: positional like SQLAlchemy's Row, plus ._mapping."""

    def __new__(cls, m: dict):
        r = super().__new__(cls, tuple(m.values()))
        r._mapping = m
        return r


class Result:
    def __init__(self, rows):
        self._rows = [Row(m) for m in rows]

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


# SQL substrings that identify each query (see long_absenteeism_query.py).
Q_BRANCH = "FROM branch_mst WHERE branch_id"
Q_SNAPSHOT = "hrms_ed_address_details"
Q_LIVE = "case_id <> :exclude_id"
Q_COUNT = "COUNT(*) AS n"
Q_LAST_ATT = "daily_ebmc_attendance"
Q_LAST_SEEN = "lt.eb_id = :eb_id"
Q_REJOIN = "AS first_seen"
Q_SET_REJOIN = "SET rejoined_seen_date"
Q_LIST = "lp.w1_date"
Q_DEPTS = "FROM dept_mst"
Q_OTHERS = "case_id <> :case_id"
Q_LETTERS = "SELECT l.*"
Q_RECEIPTS = "jute_prod_absent_union_receipt"
Q_LETTER_ATT = "a.module = :letter_module"
Q_EVENTS = "FROM jute_prod_absent_event e"
Q_CANDIDATES = "WITH seen AS"
# leave module link (TKT-000223)
Q_CASE_LEAVE = "AS stored_ltid"
Q_LEAVE_TYPES = "ORDER BY leave_type_description, leave_type_code"
Q_LEAVE_TYPE = "AND leave_type_id = :leave_type_id"
Q_PENDING = "l3.eb_id = :eb_id"


class FakeDB:
    def __init__(self):
        self.routes = []
        self.calls = []
        self.objects = {}
        self.added = []
        self._next_id = 500
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.flush = MagicMock(side_effect=self._flush)
        self.on(Q_BRANCH, [{"ok": 1}])   # branch 2 belongs to company 1

    def on(self, needle, rows):
        """Later registrations win; ``rows`` may be a callable(params)."""
        self.routes.insert(0, (needle, rows))

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        for needle, rows in self.routes:
            if needle in sql:
                return Result(rows(params) if callable(rows) else rows)
        return Result([])

    def get(self, model, pk):
        return self.objects.get((model, int(pk)))

    def put(self, obj):
        pk = sa_inspect(type(obj)).primary_key[0].key
        self.objects[(type(obj), int(getattr(obj, pk)))] = obj
        return obj

    def add(self, obj):
        self.added.append(obj)

    def _flush(self):
        for o in self.added:
            pk = sa_inspect(type(o)).primary_key[0].key
            if getattr(o, pk) is None:
                setattr(o, pk, self._next_id)
                self._next_id += 1

    def params(self, needle):
        return [p for s, p in self.calls if needle in s]

    def ran(self, needle) -> bool:
        return any(needle in s for s, _ in self.calls)

    def added_of(self, model):
        return [o for o in self.added if isinstance(o, model)]


def make_case(**kw) -> JuteProdAbsentCase:
    base = dict(
        case_id=1, co_id=1, branch_id=2, eb_id=3, case_no="LA/2/26/0001", emp_code="10000",
        emp_name="Ram Das", dept_id=5, dept_desc="WEAVING", sub_dept_desc="LOOMS",
        designation="Weaver", last_working_date=date(2026, 9, 20), detected_date=date(2026, 10, 1),
        absent_days_at_open=11, stage_code="DETECTED", stage_date=date(2026, 10, 1),
        next_due_date=date(2026, 10, 1), status=1, habitual=0, timekeeper_confirmed=0, active=1,
        rejoined_seen_date=None, status_id=None, approval_level=None, updated_by=7, leave_parked=0,
    )
    base.update(kw)
    return JuteProdAbsentCase(**base)


def case_row(**kw) -> dict:
    """A get_case_list_query row: every case column + the letter pivot."""
    letter_keys = {f"{p}_{s}" for p in ("w1", "w2", "w3", "sc", "gn") for s in ("date", "sent")}
    c = make_case(**{k: v for k, v in kw.items() if k not in letter_keys})
    m = {col.name: getattr(c, col.name) for col in JuteProdAbsentCase.__table__.columns}
    for k in sorted(letter_keys):
        m[k] = kw.get(k)
    return m


def event_row(**kw) -> dict:
    m = {col.name: None for col in JuteProdAbsentEvent.__table__.columns}
    m.update(event_id=9, case_id=1, event_type="REMARK", event_date=date(2026, 10, 5), text="x",
             active=1, updated_by=7, attachment_id=None, attachment_filename=None)
    m.update(kw)
    return m


def letter_row(**kw) -> dict:
    m = {col.name: None for col in JuteProdAbsentLetter.__table__.columns}
    m.update(letter_id=11, case_id=1, stage_code="WARNING_1", letter_no=1, template_code="WARNING_1",
             issue_date=date(2026, 10, 2), body="text", active=1, forced=0)
    m.update(kw)
    return m


def leave_row(case_id=1, **kw) -> dict:
    """A get_case_leave_query row (TKT-000223): the case state, the stored leave link
    (stored_*) and the worker's approved leave of the leave module (the lv columns,
    all None when the SQL finds none)."""
    m = dict(case_id=case_id, eb_id=3, status=1, status_id=None, stage_code="VERIFIED",
             next_due_date=TODAY, rejoined_seen_date=None, leave_parked=0,
             stored_ltid=None, stored_type_id=None, stored_type_name=None, stored_from=None, stored_to=None,
             leave_transaction_id=None, leave_type_id=None, leave_from_date=None, leave_to_date=None,
             leave_type_name=None)
    m.update(kw)
    return m


# An approved STL LEAVE (leave_transactions.status 3) that covers TODAY (11.10.2026), as the
# lv columns of a leave_row, as the stored_* columns, and as a case's own leave columns.
LV_STL = dict(leave_transaction_id=901, leave_type_id=24, leave_type_name="STL LEAVE",
              leave_from_date=date(2026, 10, 5), leave_to_date=date(2026, 10, 20))
STORED_STL = dict(stored_ltid=901, stored_type_id=24, stored_type_name="STL LEAVE",
                  stored_from=date(2026, 10, 5), stored_to=date(2026, 10, 20))
CASE_STL = dict(leave_transaction_id=901, leave_type_id=24, leave_type_name="STL LEAVE",
                leave_from=date(2026, 10, 5), leave_to=date(2026, 10, 20))
AUTO_VERIFY_TEXT = "Verification step removed — 1st warning due now (leave module checked automatically)"
PARKED_TEXT = "On STL LEAVE 05.10.2026 to 20.10.2026 (leave module)"
# the leave types of sls company 2 (hrms_leave_types_mst), in the order the issue quotes them
SLS_LEAVE_TYPES = [
    {"leave_type_id": 14, "leave_type_code": "A", "leave_type_description": "AUTHORISED LEAVE"},
    {"leave_type_id": 26, "leave_type_code": "C", "leave_type_description": "ACCIDENT LEAVE"},
    {"leave_type_id": 25, "leave_type_code": "S", "leave_type_description": "SICK LEAVE"},
    {"leave_type_id": 24, "leave_type_code": "L", "leave_type_description": "STL LEAVE"},
]


def norm(q) -> str:
    """A query's SQL on one line (single spaces) for substring assertions."""
    return " ".join(str(q).split())


SNAPSHOT = {
    "eb_id": 3, "emp_status": 21, "emp_code": "10000", "emp_name": "Ram Das", "father_name": "Shyam Das",
    "mobile": "9800000000", "date_of_join": date(2010, 4, 1), "dept_id": 5, "dept_desc": "WEAVING",
    "sub_dept_desc": "LOOMS", "designation": "Weaver", "category": "PERMANENT",
    "address_line_1": "12 Mill Road", "address_line_2": "", "city_name": "Howrah",
    "state_name": "West Bengal", "pin_code": 711101, "esi_no": "4005", "pf_no": "3457",
}


def _integrity(code=1062, key="uq_jpac_live"):
    orig = pymysql.err.IntegrityError(code, f"Duplicate entry '1-1-2-3' for key 'jute_prod_absent_case.{key}'")
    return IntegrityError("INSERT INTO jute_prod_absent_case ...", {}, orig)


class _Base:
    """App overrides + edit access + a fixed IST today for every endpoint test."""

    level = LEVEL_EDIT

    def setup_method(self):
        self.db = FakeDB()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        self._patches = [
            patch.object(lac, "access_level", return_value=self.level),
            patch.object(la, "today_ist", return_value=TODAY),
        ]
        self.access = self._patches[0].start()
        self._patches[1].start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        app.dependency_overrides.clear()


# =============================================================================
# Pure helpers of long_absenteeism_common
# =============================================================================


class TestCommonHelpers:
    def test_fmt_dmy(self):
        assert fmt_dmy(date(2026, 1, 5)) == "05.01.2026"
        assert fmt_dmy(datetime(2026, 1, 5, 14, 30)) == "05.01.2026"
        assert fmt_dmy("2026-01-05") == "05.01.2026"
        assert fmt_dmy("2026-01-05T10:00:00") == "05.01.2026"
        assert fmt_dmy(None) == "" and fmt_dmy("") == ""
        assert fmt_dmy("not a date") == "not a date"

    def test_merge_text(self):
        tpl = "To {name} ({eb_no})\n{designation}\n\n\n\nRef {xyz} {case_no}"
        out = merge_text(tpl, {"name": "Ram", "eb_no": 10000, "designation": None, "case_no": " "})
        # None = missing data (underscores); "" / blank = intentionally empty (dropped);
        # the template's own blank lines are kept as typed (no collapsing)
        assert out == "To Ram (10000)\n________\n\n\n\nRef {xyz} "
        assert merge_text("\n\n{name}\n", {"name": "A"}) == "\n\nA\n"
        assert merge_text(None, {}) == ""

    def test_merge_text_drops_only_the_line_of_an_emptied_token(self):
        f = {"previous_notices": "", "signatory": "Personnel Manager", "name": "Ram"}
        # an emptied paragraph goes with the blank line above it: one gap stays
        assert merge_text("A\n\n{previous_notices}\n\nB", f) == "A\n\nB"
        assert merge_text("{previous_notices}\n\nA", f) == "A"
        assert merge_text("A\n\n{previous_notices}", f) == "A"
        assert merge_text("A\n  {previous_notices}\t\nB", f) == "A\nB"
        assert merge_text("A\r\n{previous_notices}\r\nB", f) == "A\r\nB"
        # last line without a newline of its own: the newline before it goes instead
        assert merge_text("A\n{previous_notices}", f) == "A"
        assert merge_text("{previous_notices}", f) == ""
        # the signature gap of the default templates survives
        assert merge_text("company.\n\n\n{signatory}", f) == "company.\n\n\nPersonnel Manager"
        # not alone on its line -> only the token disappears; None / unknown keep the line
        assert merge_text("Ref {previous_notices}.\nB", f) == "Ref .\nB"
        assert merge_text("A\n{address}\nB", {"address": None}) == "A\n________\nB"
        assert merge_text("A\n{unknown}\nB", f) == "A\n{unknown}\nB"
        assert merge_text("A\n{name}\nB", f) == "A\nRam\nB"

    def test_letters_issued_sentence(self):
        letters = [
            {"stage_code": "WARNING_1", "issue_date": date(2026, 9, 1), "sent_date": date(2026, 9, 3),
             "ad_no": "RA123"},
            {"stage_code": "WARNING_2", "issue_date": "2026-09-15", "sent_date": None},
            {"stage_code": "WARNING_3", "issue_date": "2026-09-26", "sent_date": "2026-09-27"},
            {"stage_code": "SHOW_CAUSE", "issue_date": "2026-10-01", "sent_date": "2026-10-02", "ad_no": ""},
            {"stage_code": "GATE_NOTICE", "issue_date": "2026-10-06", "sent_date": "2026-10-06",
             "gate_display_date": "2026-10-07"},
            {"stage_code": "STRUCK_OFF", "issue_date": "2026-10-20"},
        ]
        assert lac.letters_issued_sentence(letters) == (
            "1st warning letter dated 01.09.2026, sent 03.09.2026 (A.D. No. RA123); "
            "2nd warning letter dated 15.09.2026; "
            "3rd warning letter dated 26.09.2026, sent 27.09.2026; "
            "show cause notice dated 01.10.2026, sent 02.10.2026; "
            "gate notice dated 06.10.2026, displayed 07.10.2026"
        )
        # gate notice without a display date shows its sent date; nothing issued -> ""
        gn = {"stage_code": "GATE_NOTICE", "issue_date": "2026-10-06", "sent_date": "2026-10-08"}
        assert lac.letters_issued_sentence([gn]) == "gate notice dated 06.10.2026, displayed 08.10.2026"
        assert lac.letters_issued_sentence([]) == ""
        # neutral wording: never addressed to the worker
        out = lac.letters_issued_sentence(letters)
        assert "you" not in out and "our " not in out

    def test_previous_notices_empty_for_the_first_warning(self):
        assert previous_notices_sentence([]) == ""
        assert previous_notices_sentence([{"stage_code": "STRUCK_OFF", "issue_date": "2026-10-01"}]) == ""

    def test_previous_notices_one_and_two_letters(self):
        w1 = {"stage_code": "WARNING_1", "issue_date": date(2026, 9, 1), "sent_date": date(2026, 9, 3),
              "dispatch_mode": "REG_POST_AD", "ad_no": "RA123"}
        one = previous_notices_sentence([w1])
        assert one == (
            "This is in continuation of our 1st warning letter dated 01.09.2026, sent on 03.09.2026 "
            "by registered post with A.D. (A.D. No. RA123), to which no reply has been received from "
            "you. This is the 2nd notice being issued to you on the same matter."
        )
        w2 = {"stage_code": "WARNING_2", "issue_date": "2026-09-15", "sent_date": None}
        two = previous_notices_sentence([w1, w2])
        assert "(A.D. No. RA123) and our 2nd warning letter dated 15.09.2026, to which" in two
        assert two.endswith("This is the 3rd notice being issued to you on the same matter.")
        sc = {"stage_code": "SHOW_CAUSE", "issue_date": "2026-10-01", "sent_date": "2026-10-02",
              "dispatch_mode": "HAND"}
        four = previous_notices_sentence([w1, w2, {"stage_code": "WARNING_3", "issue_date": "2026-09-25"}, sc])
        assert "our show cause notice dated 01.10.2026, sent on 02.10.2026 by hand" in four
        assert "This is the 5th notice" in four

    def test_wait_and_reply_tables(self):
        assert [wait_days_for(s, RULE_DEFAULTS) for s in
                ("WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE", "STRUCK_OFF", "X")] == \
            [0, 10, 10, 15, 3, 7, 0]
        assert [reply_days_for(s, RULE_DEFAULTS) for s in
                ("WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE", "STRUCK_OFF")] == \
            [7, 7, 7, 3, 7, 0]
        rule = {**RULE_DEFAULTS, "wait_w2_days": 4, "sc_reply_days": 5, "notice_reply_days": 9}
        assert wait_days_for("WARNING_2", rule) == 4
        assert wait_days_for("GATE_NOTICE", rule) == 5 and reply_days_for("SHOW_CAUSE", rule) == 5
        assert wait_days_for("STRUCK_OFF", rule) == 9 and reply_days_for("GATE_NOTICE", rule) == 9

    def test_next_stage_of(self):
        assert next_stage_of(make_case(stage_code="DETECTED")) is None
        assert next_stage_of(make_case(stage_code="VERIFIED")) == "WARNING_1"
        assert next_stage_of(make_case(stage_code="WARNING_3")) == "SHOW_CAUSE"
        assert next_stage_of(make_case(stage_code="GATE_NOTICE")) == "STRUCK_OFF"
        assert next_stage_of(make_case(stage_code="STRUCK_OFF")) is None

    def test_due_date_for_next(self):
        """The FIRST allowed day: a wait of N days after the base opens on base + N + 1."""
        rule = dict(RULE_DEFAULTS)
        verified = make_case(stage_code="VERIFIED", stage_date=date(2026, 10, 3))
        assert due_date_for_next(verified, None, rule) == date(2026, 10, 3)   # 1st warning: no wait
        w1 = make_case(stage_code="WARNING_1", stage_date=date(2026, 10, 4))
        sent = JuteProdAbsentLetter(issue_date=date(2026, 10, 4), sent_date=date(2026, 10, 6))
        unsent = JuteProdAbsentLetter(issue_date=date(2026, 10, 4), sent_date=None)
        assert due_date_for_next(w1, sent, rule) == date(2026, 10, 17)      # sent 06.10 + 10 days -> 17.10
        assert due_date_for_next(w1, unsent, rule) == date(2026, 10, 15)    # issue + 10 + 1 until sent
        assert due_date_for_next(w1, None, rule) == date(2026, 10, 15)      # stage_date fallback
        w3 = make_case(stage_code="WARNING_3", stage_date=date(2026, 10, 1))
        assert due_date_for_next(w3, sent, rule) == date(2026, 10, 22)      # show cause: 15 days, then
        gn = make_case(stage_code="GATE_NOTICE")
        assert due_date_for_next(gn, sent, rule) == date(2026, 10, 14)      # struck off: day after the 7
        assert due_date_for_next(make_case(stage_code="STRUCK_OFF"), sent, rule) is None
        zero = dict(rule, wait_w2_days=0)
        assert due_date_for_next(w1, sent, zero) == date(2026, 10, 6)       # no wait: the base day
        assert lac.first_allowed_day(date(2026, 9, 3), 10) == date(2026, 9, 14)
        assert lac.first_allowed_day(date(2026, 9, 3), 0) == date(2026, 9, 3)

    def test_load_rule_defaults_and_overrides(self):
        db = MagicMock()
        db.get.return_value = None
        rule = load_rule(db, "4")
        assert rule == {**RULE_DEFAULTS, "co_id": 4}
        assert db.get.call_args[0] == (JuteProdAbsentRule, 4)
        db.get.return_value = JuteProdAbsentRule(
            co_id=4, detect_days=14, lookback_days=60, wait_w2_days=None, exclude_t_codes=0,
            exclude_prefixes=" t c,r ", clause_text="  ", signatory="Mill Manager", extra_1_label="Union")
        rule = load_rule(db, 4)
        assert rule["detect_days"] == 14 and rule["lookback_days"] == 60
        assert rule["wait_w2_days"] == RULE_DEFAULTS["wait_w2_days"]      # NULL keeps the default
        assert rule["exclude_t_codes"] == RULE_DEFAULTS["exclude_t_codes"]  # retired: the row is not read
        assert rule["exclude_prefixes"] == "T,C,R"                         # canonical form
        assert rule["clause_text"] == RULE_DEFAULTS["clause_text"]        # blank keeps the default
        assert rule["signatory"] == "Mill Manager" and rule["extra_1_label"] == "Union"

    def test_load_rule_exclude_prefixes_default_when_null_or_blank(self):
        assert RULE_DEFAULTS["exclude_prefixes"] == "T,C,R,L"
        db = MagicMock()
        for stored in (None, "", "   ", "1, 2"):          # NULL / blank / no letter -> the code default
            db.get.return_value = JuteProdAbsentRule(co_id=4, exclude_prefixes=stored)
            assert load_rule(db, 4)["exclude_prefixes"] == "T,C,R,L", stored
        db.get.return_value = JuteProdAbsentRule(co_id=4, exclude_prefixes="L")
        assert load_rule(db, 4)["exclude_prefixes"] == "L"

    def test_parse_exclude_prefixes(self):
        assert parse_exclude_prefixes("T,C,R,L") == ["T", "C", "R", "L"]
        # case, spaces / commas as separators, repeats dropped in first-seen order
        assert parse_exclude_prefixes(" t c,,r ,T\tl ") == ["T", "C", "R", "L"]
        # only single ASCII letters survive
        assert parse_exclude_prefixes("TX, 1, é, Ω, K, %, _") == ["K"]
        for empty in (None, "", "  ", ",,", "12, TX"):
            assert parse_exclude_prefixes(empty) == [], empty
        assert canonical_exclude_prefixes("r t r") == "R,T"
        assert canonical_exclude_prefixes(" , ") is None

    def test_exclude_prefix_regex(self):
        assert exclude_prefix_regex("T,C,R,L") == "^[TCRL]"
        assert exclude_prefix_regex("t, c") == "^[TC]"
        assert exclude_prefix_regex("a a A") == "^[A]"
        for empty in (None, "", " , ", "1,2", "TX"):
            assert exclude_prefix_regex(empty) is None, empty
        # nothing but the validated letters ever reaches the pattern
        assert exclude_prefix_regex("T,'],x OR 1=1 --") == "^[TX]"

    def test_absent_days_live(self):
        c = make_case(last_working_date=date(2026, 9, 20))
        assert absent_days_live(c, TODAY) == 21
        c.rejoined_seen_date = date(2026, 9, 30)
        assert absent_days_live(c, TODAY) == 10                           # stops at the rejoin
        assert absent_days_live(make_case(last_working_date=date(2026, 10, 12)), TODAY) == 0

    def test_case_to_dict_labels(self):
        d = case_to_dict(make_case(stage_code="WARNING_2", status=2, exit_reason=None), TODAY)
        assert d["stage_label"] == "2nd warning" and d["stage_order"] == 3
        assert d["status_label"] == "Parked" and d["absent_days"] == 21
        assert d["last_working_date"] == "2026-09-20"

    def test_codes_payload(self):
        p = codes_payload()
        user_exits = [e["code"] for e in p["exit_reasons"]]
        assert "STRUCK_OFF" not in user_exits and "DISMISSED" not in user_exits and "ESI" in user_exits
        assert p["user_event_types"] == ["REMARK", "FEEDBACK", "REPLY"]
        assert [s["code"] for s in p["stages"]][0] == "DETECTED"
        # owner 2026-10-11 (later): one template per warning, the single WARNING is gone
        assert [t["code"] for t in p["template_codes"]] == [
            "WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE", "STRUCK_OFF_MEMO"]
        assert p["template_codes"][2]["label"] == "3rd & last warning letter"

    # ---- leave types and the automatic leave link (TKT-000223) ---------------

    def test_leave_exit_reason_and_the_legacy_results(self):
        assert lac.EXIT_REASONS["LEAVE"] == "On leave" and "LEAVE" in lac.USER_EXIT_REASONS
        # /verify survives for API compatibility only: still exactly NONE / ESI / STL
        assert list(lac.ESI_STL_RESULTS) == ["NONE", "ESI", "STL"]
        assert lac.RETURN_DATE_EXIT_REASONS == ("ESI", "STL", "MATERNITY", "MEDICAL", "LEAVE")
        p = codes_payload()
        assert "LEAVE" in [e["code"] for e in p["exit_reasons"]]
        assert [e["code"] for e in p["esi_stl_results"]] == ["NONE", "ESI", "STL"]
        assert lac.exit_reason_label("LEAVE", "STL LEAVE") == "On leave: STL LEAVE"
        assert lac.exit_reason_label("LEAVE", None) == "On leave" == lac.exit_reason_label("LEAVE", "  ")
        assert lac.exit_reason_label("ESI") == "On ESI" and lac.exit_reason_label(None) is None
        assert lac.exit_reason_label("WEIRD") == "WEIRD"
        assert lac.esi_stl_result_label("ESI") == "On ESI" and lac.esi_stl_result_label("NONE") == "Not on ESI / STL"
        assert lac.esi_stl_result_label(None) is None

    def test_leave_option_label_and_type_name(self):
        row = lambda **kw: {"leave_type_id": 7, **kw}  # noqa: E731
        assert lac.leave_option_label(row(leave_type_description="STL LEAVE", leave_type_code="L")) == "STL LEAVE (L)"
        # description and code the same word (any case) -> shown once, the description's spelling
        assert lac.leave_option_label(row(leave_type_description="Sick", leave_type_code="SICK")) == "Sick"
        assert lac.leave_option_label(row(leave_type_description=" ", leave_type_code=" L ")) == "L"
        assert lac.leave_option_label(row(leave_type_description=" Casual ", leave_type_code=None)) == "Casual"
        assert lac.leave_option_label(row()) == "Leave type 7"
        # the name a case keeps: description, else the code, cut to the column width
        assert lac.leave_type_name_of(row(leave_type_description="STL LEAVE", leave_type_code="L")) == "STL LEAVE"
        assert lac.leave_type_name_of(row(leave_type_description="  ", leave_type_code="L")) == "L"
        assert lac.leave_type_name_of(row()) is None
        assert len(lac.leave_type_name_of(row(leave_type_description="x" * 150))) == lac.LEAVE_TYPE_NAME_MAX == 100

    def test_build_leave_options_sls_company_2(self):
        opts = lac.build_leave_options(SLS_LEAVE_TYPES)
        assert [(o["code"], o["leave_type_id"], o["label"], o["legacy_code"], o["source"]) for o in opts] == [
            ("LEAVE", 14, "AUTHORISED LEAVE (A)", None, "master"),
            ("LEAVE", 26, "ACCIDENT LEAVE (C)", None, "master"),
            ("LEAVE", 25, "SICK LEAVE (S)", None, "master"),
            ("LEAVE", 24, "STL LEAVE (L)", "STL", "master"),       # the STL type carries the legacy STL code
            ("ESI", None, "On ESI", "ESI", "legacy"),              # no ESI / maternity type: still offered
            ("MATERNITY", None, "Maternity leave", "MATERNITY", "legacy"),
        ]
        assert not [o for o in opts if o["code"] == "STL"]         # no stand-alone STL

    def test_build_leave_options_legacy_matching(self):
        # code equality (any case) takes the legacy code, whatever the description says
        opts = lac.build_leave_options([{"leave_type_id": 31, "leave_type_code": "esi",
                                         "leave_type_description": "Employees insurance"}])
        assert [(o["code"], o["leave_type_id"], o["legacy_code"], o["source"]) for o in opts] == [
            ("LEAVE", 31, "ESI", "master"), ("STL", None, "STL", "legacy"), ("MATERNITY", None, "MATERNITY", "legacy")]
        # the description pattern finds maternity leave; an STL word inside another word does not match
        opts = lac.build_leave_options([
            {"leave_type_id": 1, "leave_type_code": "M", "leave_type_description": "Maternity benefit"},
            {"leave_type_id": 2, "leave_type_code": "H", "leave_type_description": "Hostler leave"}])
        assert [(o["leave_type_id"], o["legacy_code"]) for o in opts[:2]] == [(1, "MATERNITY"), (2, None)]
        assert [o["code"] for o in opts[2:]] == ["ESI", "STL"] and all(o["source"] == "legacy" for o in opts[2:])
        # a legacy code goes to the FIRST matching type only, and a type carries one legacy code
        opts = lac.build_leave_options([
            {"leave_type_id": 5, "leave_type_code": "STL", "leave_type_description": "STL one"},
            {"leave_type_id": 6, "leave_type_code": "STL2", "leave_type_description": "STL two"},
            {"leave_type_id": None, "leave_type_code": "ESI", "leave_type_description": "no id: ignored"}])
        assert [(o["leave_type_id"], o["legacy_code"]) for o in opts if o["source"] == "master"] == [(5, "STL"), (6, None)]
        assert [o["code"] for o in opts if o["source"] == "legacy"] == ["ESI", "MATERNITY"]

    def test_build_leave_options_empty_master_is_the_three_legacy_entries(self):
        for empty in ([], None):
            opts = lac.build_leave_options(empty)
            assert opts == [
                {"code": "ESI", "leave_type_id": None, "label": "On ESI", "legacy_code": "ESI", "source": "legacy"},
                {"code": "STL", "leave_type_id": None, "label": "On STL", "legacy_code": "STL", "source": "legacy"},
                {"code": "MATERNITY", "leave_type_id": None, "label": "Maternity leave",
                 "legacy_code": "MATERNITY", "source": "legacy"},
            ]

    def test_exit_options_order_and_labels(self):
        leave_options = lac.build_leave_options(SLS_LEAVE_TYPES)
        before = [dict(o) for o in leave_options]
        opts = lac.exit_options(leave_options)
        assert [(o["code"], o["leave_type_id"]) for o in opts] == [
            ("REJOINED", None), ("LEAVE", 14), ("LEAVE", 26), ("LEAVE", 25), ("LEAVE", 24),
            ("ESI", None), ("MATERNITY", None), ("MEDICAL", None), ("RESIGNED", None), ("EXPIRED", None),
            ("OTHER", None)]
        assert [o["label"] for o in opts] == [
            "Rejoined", "On leave: AUTHORISED LEAVE (A)", "On leave: ACCIDENT LEAVE (C)", "On leave: SICK LEAVE (S)",
            "On leave: STL LEAVE (L)", "On ESI", "Maternity leave", "Medical report submitted", "Resigned",
            "Expired", "Other"]
        assert [o["source"] for o in opts] == ["builtin"] + ["master"] * 4 + ["legacy"] * 2 + ["builtin"] * 4
        assert opts[4]["legacy_code"] == "STL"
        assert leave_options == before                      # the leave options themselves are not relabelled
        # no master types: Rejoined, the legacy trio, then the built-ins; STRUCK_OFF / DISMISSED never offered
        codes = [o["code"] for o in lac.exit_options(lac.build_leave_options([]))]
        assert codes == ["REJOINED", "ESI", "STL", "MATERNITY", "MEDICAL", "RESIGNED", "EXPIRED", "OTHER"]
        assert lac.exit_options(None)[0]["code"] == "REJOINED" and len(lac.exit_options(None)) == 5

    def test_case_to_dict_leave_fields(self):
        d = case_to_dict(make_case(**CASE_STL), TODAY)
        assert d["on_leave_now"] is True and d["leave_parked"] == 0
        assert d["on_leave_label"] == "STL LEAVE 05.10.2026 – 20.10.2026 (leave module)"
        assert (d["leave_transaction_id"], d["leave_type_id"], d["leave_from"], d["leave_to"]) == (
            901, 24, "2026-10-05", "2026-10-20")
        assert d["esi_stl_result_label"] is None and d["exit_reason_label"] is None
        # ended: no longer on leave now, the same wording (the UI words "ended" from the dates)
        d = case_to_dict(make_case(**{**CASE_STL, "leave_to": date(2026, 10, 8)}), TODAY)
        assert d["on_leave_now"] is False and d["on_leave_label"] == "STL LEAVE 05.10.2026 – 08.10.2026 (leave module)"
        # not started yet
        fut = {**CASE_STL, "leave_from": date(2026, 11, 1), "leave_to": date(2026, 11, 20)}
        d = case_to_dict(make_case(**fut), TODAY)
        assert d["on_leave_now"] is False
        assert d["on_leave_label"] == "STL LEAVE from 01.11.2026 to 20.11.2026 (leave module)"
        # on the first / last day it still covers
        assert case_to_dict(make_case(**{**CASE_STL, "leave_from": TODAY}), TODAY)["on_leave_now"] is True
        assert case_to_dict(make_case(**{**CASE_STL, "leave_to": TODAY}), TODAY)["on_leave_now"] is True
        # an unnamed type reads "Leave"
        d = case_to_dict(make_case(**{**CASE_STL, "leave_type_name": None}), TODAY)
        assert d["on_leave_label"] == "Leave 05.10.2026 – 20.10.2026 (leave module)"
        # not linked: none, even with stale dates or a manual type pick
        d = case_to_dict(make_case(), TODAY)
        assert d["on_leave_now"] is False and d["on_leave_label"] is None
        stale = make_case(leave_from=date(2026, 10, 5), leave_to=date(2026, 10, 20), leave_type_id=24)
        d = case_to_dict(stale, TODAY)
        assert d["on_leave_now"] is False and d["on_leave_label"] is None
        # legacy verification result
        assert case_to_dict(make_case(esi_stl_result="ESI"), TODAY)["esi_stl_result_label"] == "On ESI"
        assert case_to_dict(make_case(esi_stl_result="NONE"), TODAY)["esi_stl_result_label"] == "Not on ESI / STL"

    def test_case_to_dict_manual_leave_exit(self):
        c = make_case(status=0, exit_reason="LEAVE", leave_type_id=25, leave_type_name="SICK LEAVE",
                      expected_return_date=date(2026, 10, 25))
        d = case_to_dict(c, TODAY)
        assert d["exit_reason_label"] == "On leave: SICK LEAVE"
        assert d["on_leave_label"] == "SICK LEAVE till 25.10.2026" and d["on_leave_now"] is False
        c.expected_return_date = None
        assert case_to_dict(c, TODAY)["on_leave_label"] == "SICK LEAVE"
        c.leave_type_name = None
        d = case_to_dict(c, TODAY)
        assert d["exit_reason_label"] == "On leave" and d["on_leave_label"] == "Leave"
        # no type kept -> nothing to say; another exit reason with a type kept -> nothing either
        assert case_to_dict(make_case(status=0, exit_reason="LEAVE"), TODAY)["on_leave_label"] is None
        other = make_case(status=0, exit_reason="RESIGNED", leave_type_id=25, leave_type_name="SICK LEAVE")
        assert case_to_dict(other, TODAY)["on_leave_label"] is None
        # a linked module leave is worded first
        both = make_case(status=0, exit_reason="LEAVE", expected_return_date=date(2026, 10, 25), **CASE_STL)
        assert case_to_dict(both, TODAY)["on_leave_label"] == "STL LEAVE 05.10.2026 – 20.10.2026 (leave module)"

    def test_auto_verified_fields(self):
        assert lac.AUTO_VERIFY_NOTE == "Leave module checked automatically"
        assert lac.auto_verified_fields(TODAY) == {
            "stage_code": "VERIFIED", "stage_date": TODAY, "verified_date": TODAY,
            "verify_note": "Leave module checked automatically", "next_due_date": TODAY}
        # a parked case keeps the next_due_date it has
        assert "next_due_date" not in lac.auto_verified_fields(TODAY, parked=True)
        assert lac.auto_verified_fields(TODAY, parked=True)["stage_code"] == "VERIFIED"
        assert lac.AUTO_VERIFY_EVENT == AUTO_VERIFY_TEXT

    def test_leave_event_texts(self):
        f, t = date(2026, 10, 5), date(2026, 10, 20)
        assert lac.leave_parked_event("STL LEAVE", f, t) == "On STL LEAVE 05.10.2026 to 20.10.2026 (leave module)"
        assert lac.leave_parked_event("Sick", f, t) == "On Sick leave 05.10.2026 to 20.10.2026 (leave module)"
        assert lac.leave_parked_event(None, f, t) == "On leave 05.10.2026 to 20.10.2026 (leave module)"
        assert lac.leave_ended_event(date(2026, 10, 8)) == "Leave ended 08.10.2026, still absent"
        assert lac.LEAVE_WITHDRAWN_EVENT == "Leave record withdrawn in the leave module; case resumed"

    def test_each_warning_has_its_own_template(self):
        assert {s: lac.TEMPLATE_OF_STAGE[s] for s in ("WARNING_1", "WARNING_2", "WARNING_3")} == {
            "WARNING_1": "WARNING_1", "WARNING_2": "WARNING_2", "WARNING_3": "WARNING_3"}
        assert set(lac.TEMPLATE_OF_STAGE.values()) == set(lac.TEMPLATE_LABELS) == set(lac.TEMPLATE_DEFAULTS)
        assert "WARNING" not in lac.TEMPLATE_LABELS
        # letters issued before the split keep a readable label
        assert lac.template_label("WARNING") == "Warning letter (1st / 2nd / 3rd)"
        assert lac.template_label("WARNING_2") == "2nd warning letter"
        assert lac.template_label("XYZ") == "XYZ" and lac.template_label(None) is None

    def test_habitual_note_sentence(self):
        assert lac.habitual_note_sentence(1) == (
            "Your attendance record also shows that you have been habitually absent without leave.")
        assert lac.habitual_note_sentence(True) == lac.HABITUAL_NOTE
        for v in (0, None, False, "x"):
            assert lac.habitual_note_sentence(v) == ""

    def test_earlier_cases_sentence(self):
        rows = [  # get_other_cases_query order: newest first
            {"case_id": 30, "case_no": None, "detected_date": date(2026, 3, 1), "status": 0,
             "exit_reason": "DISMISSED"},
            {"case_id": 20, "case_no": "LA/2/26/0003", "detected_date": "2026-02-02", "status": 0,
             "exit_reason": "ESI"},
            {"case_id": 25, "case_no": "LA/2/26/0004", "detected_date": date(2026, 2, 20), "status": 1,
             "exit_reason": None},                                       # live: not an earlier case
            {"case_id": 12, "case_no": "LA/2/25/0012", "detected_date": date(2025, 8, 5), "status": 0,
             "exit_reason": "REJOINED"},
        ]
        assert lac.earlier_cases_sentence(rows) == (
            "Earlier long-absence cases on your record: LA/2/25/0012 (detected 05.08.2025, rejoined); "
            "LA/2/26/0003 (detected 02.02.2026, on ESI)."
        )
        # only cases detected up to the letter's own case
        assert lac.earlier_cases_sentence(rows, before=date(2026, 1, 1)) == (
            "Earlier long-absence cases on your record: LA/2/25/0012 (detected 05.08.2025, rejoined).")
        assert lac.earlier_cases_sentence(rows, before="2026-02-02").endswith("(detected 02.02.2026, on ESI).")
        # missing case number / reason / date stay readable
        odd = [{"case_id": 7, "case_no": None, "detected_date": None, "status": 0, "exit_reason": None}]
        assert lac.earlier_cases_sentence(odd) == "Earlier long-absence cases on your record: case 7."
        assert lac.earlier_cases_sentence([]) == "" and lac.earlier_cases_sentence(None) == ""
        assert lac.earlier_cases_sentence(rows[:1]) == ""                 # only a dismissal


# =============================================================================
# Pure helpers of the router
# =============================================================================


class TestRouterHelpers:
    def test_compose_address(self):
        assert compose_address(SNAPSHOT) == "12 Mill Road, Howrah, West Bengal, 711101"
        assert compose_address({"address_line_1": "", "pin_code": 0}) is None
        assert compose_address({}) is None
        assert len(compose_address({"address_line_1": "x" * 600})) == 500

    def test_make_case_no(self):
        # the sequence is the row's own case_id (collision-free)
        assert make_case_no(29, date(2026, 10, 11), 7) == "LA/29/26/0007"
        assert make_case_no(2, date(2009, 1, 1), 12345) == "LA/2/09/12345"

    def test_case_out_is_shared_by_both_routers(self):
        from src.juteProduction import long_absenteeism_letters as lal
        assert la.case_out is lac.case_out and lal.case_out is lac.case_out and la.due_band is lac.due_band

    def test_due_band_each_band(self):
        b = lambda **kw: due_band(make_case(**kw), TODAY)  # noqa: E731
        assert b(status=0, rejoined_seen_date=date(2026, 10, 5)) == "NONE"
        assert b(rejoined_seen_date=date(2026, 10, 5), status=2, status_id=20) == "MARKED_PRESENT"
        assert b(status=2, status_id=20) == "REPLY_REVIEW"
        assert b(stage_code="GATE_NOTICE", status_id=20, next_due_date=date(2026, 10, 1)) == "APPROVAL"
        assert b(stage_code="DETECTED") == "VERIFY"
        assert b(stage_code="VERIFIED", next_due_date=TODAY) == "LETTER_DUE"
        assert b(stage_code="WARNING_1", next_due_date=date(2026, 10, 12)) == "NONE"   # not due yet
        assert b(stage_code="WARNING_1", next_due_date=None) == "NONE"
        assert b(stage_code="STRUCK_OFF", next_due_date=date(2026, 1, 1)) == "NONE"

    def test_due_band_struck_off_due(self):
        """A gate-notice case whose window has ended is STRUCK_OFF_DUE (send for
        approval), never LETTER_DUE."""
        b = lambda **kw: due_band(make_case(stage_code="GATE_NOTICE", **kw), TODAY)  # noqa: E731
        assert b(next_due_date=TODAY) == "STRUCK_OFF_DUE"
        assert b(next_due_date=date(2026, 10, 1), status_id=4) == "STRUCK_OFF_DUE"      # rejected, reopened
        assert b(next_due_date=date(2026, 10, 12)) == "NONE"                            # window still running
        assert b(next_due_date=None) == "NONE"
        assert b(next_due_date=TODAY, status_id=20) == "APPROVAL"
        assert b(next_due_date=TODAY, status_id=3) == "NONE"
        assert b(next_due_date=TODAY, status=2) == "REPLY_REVIEW"
        assert b(next_due_date=TODAY, rejoined_seen_date=date(2026, 10, 9)) == "MARKED_PRESENT"
        for stage in ("VERIFIED", "WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE"):
            assert due_band(make_case(stage_code=stage, next_due_date=TODAY), TODAY) == "LETTER_DUE"
        d = case_out(make_case(stage_code="GATE_NOTICE", next_due_date=TODAY), TODAY)
        assert (d["due_band"], d["next_stage_code"], d["next_stage_label"]) == (
            "STRUCK_OFF_DUE", "STRUCK_OFF", "Struck off")
        assert lac.DUE_BANDS.index("STRUCK_OFF_DUE") < lac.DUE_BANDS.index("LETTER_DUE")

    def test_case_out_adds_next_stage_and_band(self):
        d = case_out(make_case(stage_code="WARNING_2", next_due_date=TODAY), TODAY)
        assert (d["next_stage_code"], d["next_stage_label"], d["due_band"]) == ("WARNING_3", "3rd & last warning", "LETTER_DUE")
        d = case_out(make_case(stage_code="WARNING_2", status=0), TODAY)
        assert d["next_stage_code"] is None and d["next_stage_label"] is None and d["due_band"] == "NONE"

    def test_due_band_on_leave(self):
        """ON_LEAVE (TKT-000223): the linked module leave covers today. Tested after
        MARKED_PRESENT and before REPLY_REVIEW / APPROVAL / VERIFY / the due bands."""
        b = lambda **kw: due_band(make_case(**{"stage_code": "VERIFIED", **CASE_STL, **kw}), TODAY)  # noqa: E731
        assert b() == "ON_LEAVE"                                          # not LETTER_DUE although due today
        assert b(status=2, leave_parked=1) == "ON_LEAVE"                  # parked by its leave: not REPLY_REVIEW
        assert b(status=2, status_id=20) == "ON_LEAVE"                    # ... nor APPROVAL
        assert b(stage_code="GATE_NOTICE", next_due_date=TODAY) == "ON_LEAVE"   # ... nor STRUCK_OFF_DUE
        assert b(stage_code="DETECTED") == "ON_LEAVE"                     # ... nor VERIFY (legacy rows)
        assert b(rejoined_seen_date=date(2026, 10, 9)) == "MARKED_PRESENT"     # rejoined wins
        assert b(status=2, rejoined_seen_date=date(2026, 10, 9)) == "MARKED_PRESENT"
        assert b(status=0) == "NONE"                                      # closed cases never have a band
        # both ends are inclusive
        assert b(leave_from=TODAY) == "ON_LEAVE" and b(leave_to=TODAY) == "ON_LEAVE"
        # an ended leave: the normal bands again
        ended = {"leave_to": date(2026, 10, 10)}
        assert b(**ended) == "LETTER_DUE" and b(status=2, **ended) == "REPLY_REVIEW"
        assert b(stage_code="DETECTED", **ended) == "VERIFY"
        # a leave that has not started yet
        fut = {"leave_from": date(2026, 10, 12), "leave_to": date(2026, 10, 30)}
        assert b(**fut) == "LETTER_DUE" and b(status=2, **fut) == "REPLY_REVIEW"
        # only a LINKED leave counts (stale dates alone, or a half-known span, never do)
        assert b(leave_transaction_id=None) == "LETTER_DUE"
        assert b(leave_to=None) == "LETTER_DUE" and b(leave_from=None) == "LETTER_DUE"
        d = case_out(make_case(stage_code="VERIFIED", next_due_date=TODAY, **CASE_STL), TODAY)
        assert (d["due_band"], d["next_stage_code"], d["on_leave_now"]) == ("ON_LEAVE", "WARNING_1", True)

    def test_due_bands_order_has_on_leave_right_after_marked_present(self):
        assert lac.DUE_BANDS.index("ON_LEAVE") == lac.DUE_BANDS.index("MARKED_PRESENT") + 1
        assert lac.DUE_BANDS == ("MARKED_PRESENT", "ON_LEAVE", "REPLY_REVIEW", "APPROVAL", "VERIFY",
                                 "STRUCK_OFF_DUE", "LETTER_DUE")

    def test_perf_dept_of(self):
        assert perf_dept_of("Weaver (Hessian)", "WEAVING", None) == "WEAVING"
        assert perf_dept_of(" SPINNER ", "SPINNING", None) == "SPINNING"
        assert perf_dept_of("Helper", "Winding Dept", None) == "WINDING"
        assert perf_dept_of("Helper", "Prep", "Cop winding") == "WINDING"
        assert perf_dept_of("Helper", "Batching", "Batching A") is None
        assert perf_dept_of(None, None, None) is None

    def test_perf_window(self):
        lwd = date(2026, 9, 20)
        for dept in ("WEAVING", "SPINNING", "WINDING"):
            assert perf_window(lwd, dept) == (date(2026, 8, 21), lwd)   # 31 days inclusive
        with patch.dict(la.MAX_DAYS_BY_DEPT, {"WEAVING": 10}):
            assert perf_window(lwd, "WEAVING") == (date(2026, 9, 11), lwd)

    def test_live_unique_violation(self):
        assert la._is_live_unique_violation(_integrity())
        assert la._is_live_unique_violation(_integrity(code=9999, key="uq_jpac_live"))
        assert not la._is_live_unique_violation(_integrity(code=1452, key="fk_x"))


# =============================================================================
# Setup / candidates / list / get
# =============================================================================


class TestReadEndpoints(_Base):
    def test_setup(self):
        self.db.on(Q_DEPTS, [{"dept_id": 5, "dept_desc": "WEAVING"}, {"dept_id": 6, "dept_desc": "SPINNING"}])
        resp = client.get(f"{PREFIX}/setup", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["depts"] == [{"dept_id": 5, "dept_desc": "WEAVING"}, {"dept_id": 6, "dept_desc": "SPINNING"}]
        assert d["access_level"] == LEVEL_EDIT and d["today"] == "2026-10-11"
        assert d["rule"]["detect_days"] == 10 and d["rule"]["co_id"] == 1
        assert d["rule"]["exclude_prefixes"] == "T,C,R,L"
        assert d["stages"][1] == {"code": "VERIFIED", "label": "Verified", "order": 1}
        assert self.db.params(Q_DEPTS) == [{"branch_id": 2}]
        assert self.db.params(Q_BRANCH) == [{"branch_id": 2, "co_id": 1}]

    def test_setup_without_view_is_403(self):
        self.access.return_value = 0
        resp = client.get(f"{PREFIX}/setup", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 403
        assert "view access to Long Absenteeism" in resp.json()["detail"]

    def test_params_required(self):
        assert client.get(f"{PREFIX}/setup", params={"co_id": 1}).status_code == 400
        assert client.get(f"{PREFIX}/candidates", params={"co_id": "x", "branch_id": 2}).status_code == 400
        assert client.get(f"{PREFIX}/get", params={"co_id": 1}).status_code == 400
        assert client.get(f"{PREFIX}/candidates", params={"co_id": 1, "branch_id": 2,
                                                          "as_on": "11/10/2026"}).status_code == 400
        assert not self.db.calls

    def test_candidates_bind_values_and_shape(self):
        self.db.put(JuteProdAbsentRule(co_id=1, detect_days=12, lookback_days=60, exclude_t_codes=0,
                                       exclude_prefixes="t, a"))
        self.db.on(Q_CANDIDATES, [{
            "eb_id": 3, "emp_code": "10000", "emp_name": "Ram Das", "dept_id": 5, "dept_desc": "WEAVING",
            "sub_dept_desc": "LOOMS", "designation": "Weaver", "category": "PERMANENT",
            "last_seen": date(2026, 9, 20), "absent_days": 21, "closed_cases": 2}])
        resp = client.get(f"{PREFIX}/candidates", params={
            "co_id": 1, "branch_id": 2, "as_on": "2026-10-11", "dept_id": 5})
        assert resp.status_code == 200, resp.text
        assert self.db.params(Q_CANDIDATES) == [{
            "co_id": 1, "branch_id": 2, "from_date": date(2026, 8, 12), "as_on": date(2026, 10, 11),
            "detect_days": 12, "exclude_regex": "^[TA]", "dept_id": 5}]
        assert resp.json()["data"] == [{
            "eb_id": 3, "emp_code": "10000", "emp_name": "Ram Das", "dept_id": 5, "dept_desc": "WEAVING",
            "sub_dept_desc": "LOOMS", "designation": "Weaver", "category": "PERMANENT",
            "last_seen": "2026-09-20", "absent_days": 21, "closed_cases": 2,
            # TKT-000223: the leave link of the row (all None when the SQL finds no leave)
            "leave_transaction_id": None, "leave_type_id": None, "leave_type_name": None,
            "leave_from": None, "leave_to": None, "pending_leave_since": None}]

    def test_candidates_defaults_to_today_and_rule_defaults(self):
        resp = client.get(f"{PREFIX}/candidates", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200 and resp.json()["data"] == []
        p = self.db.params(Q_CANDIDATES)[0]
        # default lookback_days = 0 -> no look-back: from_date binds NULL (amendment 11.18)
        assert RULE_DEFAULTS["lookback_days"] == 0
        assert (p["as_on"], p["from_date"], p["detect_days"], p["exclude_regex"], p["dept_id"]) == (
            TODAY, None, 10, "^[TCRL]", None)
        assert "exclude_t" not in p

    def test_candidates_lookback_zero_binds_null_else_the_window(self):
        self.db.put(JuteProdAbsentRule(co_id=1, detect_days=12, lookback_days=0))
        client.get(f"{PREFIX}/candidates", params={"co_id": 1, "branch_id": 2, "as_on": "2026-10-11"})
        self.db.put(JuteProdAbsentRule(co_id=1, detect_days=12, lookback_days=30))
        client.get(f"{PREFIX}/candidates", params={"co_id": 1, "branch_id": 2, "as_on": "2026-10-11"})
        assert [p["from_date"] for p in self.db.params(Q_CANDIDATES)] == [None, date(2026, 9, 11)]

    def test_candidates_query_lookback_is_optional_on_both_legs(self):
        sql = " ".join(str(la.get_candidates_query()).split())
        assert "AND da.attendance_date <= :as_on AND (:from_date IS NULL OR da.attendance_date >= :from_date)" in sql
        assert "AND ltd.leave_date <= :as_on AND (:from_date IS NULL OR ltd.leave_date >= :from_date)" in sql
        assert "BETWEEN :from_date" not in sql

    def test_candidates_query_binds_the_prefix_regex(self):
        sql = str(la.get_candidates_query())
        assert "NOT REGEXP :exclude_regex" in sql and ":exclude_regex IS NULL" in sql
        assert ":exclude_t " not in sql and "NOT LIKE 'T%'" not in sql

    def test_list_validation(self):
        for params in ({"view": "late"}, {"stage_code": "FIRED"}, {"q": "x" * 101}, {"dept_id": "x"}):
            resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, **params})
            assert resp.status_code == 400, params
        assert not self.db.calls

    def test_list_refreshes_rejoins_and_filters_due(self):
        self.db.on(Q_REJOIN, [
            {"case_id": 1, "rejoined_seen_date": None, "first_seen": date(2026, 10, 6)},           # rejoined
            {"case_id": 2, "rejoined_seen_date": date(2026, 10, 2), "first_seen": date(2026, 10, 2)},  # same
            {"case_id": 3, "rejoined_seen_date": date(2026, 10, 2), "first_seen": None},           # cleared
        ])
        self.db.on(Q_LIST, [
            case_row(case_id=1, rejoined_seen_date=date(2026, 10, 6), stage_code="WARNING_1",
                     w1_date=date(2026, 10, 2), w1_sent=date(2026, 10, 3)),
            case_row(case_id=4, stage_code="WARNING_1", next_due_date=date(2026, 10, 20)),   # NONE
            case_row(case_id=5, stage_code="VERIFIED", next_due_date=date(2026, 10, 9)),     # LETTER_DUE
        ])
        resp = client.get(f"{PREFIX}/list", params={
            "co_id": 1, "branch_id": 2, "view": "due", "q": " 100 ", "stage_code": "warning_1"})
        assert resp.status_code == 200, resp.text
        assert self.db.params(Q_REJOIN) == [{"co_id": 1, "branch_id": 2, "case_id": None}]
        updates = sorted(self.db.params(Q_SET_REJOIN), key=lambda p: p["case_id"])
        assert updates == [{"case_id": 1, "rejoined_seen_date": date(2026, 10, 6)},
                           {"case_id": 3, "rejoined_seen_date": None}]
        self.db.commit.assert_called_once()
        assert self.db.params(Q_LIST) == [{"co_id": 1, "branch_id": 2, "status_mode": "live", "dept_id": None,
                                           "stage_code": "WARNING_1", "q": "%100%", "today": TODAY}]
        rows = resp.json()["data"]
        assert [(r["case_id"], r["due_band"]) for r in rows] == [(1, "MARKED_PRESENT"), (5, "LETTER_DUE")]
        assert rows[0]["w1_date"] == "2026-10-02" and rows[0]["w1_sent"] == "2026-10-03"
        assert rows[0]["gn_date"] is None and rows[0]["absent_days"] == 16
        assert rows[1]["next_stage_code"] == "WARNING_1" and rows[1]["next_stage_label"] == "1st warning"

    def test_list_no_rejoin_change_no_commit_and_view_modes(self):
        self.db.on(Q_REJOIN, [{"case_id": 1, "rejoined_seen_date": date(2026, 10, 2), "first_seen": None}])
        self.db.on(Q_LIST, [case_row(case_id=4, status=0, exit_reason="RESIGNED")])
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "view": "closed"})
        assert resp.status_code == 200
        assert not self.db.ran(Q_REJOIN)                      # closed view: no rejoin refresh at all
        assert not self.db.ran(Q_SET_REJOIN)
        self.db.commit.assert_not_called()
        assert self.db.params(Q_LIST)[0]["status_mode"] == "closed"
        row = resp.json()["data"][0]
        assert row["due_band"] == "NONE" and row["exit_reason_label"] == "Resigned"
        self.db.on(Q_REJOIN, [{"case_id": 1, "rejoined_seen_date": None, "first_seen": None}])
        for view, mode in (("open", "open"), ("parked", "parked"), ("all", "all")):
            client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "view": view})
            assert self.db.params(Q_LIST)[-1]["status_mode"] == mode
        assert len(self.db.params(Q_REJOIN)) == 3 and not self.db.ran(Q_SET_REJOIN)
        self.db.commit.assert_not_called()

    def test_rejoin_query_is_index_friendly(self):
        """idx_da_dedupe (eb_id, attendance_date, spell_id, is_active): the rejoin /
        last-seen / last-attendance filters compare the bare columns."""
        rejoin = str(la.get_rejoin_query())
        assert "da.eb_id = c.eb_id" in rejoin and "da.attendance_date > c.last_working_date" in rejoin
        for q in (la.get_last_seen_query(), la.get_last_attendance_query()):
            sql = str(q)
            assert "da.eb_id = :eb_id" in sql and "da.attendance_date <= :as_on" in sql

    def test_get_case_page(self):
        c = self.db.put(make_case(stage_code="GATE_NOTICE", next_due_date=date(2026, 10, 20)))
        self.db.on(Q_REJOIN, [{"case_id": 1, "rejoined_seen_date": None, "first_seen": date(2026, 10, 9)}])
        self.db.on(Q_LETTERS, [letter_row(), letter_row(letter_id=12, stage_code="GATE_NOTICE", letter_no=None,
                                                         template_code="GATE_NOTICE", sent_date=date(2026, 10, 3),
                                                         dispatch_mode="GATE_BOARD")])
        self.db.on(Q_RECEIPTS, [{"receipt_id": 1, "letter_id": 12, "union_name": "INTUC", "received_by": "Rao",
                                 "received_date": date(2026, 10, 3), "remarks": None, "active": 1,
                                 "updated_by": 7, "updated_date_time": datetime(2026, 10, 3, 10, 0)}])
        self.db.on(Q_LETTER_ATT, [
            {"module": "jute_prod_absent_letter", "entity_id": 11, "file_type": "signed_copy",
             "attachment_id": 77, "filename": "w1.pdf"},
            {"module": "jute_prod_absent_receipt", "entity_id": 1, "file_type": "union_receipt",
             "attachment_id": 78, "filename": "intuc.jpg"},
            {"module": "jute_prod_absent_receipt", "entity_id": 11, "file_type": "union_receipt",
             "attachment_id": 79, "filename": "not-a-letter-slot.jpg"},
        ])
        self.db.on(Q_EVENTS, [event_row(event_id=9, event_type="REPLY", channel="PHONE",
                                        attachment_id=5, attachment_filename="m.jpg")])
        self.db.on(Q_OTHERS, [{"case_id": 0, "case_no": None, "detected_date": date(2026, 3, 1),
                               "stage_code": "DETECTED", "status": 0, "exit_reason": "DISMISSED",
                               "exit_date": date(2026, 3, 1), "struck_off_date": None}])
        resp = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert c.rejoined_seen_date == date(2026, 10, 9)
        self.db.commit.assert_called_once()
        assert self.db.params(Q_REJOIN) == [{"co_id": 1, "branch_id": 2, "case_id": 1}]
        assert d["case"]["due_band"] == "MARKED_PRESENT" and d["case"]["rejoined_seen_date"] == "2026-10-09"
        assert d["case"]["next_stage_code"] == "STRUCK_OFF"
        w1, gn = d["letters"]
        assert w1["attachments"] == {"signed_copy": {"attachment_id": 77, "filename": "w1.pdf"}, "ad_card": None}
        assert w1["receipts"] == [] and w1["stage_label"] == "1st warning"
        assert gn["receipts"][0]["union_name"] == "INTUC" and gn["receipts"][0]["received_date"] == "2026-10-03"
        assert gn["receipts"][0]["attachment"] == {"attachment_id": 78, "filename": "intuc.jpg"}
        assert gn["receipts"][0]["updated_date_time"] == "2026-10-03T10:00:00"
        assert gn["attachments"] == {"signed_copy": None, "ad_card": None}
        assert gn["dispatch_mode_label"] == "Gate board"
        assert self.db.params(Q_LETTER_ATT) == [{"case_id": 1, "co_id": 1, "letter_module": "jute_prod_absent_letter",
                                                 "receipt_module": "jute_prod_absent_receipt"}]
        assert d["events"][0]["event_type_label"] == "Reply / contact"
        assert d["events"][0]["channel_label"] == "Phone" and d["events"][0]["attachment_id"] == 5
        assert self.db.params(Q_EVENTS) == [{"case_id": 1, "co_id": 1, "module": "jute_prod_absent_event",
                                             "event_id": None}]
        assert d["earlier_cases"][0]["exit_reason_label"] == "Dismissed (not a case)"
        assert self.db.params(Q_OTHERS) == [{"co_id": 1, "branch_id": 2, "eb_id": 3, "case_id": 1}]

    def test_get_closed_case_skips_rejoin(self):
        self.db.put(make_case(status=0, exit_reason="RESIGNED"))
        resp = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200 and resp.json()["data"]["letters"] == []
        assert not self.db.ran(Q_REJOIN) and not self.db.ran(Q_CASE_LEAVE)
        self.db.commit.assert_not_called()

    @pytest.mark.parametrize("path", ["get", "events", "performance"])
    def test_case_of_other_company_404(self, path):
        self.db.put(make_case(co_id=9))
        resp = client.get(f"{PREFIX}/{path}", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 404
        self.access.assert_not_called()

    def test_inactive_case_404(self):
        self.db.put(make_case(active=0))
        assert client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1}).status_code == 404
        assert client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 99}).status_code == 404


# =============================================================================
# Open / dismiss
# =============================================================================


class TestOpenDismiss(_Base):
    def _open_db(self, live=None, last_seen=date(2026, 9, 20), count=3, snapshot=SNAPSHOT):
        self.db.on(Q_SNAPSHOT, [snapshot] if snapshot else [])
        self.db.on(Q_LIVE, [live] if live else [])
        self.db.on(Q_LAST_ATT, [{"daily_atten_id": 1, "attendance_date": date(2026, 9, 20),
                                 "spell_code": "A1", "machine_name": "LOOM 12"}])
        self.db.on(Q_LAST_SEEN, [{"last_seen": last_seen}])
        self.db.on(Q_COUNT, [{"n": count}])

    def test_validation_400_before_db(self):
        for body in ({"co_id": 1, "branch_id": 2}, {"co_id": 1, "branch_id": 2, "eb_id": "x"},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "last_working_date": "2026-10-12"},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "last_working_date": "20/09/2026"},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "remarks": "x" * 1001}):
            assert client.post(f"{PREFIX}/open", json=body).status_code == 400, body
        assert not self.db.calls

    def test_open_happy_path(self):
        self._open_db()
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3, "remarks": " r "})
        assert resp.status_code == 200, resp.text
        (c,) = self.db.added_of(JuteProdAbsentCase)
        # the number is the row's own case_id (the fake assigns 500 at flush)
        assert (c.co_id, c.branch_id, c.eb_id, c.case_id, c.case_no) == (1, 2, 3, 500, "LA/2/26/0500")
        assert (c.emp_code, c.emp_name, c.dept_id, c.dept_desc, c.designation) == (
            "10000", "Ram Das", 5, "WEAVING", "Weaver")
        assert c.address == "12 Mill Road, Howrah, West Bengal, 711101"
        assert (c.father_name, c.mobile, c.esi_no, c.pf_no, c.date_of_join) == (
            "Shyam Das", "9800000000", "4005", "3457", date(2010, 4, 1))
        assert (c.last_machine, c.last_spell) == ("LOOM 12", "A1")
        assert (c.last_working_date, c.detected_date, c.absent_days_at_open) == (date(2026, 9, 20), TODAY, 21)
        # owner 12.10.2026: no manual verification — the case opens at VERIFIED, 1st warning due today
        assert (c.stage_code, c.stage_date, c.next_due_date, c.status) == ("VERIFIED", TODAY, TODAY, 1)
        assert (c.verified_date, c.verify_note) == (TODAY, "Leave module checked automatically")
        assert (c.esi_stl_result, c.timekeeper_confirmed, c.habitual, c.leave_parked) == (None, 0, 0, 0)
        assert c.remarks == "r" and c.updated_by == 7
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.case_id, ev.event_type, ev.event_date) == (c.case_id, "SYSTEM", TODAY)
        assert ev.text == "Case opened. Last working date 20.09.2026, 21 days absent. 1st warning due now."
        self.db.commit.assert_called_once()                   # no leave found -> the refresh commits nothing
        assert self.db.params(Q_CASE_LEAVE) == [{"co_id": 1, "branch_id": 2, "case_id": 500, "today": TODAY}]
        assert not self.db.ran(Q_COUNT)                       # no count-based sequence any more
        assert self.db.params(Q_SNAPSHOT) == [{"eb_id": 3, "branch_id": 2}]
        assert self.db.params(Q_LIVE) == [{"co_id": 1, "branch_id": 2, "eb_id": 3, "exclude_id": None}]
        assert self.db.params(Q_LAST_SEEN) == [{"eb_id": 3, "branch_id": 2, "as_on": TODAY}]
        d = resp.json()["data"]
        assert d["case_no"] == "LA/2/26/0500" and d["due_band"] == "LETTER_DUE" and d["absent_days"] == 21
        assert d["stage_code"] == "VERIFIED" and d["next_stage_code"] == "WARNING_1"

    def test_worker_lookup_needs_the_latest_official_row_in_the_branch(self):
        """A worker transferred out keeps an older active official row of this
        branch: /open and /dismiss must use the latest active row overall."""
        sql = str(la.get_worker_snapshot_query())
        assert "od.branch_id = :branch_id" in sql
        assert "od.tbl_hrms_ed_official_detail_id = (" in sql
        assert "SELECT MAX(o2.tbl_hrms_ed_official_detail_id)" in sql
        assert "WHERE o2.eb_id = od.eb_id AND o2.active = 1" in sql

    def test_open_with_given_last_working_date_skips_last_seen(self):
        self._open_db()
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3,
                                                   "last_working_date": "2026-09-25"})
        assert resp.status_code == 200, resp.text
        assert not self.db.ran(Q_LAST_SEEN)
        assert self.db.added_of(JuteProdAbsentCase)[0].absent_days_at_open == 16

    def test_open_back_dated_detection(self):
        """Owner 2026-10-12 (back-dated entries): a case entered late keeps the day the
        absence was picked up — detected / stage / verified date and absent days at open."""
        self._open_db()
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3,
                                                   "detected_date": "2026-09-30"})
        assert resp.status_code == 200, resp.text
        (c,) = self.db.added_of(JuteProdAbsentCase)
        detected = date(2026, 9, 30)
        assert (c.detected_date, c.stage_date, c.verified_date, c.next_due_date) == (detected,) * 4
        assert c.absent_days_at_open == 10 and c.last_working_date == date(2026, 9, 20)
        assert c.case_no == "LA/2/26/0500"
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_date == detected
        assert ev.text == ("Case opened. Last working date 20.09.2026, 10 days absent when detected on 30.09.2026 "
                           "(back-dated entry, entered 11.10.2026). 1st warning due now.")
        # the last-seen fallback still runs as on today; the live absent days run to today
        assert self.db.params(Q_LAST_SEEN) == [{"eb_id": 3, "branch_id": 2, "as_on": TODAY}]
        d = resp.json()["data"]
        assert d["detected_date"] == "2026-09-30" and d["due_band"] == "LETTER_DUE" and d["absent_days"] == 21

    def test_open_detected_today_is_the_default(self):
        self._open_db()
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3,
                                                   "detected_date": TODAY.isoformat()})
        assert resp.status_code == 200, resp.text
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert "back-dated" not in ev.text and self.db.added_of(JuteProdAbsentCase)[0].detected_date == TODAY

    def test_open_detected_date_rules(self):
        g = {"co_id": 1, "branch_id": 2, "eb_id": 3}
        for body, msg in (
            ({**g, "detected_date": "2026-10-12"}, "detected_date cannot be in the future"),
            ({**g, "detected_date": "30/09/2026"}, "detected_date must be a date (YYYY-MM-DD)"),
            ({**g, "last_working_date": "2026-09-25", "detected_date": "2026-09-24"},
             "detected_date cannot be before the last working date (25.09.2026)"),
        ):
            resp = client.post(f"{PREFIX}/open", json=body)
            assert resp.status_code == 400 and resp.json()["detail"] == msg, (body, resp.text)
        assert not self.db.calls
        # against the last day seen in attendance (20.09.2026)
        self._open_db()
        resp = client.post(f"{PREFIX}/open", json={**g, "detected_date": "2026-09-19"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "detected_date cannot be before the last working date (20.09.2026)"
        assert not self.db.added
        self.db.commit.assert_not_called()

    def test_open_never_seen_400(self):
        self._open_db(last_seen=None)
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 400 and "last working date" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_open_worker_not_in_branch_400(self):
        self._open_db(snapshot=None)
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 400 and resp.json()["detail"] == "Worker not found in this branch"
        assert not self.db.added

    def test_open_separated_worker_400(self):
        self._open_db(snapshot={**SNAPSHOT, "emp_status": 41})
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 400 and "left the roll" in resp.json()["detail"]

    def test_open_duplicate_live_case_409_select(self):
        self._open_db(live={"case_id": 42, "case_no": "LA/2/26/0002"})
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 409
        assert "(LA/2/26/0002)" in resp.json()["detail"]
        assert not self.db.added
        self.db.commit.assert_not_called()

    def test_open_duplicate_live_case_409_integrity_error(self):
        """The SELECT guard passes (a concurrent request opened first): the unique
        key refuses the INSERT at flush -> 409 with the winner's number."""
        self._open_db()
        calls = {"n": 0}

        def live(_params):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [{"case_id": 43, "case_no": "LA/2/26/0005"}]

        self.db.on(Q_LIVE, live)
        self.db.flush.side_effect = _integrity()
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 409, resp.text
        assert "(LA/2/26/0005)" in resp.json()["detail"]
        self.db.rollback.assert_called()
        self.db.commit.assert_not_called()

    def test_open_other_integrity_error_is_500(self):
        self._open_db()
        self.db.commit.side_effect = _integrity(code=1452, key="fk_x")
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 500
        self.db.rollback.assert_called()

    def test_branch_of_another_company_400_before_access(self):
        self.db.on(Q_BRANCH, [])
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 400 and "does not belong" in resp.json()["detail"]
        self.access.assert_not_called()

    def test_dismiss_validation(self):
        for body in ({"co_id": 1, "branch_id": 2, "eb_id": 3},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "reason": " "},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "reason": "x", "snooze_days": 400},
                     {"co_id": 1, "branch_id": 2, "eb_id": 3, "reason": "x", "snooze_days": -1}):
            assert client.post(f"{PREFIX}/dismiss", json=body).status_code == 400, body
        assert not self.db.calls

    def test_dismiss_creates_a_closed_row(self):
        self._open_db()
        self.db.put(JuteProdAbsentRule(co_id=1, dismiss_snooze_days=45))
        resp = client.post(f"{PREFIX}/dismiss", json={"co_id": 1, "branch_id": 2, "eb_id": 3,
                                                      "reason": "On company leave"})
        assert resp.status_code == 200, resp.text
        (c,) = self.db.added_of(JuteProdAbsentCase)
        assert (c.status, c.stage_code, c.exit_reason, c.exit_date, c.exit_remarks) == (
            0, "DETECTED", "DISMISSED", TODAY, "On company leave")
        assert c.snooze_until == date(2026, 11, 25) and c.case_no is None and c.next_due_date is None
        assert (c.emp_code, c.emp_name, c.dept_desc, c.designation) == ("10000", "Ram Das", "WEAVING", "Weaver")
        assert c.address is None and c.esi_no is None          # minimal snapshot
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "DISMISS" and "until 25.11.2026" in ev.text
        assert not self.db.ran(Q_COUNT)
        assert resp.json()["data"]["status_label"] == "Closed"

    def test_dismiss_explicit_snooze_and_duplicate(self):
        self._open_db()
        resp = client.post(f"{PREFIX}/dismiss", json={"co_id": 1, "branch_id": 2, "eb_id": 3,
                                                      "reason": "x", "snooze_days": 0})
        assert resp.status_code == 200
        assert self.db.added_of(JuteProdAbsentCase)[0].snooze_until == TODAY
        self.setup_method_fresh()
        self._open_db(live={"case_id": 42, "case_no": None})
        resp = client.post(f"{PREFIX}/dismiss", json={"co_id": 1, "branch_id": 2, "eb_id": 3, "reason": "x"})
        assert resp.status_code == 409 and "(#42)" in resp.json()["detail"]

    def setup_method_fresh(self):
        self.db = FakeDB()
        app.dependency_overrides[get_tenant_db] = lambda: self.db


# =============================================================================
# Verify / update
# =============================================================================


class TestVerifyUpdate(_Base):
    def test_verify_moves_detected_to_verified(self):
        c = self.db.put(make_case())
        resp = client.post(f"{PREFIX}/verify", json={
            "co_id": 1, "case_id": 1, "esi_stl_result": "none", "timekeeper_confirmed": True,
            "habitual": True, "note": "Confirmed by TK"})
        assert resp.status_code == 200, resp.text
        assert (c.stage_code, c.stage_date, c.next_due_date, c.verified_date) == ("VERIFIED", TODAY, TODAY, TODAY)
        assert (c.esi_stl_result, c.timekeeper_confirmed, c.habitual, c.verify_note) == ("NONE", 1, 1, "Confirmed by TK")
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "VERIFY"
        assert ev.text == ("Not on ESI / STL; time keeper confirmed: yes; habitual: yes. "
                           "Verified — 1st warning due now. Confirmed by TK")
        d = resp.json()["data"]
        assert d["due_band"] == "LETTER_DUE" and d["next_stage_code"] == "WARNING_1"

    def test_verify_esi_or_unconfirmed_stays_detected(self):
        c = self.db.put(make_case())
        resp = client.post(f"{PREFIX}/verify", json={
            "co_id": 1, "case_id": 1, "esi_stl_result": "ESI", "timekeeper_confirmed": True})
        assert resp.status_code == 200
        assert c.stage_code == "DETECTED" and c.esi_stl_result == "ESI" and c.habitual == 0
        c2 = self.db.put(make_case(case_id=2))
        client.post(f"{PREFIX}/verify", json={"co_id": 1, "case_id": 2, "esi_stl_result": "NONE"})
        assert c2.stage_code == "DETECTED" and c2.timekeeper_confirmed == 0

    def test_verify_at_a_later_stage_never_moves_back(self):
        c = self.db.put(make_case(stage_code="WARNING_2", stage_date=date(2026, 10, 5),
                                  next_due_date=date(2026, 10, 15)))
        resp = client.post(f"{PREFIX}/verify", json={
            "co_id": 1, "case_id": 1, "esi_stl_result": "NONE", "timekeeper_confirmed": True})
        assert resp.status_code == 200
        assert (c.stage_code, c.stage_date, c.next_due_date) == ("WARNING_2", date(2026, 10, 5), date(2026, 10, 15))
        assert c.verified_date == TODAY

    def test_verify_validation_and_state(self):
        self.db.put(make_case(status=2))
        assert client.post(f"{PREFIX}/verify", json={
            "co_id": 1, "case_id": 1, "esi_stl_result": "MAYBE"}).status_code == 400
        resp = client.post(f"{PREFIX}/verify", json={"co_id": 1, "case_id": 1, "esi_stl_result": "NONE"})
        assert resp.status_code == 400 and "parked" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_update_nothing_to_update_400(self):
        resp = client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "foo": "x"})
        assert resp.status_code == 400 and "Nothing to update" in resp.json()["detail"]
        assert client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "extra_1": "x" * 201}).status_code == 400
        assert client.post(f"{PREFIX}/update", json={
            "co_id": 1, "case_id": 1, "last_working_date": None}).status_code == 400

    def test_update_only_sent_keys_change_closed_case_too(self):
        c = self.db.put(make_case(status=0, exit_reason="RESIGNED", remarks="old", extra_1="keep"))
        self.access.return_value = LEVEL_CREATE
        resp = client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "remarks": " new ",
                                                     "extra_2": "", "habitual": "yes"})
        assert resp.status_code == 200, resp.text
        assert (c.remarks, c.extra_1, c.extra_2, c.habitual) == ("new", "keep", None, 1)
        assert not self.db.added_of(JuteProdAbsentEvent)        # no event for remarks
        self.db.commit.assert_called_once()

    def test_update_last_working_date_needs_edit(self):
        c = self.db.put(make_case())
        self.access.return_value = LEVEL_CREATE
        resp = client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "remarks": "x",
                                                     "last_working_date": "2026-09-18"})
        assert resp.status_code == 403
        assert c.remarks is None and c.last_working_date == date(2026, 9, 20)

    def test_update_last_working_date_recomputes(self):
        c = self.db.put(make_case(rejoined_seen_date=date(2026, 9, 25)))
        resp = client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "last_working_date": "2026-09-26"})
        assert resp.status_code == 200, resp.text
        assert c.last_working_date == date(2026, 9, 26) and c.absent_days_at_open == 5
        assert c.rejoined_seen_date is None
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "SYSTEM" and ev.text == "Last working date changed from 20.09.2026 to 26.09.2026"

    def test_update_last_working_date_after_opening_400(self):
        self.db.put(make_case())
        resp = client.post(f"{PREFIX}/update", json={"co_id": 1, "case_id": 1, "last_working_date": "2026-10-02"})
        assert resp.status_code == 400 and "01.10.2026" in resp.json()["detail"]
        self.db.commit.assert_not_called()


# =============================================================================
# Park / resume / exit / reopen
# =============================================================================


class TestLifecycle(_Base):
    def test_park(self):
        c = self.db.put(make_case(stage_code="WARNING_1"))
        assert client.post(f"{PREFIX}/park", json={"co_id": 1, "case_id": 1}).status_code == 400
        resp = client.post(f"{PREFIX}/park", json={"co_id": 1, "case_id": 1, "note": "Came with union rep"})
        assert resp.status_code == 200, resp.text
        assert c.status == 2 and resp.json()["data"]["due_band"] == "REPLY_REVIEW"
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.event_type, ev.text) == ("PARK", "Came with union rep")

    def test_park_blocked_by_pending_approval_or_state(self):
        self.db.put(make_case(stage_code="GATE_NOTICE", status_id=20))
        resp = client.post(f"{PREFIX}/park", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 400 and "reject it first" in resp.json()["detail"]
        self.db.put(make_case(status=2))
        assert client.post(f"{PREFIX}/park", json={"co_id": 1, "case_id": 1, "note": "x"}).status_code == 400
        self.db.commit.assert_not_called()

    def test_resume(self):
        c = self.db.put(make_case(status=2, stage_code="WARNING_1", next_due_date=date(2026, 10, 1)))
        resp = client.post(f"{PREFIX}/resume", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert c.status == 1 and c.next_due_date == TODAY
        assert self.db.added_of(JuteProdAbsentEvent)[0].text == "Resumed"
        later = self.db.put(make_case(case_id=2, status=2, next_due_date=date(2026, 10, 30)))
        client.post(f"{PREFIX}/resume", json={"co_id": 1, "case_id": 2, "note": "not genuine"})
        assert later.next_due_date == date(2026, 10, 30)
        self.db.put(make_case(case_id=3, status=1))
        resp = client.post(f"{PREFIX}/resume", json={"co_id": 1, "case_id": 3})
        assert resp.status_code == 400 and resp.json()["detail"] == "This case is not parked"

    def test_exit_validation(self):
        base = {"co_id": 1, "case_id": 1, "exit_date": "2026-10-05"}
        for body in ({**base, "exit_reason": "STRUCK_OFF"}, {**base, "exit_reason": "DISMISSED"},
                     {**base}, {**base, "exit_reason": "ESI", "exit_date": "2026-10-12"},
                     {**base, "exit_reason": "ESI", "expected_return_date": "2026-10-04"},
                     {"co_id": 1, "case_id": 1, "exit_reason": "ESI"}):
            assert client.post(f"{PREFIX}/exit", json=body).status_code == 400, body
        assert not self.db.calls

    def test_exit_happy_path(self):
        c = self.db.put(make_case(stage_code="WARNING_2"))
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 1, "exit_reason": "esi", "exit_date": "2026-10-05",
            "exit_remarks": "ESI certificate", "expected_return_date": "2026-11-05"})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.stage_code, c.exit_reason, c.exit_date, c.expected_return_date) == (
            0, "WARNING_2", "ESI", date(2026, 10, 5), date(2026, 11, 5))
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "EXIT" and ev.text == "On ESI: ESI certificate (expected back 05.11.2026)"

    def test_exit_blocked(self):
        self.db.put(make_case(status_id=20, stage_code="GATE_NOTICE"))
        body = {"co_id": 1, "case_id": 1, "exit_reason": "RESIGNED", "exit_date": "2026-10-05"}
        resp = client.post(f"{PREFIX}/exit", json=body)
        assert resp.status_code == 400 and "reject it first" in resp.json()["detail"]
        self.db.put(make_case(status=0, exit_reason="RESIGNED"))
        resp = client.post(f"{PREFIX}/exit", json=body)
        assert resp.status_code == 400 and "closed" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_reopen_happy_path(self):
        c = self.db.put(make_case(status=0, stage_code="WARNING_1", exit_reason="ESI", exit_date=date(2026, 10, 5),
                                  exit_remarks="cert", expected_return_date=date(2026, 10, 9),
                                  next_due_date=date(2026, 10, 3)))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "Did not return"})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.exit_reason, c.exit_date, c.exit_remarks, c.expected_return_date) == (
            1, None, None, None, None)
        assert c.next_due_date == TODAY and c.case_no == "LA/2/26/0001"
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "REOPEN"
        assert ev.text == "Reopened. It was closed on 05.10.2026 (On ESI): cert. Did not return"
        assert self.db.params(Q_LIVE) == [{"co_id": 1, "branch_id": 2, "eb_id": 3, "exclude_id": 1}]
        self.db.commit.assert_called_once()

    def test_reopen_dismissed_candidate_becomes_a_full_case(self):
        """A dismissal keeps only the minimal snapshot: reopening it re-runs the
        full one (address, ESI / PF, last machine / spell ...) and numbers it
        from its own case_id."""
        c = self.db.put(make_case(case_id=37, status=0, case_no=None, exit_reason="DISMISSED",
                                  snooze_until=date(2026, 11, 1), next_due_date=None,
                                  detected_date=date(2026, 10, 2), address=None, esi_no=None,
                                  leave_parked=1))
        self.db.on(Q_SNAPSHOT, [SNAPSHOT])
        self.db.on(Q_LAST_ATT, [{"daily_atten_id": 1, "attendance_date": date(2026, 9, 20),
                                 "spell_code": "B2", "machine_name": "LOOM 7"}])
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 37, "note": "Open it"})
        assert resp.status_code == 200, resp.text
        assert c.case_no == "LA/2/26/0037" and c.snooze_until is None and c.next_due_date == TODAY
        assert c.address == "12 Mill Road, Howrah, West Bengal, 711101"
        assert (c.father_name, c.mobile, c.esi_no, c.pf_no, c.category) == (
            "Shyam Das", "9800000000", "4005", "3457", "PERMANENT")
        assert (c.last_machine, c.last_spell, c.date_of_join) == ("LOOM 7", "B2", date(2010, 4, 1))
        assert (c.status, c.exit_reason, c.leave_parked) == (1, None, 0)
        # the manual verification step is retired: the reopened dismissal goes straight to VERIFIED
        assert (c.stage_code, c.stage_date, c.verified_date, c.verify_note) == (
            "VERIFIED", TODAY, TODAY, "Leave module checked automatically")
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "REOPEN" and ev.text.startswith("Reopened. It was closed on ")
        assert ev.text.endswith(". Open it. 1st warning due now.")
        assert self.db.params(Q_SNAPSHOT) == [{"eb_id": 3, "branch_id": 2}]
        assert not self.db.ran(Q_COUNT)
        d = resp.json()["data"]
        assert d["case_no"] == "LA/2/26/0037" and d["due_band"] == "LETTER_DUE" and d["leave_parked"] == 0

    def test_reopen_dismissed_worker_who_left_the_branch_400(self):
        c = self.db.put(make_case(status=0, case_no=None, exit_reason="DISMISSED", next_due_date=None))
        self.db.on(Q_SNAPSHOT, [])
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 400 and resp.json()["detail"] == "Worker not found in this branch"
        assert c.status == 0 and c.case_no is None
        self.db.commit.assert_not_called()

    def test_reopen_of_an_ordinary_exit_keeps_the_snapshot(self):
        self.db.put(make_case(status=0, exit_reason="ESI", exit_date=date(2026, 10, 5)))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 200, resp.text
        assert not self.db.ran(Q_SNAPSHOT) and not self.db.ran(Q_LAST_ATT)

    @pytest.mark.parametrize("path,arrange,body", [
        ("park", {}, {"note": "x"}),
        ("resume", {"status": 2}, {}),
        ("exit", {}, {"exit_reason": "RESIGNED", "exit_date": "2026-10-05"}),
        ("reopen", {"status": 0, "exit_reason": "RESIGNED"}, {"note": "x"}),
    ])
    def test_lifecycle_locks_the_case_first(self, path, arrange, body):
        self.db.put(make_case(case_id=4, **arrange))
        resp = client.post(f"{PREFIX}/{path}", json={"co_id": 1, "case_id": 4, **body})
        assert resp.status_code == 200, resp.text
        first_sql, first_params = self.db.calls[0]
        assert "FOR UPDATE" in first_sql and "FROM jute_prod_absent_case" in first_sql
        assert first_params == {"case_id": 4}

    def test_reopen_refusals(self):
        assert client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1}).status_code == 400   # note
        self.db.put(make_case(status=1))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 400 and resp.json()["detail"] == "This case is already open"
        self.db.put(make_case(status=0, exit_reason="STRUCK_OFF", stage_code="STRUCK_OFF"))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 400 and "HRMS" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_reopen_duplicate_409_select(self):
        self.db.put(make_case(status=0, exit_reason="REJOINED"))
        self.db.on(Q_LIVE, [{"case_id": 8, "case_no": "LA/2/26/0008"}])
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 409 and "(LA/2/26/0008)" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_reopen_duplicate_409_integrity_error(self):
        self.db.put(make_case(status=0, exit_reason="REJOINED"))
        calls = {"n": 0}

        def live(_params):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [{"case_id": 8, "case_no": "LA/2/26/0008"}]

        self.db.on(Q_LIVE, live)
        self.db.commit.side_effect = _integrity()
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "x"})
        assert resp.status_code == 409, resp.text
        assert "(LA/2/26/0008)" in resp.json()["detail"]
        # both look-ups skip the case being reopened
        assert [p["exclude_id"] for p in self.db.params(Q_LIVE)] == [1, 1]
        self.db.rollback.assert_called()


# =============================================================================
# Leave module link (TKT-000223): the refresh plan, the queries, the endpoints
# =============================================================================


PLAN_LEAVE = dict(CASE_STL)          # the lv of plan_leave_refresh: the approved leave of the module


def plan_state(**kw) -> dict:
    """plan_leave_refresh's case state: an open VERIFIED case that stores no leave link."""
    base = dict(status=1, status_id=None, stage_code="VERIFIED", next_due_date=TODAY, rejoined_seen_date=None,
                leave_parked=0, leave_transaction_id=None, leave_type_id=None, leave_type_name=None,
                leave_from=None, leave_to=None)
    base.update(kw)
    return base


def linked(**kw) -> dict:
    """plan_state of a case that already stores the PLAN_LEAVE columns."""
    return plan_state(**{**PLAN_LEAVE, **kw})


class TestPlanLeaveRefresh:
    """The rules of the automatic leave link, pure (owner ruling 12.10.2026: only
    APPROVED leave counts; the leave parks / resumes the case by itself)."""

    PLAN = staticmethod(lac.plan_leave_refresh)
    COLUMNS = set(lac.LEAVE_LINK_COLUMNS)

    # (a) a new approved leave covering today ----------------------------------

    def test_a_new_leave_covering_today_parks_an_open_case(self):
        upd, events = self.PLAN(plan_state(), PLAN_LEAVE, TODAY)
        assert upd == {**PLAN_LEAVE, "status": 2, "leave_parked": 1}
        assert set(upd) == self.COLUMNS | {"status", "leave_parked"}      # the five columns + the park, nothing else
        assert events == [PARKED_TEXT] == [lac.leave_parked_event("STL LEAVE", date(2026, 10, 5), date(2026, 10, 20))]
        # the last day and the first day still cover today
        for edge in ({"leave_from": TODAY}, {"leave_to": TODAY}):
            upd, events = self.PLAN(plan_state(), {**PLAN_LEAVE, **edge}, TODAY)
            assert upd["status"] == 2 and upd["leave_parked"] == 1 and len(events) == 1
        # a type whose name does not end in "leave" gets the word
        upd, events = self.PLAN(plan_state(), {**PLAN_LEAVE, "leave_type_name": "Sick"}, TODAY)
        assert events == ["On Sick leave 05.10.2026 to 20.10.2026 (leave module)"]
        # a rejected-and-reopened struck-off approval (status_id 4) does not stop it
        assert self.PLAN(plan_state(status_id=4), PLAN_LEAVE, TODAY)[0]["status"] == 2

    def test_a2_the_same_leave_never_parks_twice(self):
        """Once per leave transaction: the user resumed the case, the stored id is the same."""
        assert self.PLAN(linked(), PLAN_LEAVE, TODAY) == ({}, [])
        # the same transaction extended: the columns follow, but no new park and no event
        state = linked(leave_to=date(2026, 10, 15))
        upd, events = self.PLAN(state, PLAN_LEAVE, TODAY)
        assert upd == PLAN_LEAVE and events == []
        # a DIFFERENT transaction covering today (an extension filed as a new leave) is logged;
        # an open case is parked again for it
        upd, events = self.PLAN(linked(), {**PLAN_LEAVE, "leave_transaction_id": 902}, TODAY)
        assert upd["leave_transaction_id"] == 902 and upd["status"] == 2 and len(events) == 1

    def test_a3_a_rejoined_case_only_stores_the_columns(self):
        upd, events = self.PLAN(plan_state(rejoined_seen_date=date(2026, 10, 9)), PLAN_LEAVE, TODAY)
        assert upd == PLAN_LEAVE and events == []
        assert "status" not in upd and "leave_parked" not in upd
        # Marked present wins over a resume too
        ended = {**PLAN_LEAVE, "leave_to": date(2026, 10, 8)}
        parked = plan_state(status=2, leave_parked=1, rejoined_seen_date=date(2026, 10, 9), **ended)
        assert self.PLAN(parked, ended, TODAY) == ({}, [])
        assert self.PLAN(parked, None, TODAY) == ({k: None for k in self.COLUMNS}, [])

    def test_a4_a_pending_struck_off_approval_does_not_park(self):
        upd, events = self.PLAN(plan_state(status_id=20), PLAN_LEAVE, TODAY)
        assert upd == PLAN_LEAVE and events == []                          # columns stored, case untouched
        assert "status" not in upd and "leave_parked" not in upd

    def test_a5_a_case_parked_for_another_reason_is_not_taken_over(self):
        """Parked by a person (reply under review): the leave is logged but leave_parked stays 0,
        so the case does not resume by itself when the leave ends."""
        upd, events = self.PLAN(plan_state(status=2, leave_parked=0), PLAN_LEAVE, TODAY)
        assert upd == PLAN_LEAVE and events == [PARKED_TEXT]
        ended = {**PLAN_LEAVE, "leave_to": date(2026, 10, 8)}
        assert self.PLAN(plan_state(status=2, leave_parked=0, **ended), ended, TODAY) == ({}, [])

    def test_closed_cases_are_left_alone(self):
        assert self.PLAN(plan_state(status=0, stage_code="DETECTED"), PLAN_LEAVE, TODAY) == ({}, [])
        assert self.PLAN(plan_state(status=0, **PLAN_LEAVE), None, TODAY) == ({}, [])

    # (b) the leave has ended -----------------------------------------------------

    def test_b_an_ended_leave_resumes_a_leave_parked_case(self):
        ended = {**PLAN_LEAVE, "leave_to": date(2026, 10, 8)}
        state = plan_state(status=2, leave_parked=1, next_due_date=date(2026, 10, 3), **ended)
        upd, events = self.PLAN(state, ended, TODAY)
        assert upd == {"status": 1, "leave_parked": 0, "next_due_date": date(2026, 10, 9)}   # leave_to + 1
        assert events == ["Leave ended 08.10.2026, still absent"]
        # applied once, it does not run again (no double event)
        state.update(upd)
        assert self.PLAN(state, ended, TODAY) == ({}, [])

    def test_b2_next_due_date_is_the_later_of_the_old_one_and_the_day_after_the_leave(self):
        ended = {**PLAN_LEAVE, "leave_to": date(2026, 10, 8)}
        later = plan_state(status=2, leave_parked=1, next_due_date=date(2026, 10, 15), **ended)
        assert self.PLAN(later, ended, TODAY)[0]["next_due_date"] == date(2026, 10, 15)
        none = plan_state(status=2, leave_parked=1, next_due_date=None, **ended)
        assert self.PLAN(none, ended, TODAY)[0]["next_due_date"] == date(2026, 10, 9)
        # the day after the leave is also the earliest day when the case waited long before
        edge = plan_state(status=2, leave_parked=1, next_due_date=date(2026, 10, 9), **ended)
        assert self.PLAN(edge, ended, TODAY)[0]["next_due_date"] == date(2026, 10, 9)

    def test_b3_an_ended_leave_that_did_not_park_changes_nothing_but_the_columns(self):
        ended = {**PLAN_LEAVE, "leave_to": date(2026, 10, 8)}
        upd, events = self.PLAN(plan_state(), ended, TODAY)       # first seen already over
        assert upd == ended and events == []
        assert self.PLAN(plan_state(**ended), ended, TODAY) == ({}, [])    # open case, nothing to do

    # (c) a leave that has not started yet ----------------------------------------

    def test_c_a_future_leave_is_only_stored(self):
        fut = {**PLAN_LEAVE, "leave_transaction_id": 902, "leave_from": date(2026, 11, 1),
               "leave_to": date(2026, 11, 20)}
        upd, events = self.PLAN(plan_state(), fut, TODAY)
        assert upd == fut and events == []
        assert self.PLAN(plan_state(**fut), fut, TODAY) == ({}, [])
        # parked by a leave that is gone, only a later one is approved: resume like (d)
        state = plan_state(status=2, leave_parked=1, next_due_date=date(2026, 10, 3), **PLAN_LEAVE)
        upd, events = self.PLAN(state, fut, TODAY)
        assert upd == {**fut, "status": 1, "leave_parked": 0, "next_due_date": TODAY}
        assert events == ["Leave record withdrawn in the leave module; case resumed"]

    # (d) the leave record is gone ------------------------------------------------

    def test_d_no_leave_but_a_stored_id_clears_the_columns(self):
        cleared = {k: None for k in self.COLUMNS}
        for lv in (None, {}, {"leave_transaction_id": None, "leave_type_id": None}):
            assert self.PLAN(linked(), lv, TODAY) == (cleared, [])        # open case: just the columns
        assert self.PLAN(plan_state(), None, TODAY) == ({}, [])           # nothing stored, nothing found
        # parked by a person: the case stays parked
        assert self.PLAN(linked(status=2, leave_parked=0), None, TODAY) == (cleared, [])

    def test_d2_a_withdrawn_leave_resumes_the_case_it_parked(self):
        cleared = {k: None for k in self.COLUMNS}
        state = linked(status=2, leave_parked=1, next_due_date=date(2026, 10, 3))
        upd, events = self.PLAN(state, None, TODAY)
        assert upd == {**cleared, "status": 1, "leave_parked": 0, "next_due_date": TODAY}   # max(old, today)
        assert events == ["Leave record withdrawn in the leave module; case resumed"]
        state.update(upd)
        assert self.PLAN(state, None, TODAY) == ({}, [])                  # and it does not repeat
        later = linked(status=2, leave_parked=1, next_due_date=date(2026, 10, 15))
        assert self.PLAN(later, None, TODAY)[0]["next_due_date"] == date(2026, 10, 15)

    # (e) no manual verification --------------------------------------------------

    def test_e_a_detected_case_is_promoted_to_verified(self):
        upd, events = self.PLAN(plan_state(stage_code="DETECTED", next_due_date=date(2026, 10, 1)), None, TODAY)
        assert upd == {"stage_code": "VERIFIED", "stage_date": TODAY, "verified_date": TODAY,
                       "verify_note": lac.AUTO_VERIFY_NOTE, "next_due_date": TODAY}
        assert upd["verify_note"] == "Leave module checked automatically"
        assert events == [AUTO_VERIFY_TEXT]
        # a parked case keeps its next_due_date
        upd, events = self.PLAN(plan_state(stage_code="DETECTED", status=2, next_due_date=date(2026, 10, 15)),
                                None, TODAY)
        assert upd["stage_code"] == "VERIFIED" and "next_due_date" not in upd and events == [AUTO_VERIFY_TEXT]

    def test_e2_a_verified_case_is_not_promoted_again(self):
        for stage in ("VERIFIED", "WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE"):
            assert self.PLAN(plan_state(stage_code=stage), None, TODAY) == ({}, []), stage

    def test_e3_promotion_and_a_leave_in_one_pass(self):
        upd, events = self.PLAN(plan_state(stage_code="DETECTED"), PLAN_LEAVE, TODAY)
        assert upd["stage_code"] == "VERIFIED" and upd["status"] == 2 and upd["leave_parked"] == 1
        assert upd["next_due_date"] == TODAY and upd["leave_transaction_id"] == 901
        assert events == [AUTO_VERIFY_TEXT, PARKED_TEXT]                  # the promotion is logged first

    def test_inputs_are_coerced_like_a_database_row(self):
        lv = {"leave_transaction_id": "901", "leave_type_id": "24", "leave_type_name": " " + "x" * 120,
              "leave_from": "2026-10-05", "leave_to": datetime(2026, 10, 20, 0, 0)}
        upd, events = self.PLAN(plan_state(status_id="", next_due_date="2026-10-11"), lv, TODAY)
        assert (upd["leave_transaction_id"], upd["leave_type_id"]) == (901, 24)
        assert upd["leave_type_name"] == "x" * 100
        assert (upd["leave_from"], upd["leave_to"]) == (date(2026, 10, 5), date(2026, 10, 20))
        assert upd["status"] == 2 and len(events) == 1
        upd, _ = self.PLAN(plan_state(), {**PLAN_LEAVE, "leave_type_name": "  "}, TODAY)
        assert upd["leave_type_name"] is None


class TestLeaveQueries:
    """Text assertions on the SQL (the tests mock the database)."""

    def test_both_seen_unions_have_the_leave_header_leg(self):
        header = "LEAST(lt.leave_to_date, CAST(:as_on AS DATE)) AS seen_date"
        cand = norm(la.get_candidates_query())
        # the candidates legs are grouped per worker BEFORE the union (performance, 2026-10-12)
        assert (f"SELECT lt.eb_id, MAX({header[:-len(' AS seen_date')]}) AS seen_date FROM leave_transactions lt WHERE lt.status = 3 "
                "AND lt.branch_id = :branch_id AND lt.leave_from_date <= :as_on "
                "AND lt.leave_to_date IS NOT NULL AND (:from_date IS NULL OR lt.leave_to_date >= :from_date) "
                "GROUP BY lt.eb_id") in cand
        assert "SELECT da.eb_id, MAX(da.attendance_date) AS seen_date FROM daily_attendance da" in cand
        assert cand.count("GROUP BY") >= 4                         # three legs + the outer MAX
        seen = norm(la.get_last_seen_query())
        assert (f"SELECT {header} FROM leave_transactions lt WHERE lt.eb_id = :eb_id AND lt.status = 3 "
                "AND lt.branch_id = :branch_id AND lt.leave_from_date <= :as_on "
                "AND lt.leave_to_date IS NOT NULL") in seen
        assert cand.count(header[:-len(" AS seen_date")]) == 1 and seen.count(header) == 1
        # the per-day leg is still there, and the union stays a UNION ALL of three legs
        assert cand.count("UNION ALL") == 2 and seen.count("UNION ALL") == 2
        assert laq._LEAVE_HEADER_SEEN in cand and laq._LEAVE_HEADER_SEEN in seen

    def test_candidates_hide_workers_with_a_running_leave_exit(self):
        sql = norm(la.get_candidates_query())
        assert "c.exit_reason IN ('ESI', 'STL', 'MATERNITY', 'MEDICAL', 'LEAVE')" in sql
        assert "c.expected_return_date IS NOT NULL AND c.expected_return_date >= :as_on" in sql
        assert tuple(lac.RETURN_DATE_EXIT_REASONS) == ("ESI", "STL", "MATERNITY", "MEDICAL", "LEAVE")

    def test_candidates_leave_columns_and_join(self):
        sql = norm(la.get_candidates_query())
        for col in ("lv.leave_transaction_id,", "lv.leave_type_id,", "lv.leave_from_date AS leave_from,",
                    "lv.leave_to_date AS leave_to,", "AS leave_type_name,", "AS pending_leave_since"):
            assert col in sql, col
        # the leave pick is a LATERAL derived table (runs once per candidate, see the query docstring)
        join = sql[sql.index("LEFT JOIN LATERAL ("):sql.index("WHERE DATEDIFF")]
        assert ") lv ON TRUE" in join and "LEFT JOIN leave_transactions lv ON lv.leave_transaction_id = (" not in sql
        assert "l2.leave_to_date >= seen.last_seen" in join and "l2.status = 3" in join
        assert "l2.eb_id = pd.eb_id" in join and "LIMIT 1" in join
        assert "status = 20" not in join                      # a pending request never links a leave
        assert "COALESCE(NULLIF(TRIM(lvm.leave_type_description), ''), lvm.leave_type_code)" in sql

    def test_case_leave_query_reads_only_approved_leave_after_the_last_working_date(self):
        sql = norm(la.get_case_leave_query())
        assert "c.leave_transaction_id AS stored_ltid" in sql
        assert "l2.status = 3" in sql and "l2.leave_to_date > c.last_working_date" in sql
        assert "l2.eb_id = c.eb_id" in sql
        # one covering today first, then the latest, then the highest id
        assert ("ORDER BY (l2.leave_from_date <= :today AND l2.leave_to_date >= :today) DESC, "
                "l2.leave_to_date DESC, l2.leave_transaction_id DESC LIMIT 1") in sql
        assert "c.active = 1 AND c.status <> 0" in sql and "(:case_id IS NULL OR c.case_id = :case_id)" in sql
        assert ":co_id" in sql and ":branch_id" in sql
        for col in ("AS stored_type_id", "AS stored_type_name", "AS stored_from", "AS stored_to",
                    "lv.leave_from_date", "lv.leave_to_date", "AS leave_type_name"):
            assert col in sql, col

    def test_a_pending_request_is_a_hint_only(self):
        """Status 20 appears only in the pending subquery (l3); nothing that links or parks reads it."""
        pending = norm(la.get_pending_leave_query())
        assert "l3.status = 20" in pending and "l3.eb_id = :eb_id" in pending
        assert "l3.leave_from_date <= :today AND l3.leave_to_date >= :today" in pending
        assert "MIN(DATE(l3.updated_date_time))" in pending and "AS pending_leave_since" in pending
        for q in (la.get_case_leave_query(), laq.get_set_case_leave_query(), la.get_last_seen_query(),
                  la.get_rejoin_query(), la.get_leave_types_query(), la.get_leave_type_query()):
            assert "status = 20" not in norm(q)
        for q in (la.get_candidates_query(), la.get_case_list_query(), la.get_pending_leave_query()):
            sql = norm(q)
            assert re.findall(r"\w+\.status = 20", sql) == ["l3.status = 20"], sql
        # the approved-only links of the candidates / case-leave queries use status 3
        for q in (la.get_candidates_query(), la.get_case_leave_query(), la.get_last_seen_query()):
            assert "lt.status = 3" in norm(q) or "l2.status = 3" in norm(q)

    def test_list_query_carries_the_pending_hint(self):
        sql = norm(la.get_case_list_query())
        assert "AS pending_leave_since" in sql and "l3.eb_id = c.eb_id" in sql
        assert ":today" in sql

    def test_set_case_leave_query_writes_the_five_columns_only(self):
        sql = norm(laq.get_set_case_leave_query())
        assert sql.startswith("UPDATE jute_prod_absent_case SET leave_transaction_id = :leave_transaction_id, "
                              "leave_type_id = :leave_type_id, leave_type_name = :leave_type_name, "
                              "leave_from = :leave_from, leave_to = :leave_to WHERE case_id = :case_id")
        assert "updated_by" not in sql and "status" not in sql

    def test_leave_type_queries(self):
        types = norm(la.get_leave_types_query())
        assert "FROM hrms_leave_types_mst WHERE company_id = :co_id AND is_active = 1" in types
        assert types.endswith("ORDER BY leave_type_description, leave_type_code")
        assert "SELECT leave_type_id, leave_type_code, leave_type_description, payable" in types
        one = norm(la.get_leave_type_query())
        assert "WHERE company_id = :co_id AND is_active = 1 AND leave_type_id = :leave_type_id" in one
        assert one.endswith("LIMIT 1") and "ORDER BY" not in one


class TestLeaveRead(_Base):
    """/setup, /candidates, /list, /get with the leave module link."""

    def _types(self, rows=None):
        self.db.on(Q_LEAVE_TYPES, [{**t, "payable": 1} for t in (rows or SLS_LEAVE_TYPES)])

    def test_setup_returns_the_leave_types_options_and_exit_options(self):
        self._types()
        resp = client.get(f"{PREFIX}/setup", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert self.db.params(Q_LEAVE_TYPES) == [{"co_id": 1}]
        assert "verify_options" not in d                              # no manual verification any more
        assert d["leave_types"][0] == {"leave_type_id": 14, "leave_type_code": "A",
                                       "leave_type_description": "AUTHORISED LEAVE", "payable": 1}
        assert [t["leave_type_id"] for t in d["leave_types"]] == [14, 26, 25, 24]
        assert [(o["code"], o["leave_type_id"], o["label"], o["legacy_code"], o["source"])
                for o in d["leave_options"]] == [
            ("LEAVE", 14, "AUTHORISED LEAVE (A)", None, "master"),
            ("LEAVE", 26, "ACCIDENT LEAVE (C)", None, "master"),
            ("LEAVE", 25, "SICK LEAVE (S)", None, "master"),
            ("LEAVE", 24, "STL LEAVE (L)", "STL", "master"),
            ("ESI", None, "On ESI", "ESI", "legacy"),
            ("MATERNITY", None, "Maternity leave", "MATERNITY", "legacy")]
        assert [o["code"] for o in d["exit_options"]] == [
            "REJOINED", "LEAVE", "LEAVE", "LEAVE", "LEAVE", "ESI", "MATERNITY", "MEDICAL", "RESIGNED", "EXPIRED",
            "OTHER"]
        assert d["exit_options"][1]["label"] == "On leave: AUTHORISED LEAVE (A)"
        assert d["exit_options"][5]["label"] == "On ESI"
        assert d["exit_options"] == lac.exit_options(d["leave_options"])
        # the old answers are still in the code lists
        assert "LEAVE" in [e["code"] for e in d["exit_reasons"]]
        assert [e["code"] for e in d["esi_stl_results"]] == ["NONE", "ESI", "STL"]

    def test_setup_without_leave_types_offers_the_legacy_trio(self):
        resp = client.get(f"{PREFIX}/setup", params={"co_id": 1, "branch_id": 2})
        d = resp.json()["data"]
        assert d["leave_types"] == []
        assert [(o["code"], o["source"]) for o in d["leave_options"]] == [
            ("ESI", "legacy"), ("STL", "legacy"), ("MATERNITY", "legacy")]
        assert [o["code"] for o in d["exit_options"]] == [
            "REJOINED", "ESI", "STL", "MATERNITY", "MEDICAL", "RESIGNED", "EXPIRED", "OTHER"]

    def test_setup_skips_a_leave_type_row_without_an_id(self):
        self._types([*SLS_LEAVE_TYPES[:1], {"leave_type_id": None, "leave_type_code": "?",
                                             "leave_type_description": "broken row"}])
        d = client.get(f"{PREFIX}/setup", params={"co_id": 1, "branch_id": 2}).json()["data"]
        assert [t["leave_type_id"] for t in d["leave_types"]] == [14]

    def test_candidates_rows_carry_the_leave_link(self):
        self.db.on(Q_CANDIDATES, [{
            "eb_id": 3, "emp_code": "10000", "emp_name": "Ram Das", "dept_id": 5, "dept_desc": "WEAVING",
            "sub_dept_desc": "LOOMS", "designation": "Weaver", "category": "PERMANENT",
            "last_seen": date(2026, 9, 20), "absent_days": 21, "closed_cases": 0,
            "leave_transaction_id": 901, "leave_type_id": 24, "leave_type_name": "STL LEAVE",
            "leave_from": date(2026, 9, 10), "leave_to": date(2026, 9, 20),
            "pending_leave_since": date(2026, 10, 9)}])
        resp = client.get(f"{PREFIX}/candidates", params={"co_id": 1, "branch_id": 2, "as_on": "2026-10-11"})
        assert resp.status_code == 200, resp.text
        (row,) = resp.json()["data"]
        assert (row["leave_transaction_id"], row["leave_type_id"], row["leave_type_name"]) == (901, 24, "STL LEAVE")
        assert (row["leave_from"], row["leave_to"], row["pending_leave_since"]) == (
            "2026-09-10", "2026-09-20", "2026-10-09")
        # the binds did not change with the leave columns
        assert self.db.params(Q_CANDIDATES) == [{
            "co_id": 1, "branch_id": 2, "from_date": None, "as_on": date(2026, 10, 11), "detect_days": 10,
            "exclude_regex": "^[TCRL]", "dept_id": None}]

    def test_list_leave_refresh_parks_a_new_leave_and_logs_it(self):
        c = self.db.put(make_case(stage_code="VERIFIED", next_due_date=TODAY))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, **LV_STL)])
        self.db.on(Q_LIST, [{**case_row(case_id=1, stage_code="VERIFIED", next_due_date=TODAY, status=2,
                                        leave_parked=1, **CASE_STL),
                             "pending_leave_since": date(2026, 10, 4)},
                            case_row(case_id=2, stage_code="WARNING_1", next_due_date=date(2026, 10, 20))])
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "view": "all"})
        assert resp.status_code == 200, resp.text
        # the leave refresh runs for the branch (case_id None) after the rejoin refresh, before the list
        assert self.db.params(Q_CASE_LEAVE) == [{"co_id": 1, "branch_id": 2, "case_id": None, "today": TODAY}]
        sqls = [s for s, _ in self.db.calls]
        first = lambda needle: next(i for i, s in enumerate(sqls) if needle in s)  # noqa: E731
        assert first(Q_REJOIN) < first(Q_CASE_LEAVE) < first(Q_LIST)
        # the ORM row was parked by the leave and the leave columns stored
        assert (c.status, c.leave_parked) == (2, 1)
        assert (c.leave_transaction_id, c.leave_type_id, c.leave_type_name) == (901, 24, "STL LEAVE")
        assert (c.leave_from, c.leave_to) == (date(2026, 10, 5), date(2026, 10, 20))
        assert (c.stage_code, c.next_due_date) == ("VERIFIED", TODAY)         # the stage / due date are untouched
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.case_id, ev.event_type, ev.text, ev.updated_by, ev.event_date) == (
            1, "SYSTEM", PARKED_TEXT, None, TODAY)
        self.db.commit.assert_called_once()
        rows = resp.json()["data"]
        assert [r["case_id"] for r in rows] == [1, 2]
        assert (rows[0]["due_band"], rows[0]["on_leave_now"], rows[0]["leave_parked"]) == ("ON_LEAVE", True, 1)
        assert rows[0]["on_leave_label"] == "STL LEAVE 05.10.2026 – 20.10.2026 (leave module)"
        assert rows[0]["pending_leave_since"] == "2026-10-04" and rows[1]["pending_leave_since"] is None
        assert rows[1]["on_leave_now"] is False and rows[1]["due_band"] == "NONE"

    def test_list_leave_refresh_with_no_change_commits_nothing(self):
        c = self.db.put(make_case(stage_code="VERIFIED", **CASE_STL))
        # the same leave is stored, the case is open (resumed by hand), VERIFIED already
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, **STORED_STL, **LV_STL)])
        self.db.on(Q_LIST, [case_row(case_id=1, stage_code="VERIFIED", **CASE_STL)])
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        self.db.commit.assert_not_called()
        assert not self.db.added and c.status == 1
        assert resp.json()["data"][0]["due_band"] == "ON_LEAVE"

    def test_list_does_not_repark_a_case_resumed_by_hand(self):
        """The park happens once per leave transaction: list parks, a user resumes, list again."""
        c = self.db.put(make_case(stage_code="VERIFIED", next_due_date=TODAY))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, **LV_STL)])
        client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert (c.status, c.leave_parked) == (2, 1)
        resp = client.post(f"{PREFIX}/resume", json={"co_id": 1, "case_id": 1, "note": "back to work"})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.leave_parked) == (1, 0)
        # the row now reflects the stored link and the resumed state
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, status=c.status, leave_parked=c.leave_parked,
                                            next_due_date=c.next_due_date, **STORED_STL, **LV_STL)])
        events_before, commits_before = len(self.db.added), self.db.commit.call_count
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.leave_parked) == (1, 0)
        assert len(self.db.added) == events_before and self.db.commit.call_count == commits_before
        assert [e.event_type for e in self.db.added_of(JuteProdAbsentEvent)] == ["SYSTEM", "RESUME"]

    def test_list_ended_leave_resumes_the_case_it_parked(self):
        ended = dict(leave_transaction_id=901, leave_type_id=24, leave_type_name="STL LEAVE",
                     leave_from=date(2026, 10, 1), leave_to=date(2026, 10, 8))
        c = self.db.put(make_case(stage_code="WARNING_1", status=2, leave_parked=1,
                                  next_due_date=date(2026, 10, 3), **ended))
        self.db.on(Q_CASE_LEAVE, [leave_row(
            case_id=1, status=2, stage_code="WARNING_1", leave_parked=1, next_due_date=date(2026, 10, 3),
            stored_ltid=901, stored_type_id=24, stored_type_name="STL LEAVE", stored_from=date(2026, 10, 1),
            stored_to=date(2026, 10, 8), leave_transaction_id=901, leave_type_id=24, leave_type_name="STL LEAVE",
            leave_from_date=date(2026, 10, 1), leave_to_date=date(2026, 10, 8))])
        resp = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.leave_parked, c.next_due_date) == (1, 0, date(2026, 10, 9))
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.event_type, ev.text, ev.updated_by) == ("SYSTEM", "Leave ended 08.10.2026, still absent", None)
        self.db.commit.assert_called_once()

    def test_list_withdrawn_leave_clears_the_link_and_resumes(self):
        c = self.db.put(make_case(stage_code="WARNING_1", status=2, leave_parked=1,
                                  next_due_date=date(2026, 10, 3), **CASE_STL))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, status=2, stage_code="WARNING_1", leave_parked=1,
                                            next_due_date=date(2026, 10, 3), **STORED_STL)])    # lv columns None
        client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert (c.status, c.leave_parked, c.next_due_date) == (1, 0, TODAY)
        assert (c.leave_transaction_id, c.leave_type_id, c.leave_type_name, c.leave_from, c.leave_to) == (
            None, None, None, None, None)
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.text == "Leave record withdrawn in the leave module; case resumed"

    def test_list_promotes_detected_cases_and_skips_unknown_ones(self):
        c1 = self.db.put(make_case(case_id=1, stage_code="DETECTED", next_due_date=date(2026, 10, 1)))
        c2 = self.db.put(make_case(case_id=2, stage_code="VERIFIED"))
        self.db.on(Q_CASE_LEAVE, [
            leave_row(case_id=1, stage_code="DETECTED", next_due_date=date(2026, 10, 1)),
            leave_row(case_id=2, stage_code="VERIFIED"),
            leave_row(case_id=99, stage_code="DETECTED")])        # no such ORM row: skipped
        client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert (c1.stage_code, c1.stage_date, c1.verified_date, c1.verify_note, c1.next_due_date) == (
            "VERIFIED", TODAY, TODAY, "Leave module checked automatically", TODAY)
        assert c2.stage_code == "VERIFIED"
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.case_id, ev.event_type, ev.text) == (1, "SYSTEM", AUTO_VERIFY_TEXT)
        self.db.commit.assert_called_once()                        # one commit for the whole branch

    def test_list_closed_view_skips_the_leave_refresh(self):
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, **LV_STL)])
        self.db.put(make_case())
        client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "view": "closed"})
        assert not self.db.ran(Q_CASE_LEAVE) and not self.db.ran(Q_REJOIN)
        assert self.db.params(Q_LIST)[0]["today"] == TODAY           # :today is bound in every view
        for view in ("due", "open", "parked", "all"):
            client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2, "view": view})
        assert [p["case_id"] for p in self.db.params(Q_CASE_LEAVE)] == [None] * 4
        assert self.db.params(Q_LIST)[-1]["today"] == TODAY

    def test_get_refreshes_the_leave_link_and_carries_the_pending_hint(self):
        c = self.db.put(make_case(stage_code="VERIFIED", next_due_date=TODAY))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, **LV_STL)])
        self.db.on(Q_PENDING, [{"pending_leave_since": date(2026, 10, 9)}])
        resp = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert self.db.params(Q_CASE_LEAVE) == [{"co_id": 1, "branch_id": 2, "case_id": 1, "today": TODAY}]
        assert self.db.params(Q_PENDING) == [{"eb_id": 3, "today": TODAY}]
        sqls = [s for s, _ in self.db.calls]
        first = lambda needle: next(i for i, s in enumerate(sqls) if needle in s)  # noqa: E731
        assert first(Q_REJOIN) < first(Q_CASE_LEAVE) < first(Q_PENDING)
        assert (c.status, c.leave_parked, c.leave_transaction_id) == (2, 1, 901)
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.event_type, ev.text, ev.updated_by) == ("SYSTEM", PARKED_TEXT, None)
        self.db.commit.assert_called_once()
        case = resp.json()["data"]["case"]
        assert (case["status"], case["leave_parked"], case["due_band"], case["on_leave_now"]) == (2, 1, "ON_LEAVE", True)
        assert case["on_leave_label"] == "STL LEAVE 05.10.2026 – 20.10.2026 (leave module)"
        assert (case["leave_type_name"], case["leave_from"], case["leave_to"]) == ("STL LEAVE", "2026-10-05", "2026-10-20")
        assert case["pending_leave_since"] == "2026-10-09"

    def test_get_without_leave_or_pending_request(self):
        self.db.put(make_case(stage_code="VERIFIED", next_due_date=TODAY))
        resp = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        case = resp.json()["data"]["case"]
        assert "pending_leave_since" in case and case["pending_leave_since"] is None
        assert (case["due_band"], case["on_leave_now"], case["on_leave_label"]) == ("LETTER_DUE", False, None)
        self.db.commit.assert_not_called()

    def test_get_promotes_a_detected_live_case(self):
        c = self.db.put(make_case(stage_code="DETECTED", next_due_date=date(2026, 10, 1)))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, stage_code="DETECTED", next_due_date=date(2026, 10, 1))])
        resp = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert (c.stage_code, c.stage_date, c.verified_date, c.verify_note, c.next_due_date) == (
            "VERIFIED", TODAY, TODAY, "Leave module checked automatically", TODAY)
        assert c.status == 1
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert (ev.event_type, ev.text, ev.updated_by) == ("SYSTEM", AUTO_VERIFY_TEXT, None)
        self.db.commit.assert_called_once()
        case = resp.json()["data"]["case"]
        assert (case["stage_code"], case["due_band"], case["next_stage_code"]) == ("VERIFIED", "LETTER_DUE", "WARNING_1")
        # a second visit finds nothing to do
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, stage_code="VERIFIED", next_due_date=TODAY)])
        client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        assert len(self.db.added_of(JuteProdAbsentEvent)) == 1 and self.db.commit.call_count == 1

    def test_a_pending_leave_request_never_parks_a_case(self):
        """Regression guard (owner ruling 12.10.2026: only APPROVED leave, status 3, counts). A worker with
        only a status-20 request gets lv columns None from the SQL: nothing changes, the request is a hint."""
        c = self.db.put(make_case(stage_code="VERIFIED", next_due_date=TODAY))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1)])                   # what the SQL returns: no approved leave
        self.db.on(Q_PENDING, [{"pending_leave_since": date(2026, 10, 9)}])
        self.db.on(Q_LIST, [{**case_row(case_id=1, stage_code="VERIFIED", next_due_date=TODAY),
                             "pending_leave_since": date(2026, 10, 9)}])
        got = client.get(f"{PREFIX}/get", params={"co_id": 1, "case_id": 1})
        lst = client.get(f"{PREFIX}/list", params={"co_id": 1, "branch_id": 2})
        assert got.status_code == 200 and lst.status_code == 200
        assert (c.status, c.leave_parked, c.leave_transaction_id, c.stage_code) == (1, 0, None, "VERIFIED")
        assert not self.db.added and c.leave_from is None
        self.db.commit.assert_not_called()
        case = got.json()["data"]["case"]
        assert (case["due_band"], case["on_leave_now"], case["pending_leave_since"]) == (
            "LETTER_DUE", False, "2026-10-09")
        row = lst.json()["data"][0]
        assert (row["due_band"], row["on_leave_now"], row["pending_leave_since"]) == ("LETTER_DUE", False, "2026-10-09")
        # ... and the SQL really cannot return a pending request as the approved leave
        sql = norm(la.get_case_leave_query())
        assert "l2.status = 3" in sql and "status = 20" not in sql


class TestLeaveLifecycle(_Base):
    """/open, /reopen, /exit, /resume with the leave module link."""

    def _open_db(self):
        self.db.on(Q_SNAPSHOT, [SNAPSHOT])
        self.db.on(Q_LAST_ATT, [{"daily_atten_id": 1, "attendance_date": date(2026, 9, 20),
                                 "spell_code": "A1", "machine_name": "LOOM 12"}])
        self.db.on(Q_LAST_SEEN, [{"last_seen": date(2026, 9, 20)}])

    # ---- /open ----------------------------------------------------------------

    def test_open_parks_a_worker_already_on_leave_right_away(self):
        self._open_db()
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=500, **LV_STL)])
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 200, resp.text
        (c,) = self.db.added_of(JuteProdAbsentCase)
        assert self.db.params(Q_CASE_LEAVE) == [{"co_id": 1, "branch_id": 2, "case_id": 500, "today": TODAY}]
        assert (c.stage_code, c.status, c.leave_parked) == ("VERIFIED", 2, 1)
        assert (c.leave_transaction_id, c.leave_type_id, c.leave_type_name, c.leave_from, c.leave_to) == (
            901, 24, "STL LEAVE", date(2026, 10, 5), date(2026, 10, 20))
        opened, parked = self.db.added_of(JuteProdAbsentEvent)
        assert opened.text == "Case opened. Last working date 20.09.2026, 21 days absent. 1st warning due now."
        assert (parked.event_type, parked.text, parked.updated_by, parked.event_date) == (
            "SYSTEM", PARKED_TEXT, None, TODAY)
        assert self.db.commit.call_count == 2                      # the open, then the leave refresh
        d = resp.json()["data"]
        assert (d["status"], d["status_label"], d["due_band"], d["on_leave_now"]) == (2, "Parked", "ON_LEAVE", True)
        assert d["case_no"] == "LA/2/26/0500" and d["leave_parked"] == 1

    def test_open_with_an_ended_leave_only_links_it(self):
        self._open_db()
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=500, **{**LV_STL, "leave_to_date": date(2026, 9, 25)})])
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        assert resp.status_code == 200, resp.text
        (c,) = self.db.added_of(JuteProdAbsentCase)
        assert (c.status, c.leave_parked, c.leave_transaction_id, c.leave_to) == (1, 0, 901, date(2026, 9, 25))
        assert len(self.db.added_of(JuteProdAbsentEvent)) == 1     # only "Case opened"
        d = resp.json()["data"]
        assert (d["due_band"], d["on_leave_now"]) == ("LETTER_DUE", False)

    def test_open_ignores_a_pending_leave_request(self):
        self._open_db()
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=500)])          # only a status-20 request: no approved leave
        self.db.on(Q_PENDING, [{"pending_leave_since": date(2026, 10, 9)}])
        resp = client.post(f"{PREFIX}/open", json={"co_id": 1, "branch_id": 2, "eb_id": 3})
        (c,) = self.db.added_of(JuteProdAbsentCase)
        assert resp.status_code == 200 and (c.status, c.leave_transaction_id) == (1, None)
        assert resp.json()["data"]["due_band"] == "LETTER_DUE" and self.db.commit.call_count == 1

    # ---- /reopen --------------------------------------------------------------

    def test_reopen_of_a_detected_case_with_an_ordinary_exit_is_promoted_too(self):
        c = self.db.put(make_case(status=0, stage_code="DETECTED", exit_reason="ESI", exit_date=date(2026, 10, 5),
                                  exit_remarks="cert", next_due_date=date(2026, 10, 3), leave_parked=1))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "Did not return"})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.stage_code, c.next_due_date, c.leave_parked) == (1, "VERIFIED", TODAY, 0)
        assert (c.verified_date, c.verify_note) == (TODAY, "Leave module checked automatically")
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.text == "Reopened. It was closed on 05.10.2026 (On ESI): cert. Did not return. 1st warning due now."
        assert resp.json()["data"]["due_band"] == "LETTER_DUE"

    def test_reopen_of_a_later_stage_is_not_promoted_or_reworded(self):
        c = self.db.put(make_case(status=0, stage_code="WARNING_2", exit_reason="RESIGNED", leave_parked=1))
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "Back"})
        assert resp.status_code == 200, resp.text
        assert c.stage_code == "WARNING_2" and c.verify_note is None and c.leave_parked == 0
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert "1st warning due now" not in ev.text and ev.text.endswith(". Back")

    def test_reopen_links_a_leave_the_worker_is_on(self):
        self.db.put(make_case(status=0, stage_code="WARNING_1", exit_reason="REJOINED"))
        self.db.on(Q_CASE_LEAVE, [leave_row(case_id=1, stage_code="WARNING_1", **LV_STL)])
        resp = client.post(f"{PREFIX}/reopen", json={"co_id": 1, "case_id": 1, "note": "Absent again"})
        assert resp.status_code == 200, resp.text
        assert self.db.params(Q_CASE_LEAVE) == [{"co_id": 1, "branch_id": 2, "case_id": 1, "today": TODAY}]
        reopen, parked = self.db.added_of(JuteProdAbsentEvent)
        assert (reopen.event_type, parked.event_type, parked.text) == ("REOPEN", "SYSTEM", PARKED_TEXT)
        d = resp.json()["data"]
        assert (d["status"], d["leave_parked"], d["due_band"]) == (2, 1, "ON_LEAVE")

    # ---- /exit ----------------------------------------------------------------

    def test_exit_leave_needs_a_leave_type(self):
        base = {"co_id": 1, "case_id": 1, "exit_reason": "LEAVE", "exit_date": "2026-10-05"}
        for extra in ({}, {"leave_type_id": None}, {"leave_type_id": ""}):
            resp = client.post(f"{PREFIX}/exit", json={**base, **extra})
            assert resp.status_code == 400, extra
            assert resp.json()["detail"] == "leave_type_id is required when exit_reason is LEAVE"
        resp = client.post(f"{PREFIX}/exit", json={**base, "leave_type_id": "x"})
        assert resp.status_code == 400 and resp.json()["detail"] == "Invalid leave_type_id format"
        assert not self.db.calls                                  # all refused before the database

    def test_exit_leave_type_must_be_an_active_type_of_the_company(self):
        c = self.db.put(make_case(stage_code="WARNING_1"))
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 1, "exit_reason": "LEAVE", "exit_date": "2026-10-05", "leave_type_id": 99})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "leave_type_id is not an active leave type of this company"
        assert self.db.params(Q_LEAVE_TYPE) == [{"co_id": 1, "leave_type_id": 99}]
        assert (c.status, c.exit_reason, c.leave_type_id, c.leave_type_name) == (1, None, None, None)
        self.db.commit.assert_not_called()
        assert not self.db.added

    def test_exit_leave_happy_path(self):
        c = self.db.put(make_case(stage_code="WARNING_2", leave_parked=1, **CASE_STL))
        self.db.on(Q_LEAVE_TYPE, [{"leave_type_id": 24, "leave_type_code": "L",
                                   "leave_type_description": "STL LEAVE", "payable": 0}])
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 1, "exit_reason": "leave", "exit_date": "2026-10-05", "leave_type_id": "24",
            "exit_remarks": "Long leave", "expected_return_date": "2026-10-25"})
        assert resp.status_code == 200, resp.text
        assert self.db.params(Q_LEAVE_TYPE) == [{"co_id": 1, "leave_type_id": 24}]
        assert (c.status, c.stage_code, c.exit_reason, c.exit_date, c.expected_return_date) == (
            0, "WARNING_2", "LEAVE", date(2026, 10, 5), date(2026, 10, 25))
        assert (c.leave_type_id, c.leave_type_name, c.leave_parked) == (24, "STL LEAVE", 0)
        assert c.leave_transaction_id == 901                       # a linked module leave stays linked
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.event_type == "EXIT"
        assert ev.text == "On leave: STL LEAVE: Long leave (expected back 25.10.2026)"
        d = resp.json()["data"]
        assert d["exit_reason_label"] == "On leave: STL LEAVE" and d["due_band"] == "NONE"
        assert (d["leave_type_id"], d["leave_type_name"], d["status"]) == (24, "STL LEAVE", 0)
        self.db.commit.assert_called_once()

    def test_exit_leave_names_the_type_by_its_code_when_the_description_is_blank(self):
        c = self.db.put(make_case())
        self.db.on(Q_LEAVE_TYPE, [{"leave_type_id": 31, "leave_type_code": "XL", "leave_type_description": "  "}])
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 1, "exit_reason": "LEAVE", "exit_date": "2026-10-05", "leave_type_id": 31})
        assert resp.status_code == 200, resp.text
        assert c.leave_type_name == "XL"
        assert self.db.added_of(JuteProdAbsentEvent)[0].text == "On leave: XL"

    def test_exit_with_a_legacy_reason_needs_no_leave_type(self):
        c = self.db.put(make_case(stage_code="WARNING_1", status=2, leave_parked=1))
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 1, "exit_reason": "STL", "exit_date": "2026-10-05",
            "exit_remarks": "STL certificate", "expected_return_date": "2026-11-05"})
        assert resp.status_code == 200, resp.text
        assert not self.db.ran(Q_LEAVE_TYPE)
        assert (c.status, c.exit_reason, c.leave_type_id, c.leave_type_name, c.leave_parked) == (
            0, "STL", None, None, 0)
        assert self.db.added_of(JuteProdAbsentEvent)[0].text == "On STL: STL certificate (expected back 05.11.2026)"
        # a leave_type_id sent with another reason is neither looked up nor stored
        c2 = self.db.put(make_case(case_id=2))
        resp = client.post(f"{PREFIX}/exit", json={
            "co_id": 1, "case_id": 2, "exit_reason": "RESIGNED", "exit_date": "2026-10-05", "leave_type_id": 24})
        assert resp.status_code == 200 and not self.db.ran(Q_LEAVE_TYPE) and c2.leave_type_id is None

    # ---- /resume --------------------------------------------------------------

    def test_resume_clears_leave_parked_and_keeps_the_link(self):
        c = self.db.put(make_case(status=2, stage_code="WARNING_1", leave_parked=1,
                                  next_due_date=date(2026, 10, 1), **CASE_STL))
        resp = client.post(f"{PREFIX}/resume", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert (c.status, c.leave_parked, c.next_due_date) == (1, 0, TODAY)
        assert c.leave_transaction_id == 901                       # so the same leave never parks it again
        data = resp.json()["data"]
        assert data["leave_parked"] == 0 and data["due_band"] == "ON_LEAVE"
        assert self.db.added_of(JuteProdAbsentEvent)[0].text == "Resumed"


# =============================================================================
# Timeline
# =============================================================================


class TestTimeline(_Base):
    def test_events_list(self):
        self.db.put(make_case())
        self.db.on(Q_EVENTS, [event_row(event_id=2, event_type="SYSTEM", text="Case opened."),
                              event_row(event_id=1, event_type="REMARK")])
        resp = client.get(f"{PREFIX}/events", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200
        assert [e["event_id"] for e in resp.json()["data"]] == [2, 1]
        assert resp.json()["data"][0]["event_type_label"] == "System"

    def test_event_create_validation(self):
        base = {"co_id": 1, "case_id": 1, "event_date": "2026-10-05", "text": "x"}
        for body in ({**base, "event_type": "STAGE"}, {**base, "event_type": "REMARK", "event_date": "2026-10-12"},
                     {**base, "event_type": "REMARK", "text": " "},
                     {**base, "event_type": "REPLY", "channel": "PIGEON"},
                     {**base, "event_type": "REMARK", "text": "x" * 1001}):
            assert client.post(f"{PREFIX}/event_create", json=body).status_code == 400, body
        assert not self.db.calls

    def test_reply_parks_an_open_case(self):
        c = self.db.put(make_case(stage_code="WARNING_1"))
        self.db.on(Q_EVENTS, lambda p: [event_row(event_id=p["event_id"], event_type="REPLY", channel="UNION_REP")])
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "case_id": 1, "event_type": "reply", "event_date": "2026-10-10",
            "channel": "union_rep", "contact_name": "Rao (INTUC)", "text": "Sick, will report"})
        assert resp.status_code == 200, resp.text
        assert c.status == 2
        reply, park = self.db.added_of(JuteProdAbsentEvent)
        assert (reply.event_type, reply.event_date, reply.channel, reply.contact_name) == (
            "REPLY", date(2026, 10, 10), "UNION_REP", "Rao (INTUC)")
        assert (park.event_type, park.event_date) == ("PARK", TODAY)
        d = resp.json()["data"]
        assert d["event"]["event_id"] == reply.event_id and d["event"]["channel_label"] == "Union representative"
        assert d["case"]["status"] == 2 and d["case"]["due_band"] == "REPLY_REVIEW"
        assert self.db.params(Q_EVENTS)[0]["event_id"] == reply.event_id

    def test_reply_without_park_or_with_pending_approval_keeps_status(self):
        c = self.db.put(make_case())
        self.db.on(Q_EVENTS, [event_row()])
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "case_id": 1, "event_type": "REPLY", "event_date": "2026-10-10", "text": "x", "park": False})
        assert resp.status_code == 200 and c.status == 1
        c2 = self.db.put(make_case(case_id=2, stage_code="GATE_NOTICE", status_id=20))
        client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "case_id": 2, "event_type": "REPLY", "event_date": "2026-10-10", "text": "x"})
        assert c2.status == 1
        assert [e.event_type for e in self.db.added_of(JuteProdAbsentEvent)] == ["REPLY", "REPLY"]

    def test_remark_on_closed_case_drops_channel(self):
        c = self.db.put(make_case(status=0, exit_reason="RESIGNED"))
        self.db.on(Q_EVENTS, [event_row()])
        resp = client.post(f"{PREFIX}/event_create", json={
            "co_id": 1, "case_id": 1, "event_type": "REMARK", "event_date": "2026-10-10",
            "text": "Settled dues", "channel": "PHONE"})
        assert resp.status_code == 200, resp.text
        (ev,) = self.db.added_of(JuteProdAbsentEvent)
        assert ev.channel is None and c.status == 0

    def _event(self, **kw):
        ev = JuteProdAbsentEvent(**{"event_id": 5, "case_id": 1, "event_type": "REMARK",
                                    "event_date": date(2026, 10, 5), "text": "x", "active": 1,
                                    "updated_by": 7, **kw})
        return self.db.put(ev)

    def test_event_update_own_entry_with_create(self):
        self.db.put(make_case())
        ev = self._event(event_type="REPLY", channel="PHONE")
        self.db.on(Q_EVENTS, [event_row(event_id=5)])
        self.access.return_value = LEVEL_CREATE
        resp = client.post(f"{PREFIX}/event_update", json={
            "co_id": 1, "event_id": 5, "text": " fixed ", "channel": "letter", "event_date": "2026-10-04"})
        assert resp.status_code == 200, resp.text
        assert (ev.text, ev.channel, ev.event_date, ev.contact_name) == ("fixed", "LETTER", date(2026, 10, 4), None)
        self.db.commit.assert_called_once()

    def test_event_update_others_entry_needs_edit(self):
        self.db.put(make_case())
        ev = self._event(updated_by=99)
        self.access.return_value = LEVEL_CREATE
        resp = client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 5, "text": "y"})
        assert resp.status_code == 403 and ev.text == "x"
        self.access.return_value = LEVEL_EDIT
        self.db.on(Q_EVENTS, [event_row(event_id=5)])
        assert client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 5, "text": "y"}).status_code == 200
        assert ev.text == "y" and ev.updated_by == 7

    def test_event_update_refusals(self):
        self.db.put(make_case())
        self._event(event_type="STAGE")
        resp = client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 5, "text": "y"})
        assert resp.status_code == 400 and "System" in resp.json()["detail"]
        assert client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 5, "text": ""}).status_code == 400
        assert client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 6, "text": "y"}).status_code == 404
        self._event(event_id=7, active=0)
        assert client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 7, "text": "y"}).status_code == 404
        self.db.put(make_case(case_id=2, co_id=9))
        self._event(event_id=8, case_id=2)
        assert client.post(f"{PREFIX}/event_update", json={"co_id": 1, "event_id": 8, "text": "y"}).status_code == 404
        self.db.commit.assert_not_called()

    def test_event_deactivate(self):
        self.db.put(make_case())
        ev = self._event()
        resp = client.post(f"{PREFIX}/event_deactivate", json={"co_id": 1, "event_id": 5})
        assert resp.status_code == 200 and resp.json()["data"] == {"event_id": 5, "active": 0}
        assert ev.active == 0
        self._event(event_id=6, event_type="SYSTEM")
        resp = client.post(f"{PREFIX}/event_deactivate", json={"co_id": 1, "event_id": 6})
        assert resp.status_code == 400


# =============================================================================
# Performance
# =============================================================================


class TestPerformance(_Base):
    def test_not_available_for_other_trades(self):
        self.db.put(make_case(designation="Helper", dept_desc="BATCHING", sub_dept_desc="BATCHING A"))
        with patch.object(la, "fetch_dept_rows") as fetch:
            resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert d["available"] is False and "weavers, spinners and winding" in d["reason"]
        fetch.assert_not_called()
        self.db.commit.assert_not_called()

    def test_weaver_uses_the_low_performer_helpers_and_caches(self):
        c = self.db.put(make_case(designation="Weaver", last_working_date=date(2026, 9, 20)))
        rows = [
            {"tran_date": "2026-09-01", "shift": "A", "spell_code": "A1", "machine_id": 1, "machine": "L1",
             "eb_id": 3, "eb_no": "10000", "worker_name": "Ram", "quality_id": 7, "quality": "H",
             "prod_kg": 40.0, "std_kg": 50.0, "eff_pct": 80.0, "hours": 8.0},
            {"tran_date": "2026-09-02", "shift": "A", "spell_code": "A1", "machine_id": 1, "machine": "L1",
             "eb_id": 3, "eb_no": "10000", "worker_name": "Ram", "quality_id": 7, "quality": "H",
             "prod_kg": 30.0, "std_kg": 50.0, "eff_pct": 60.0, "hours": 6.0},
            {"tran_date": "2026-09-02", "shift": "B", "spell_code": "B1", "machine_id": 1, "machine": "L1",
             "eb_id": 4, "eb_no": "10001", "worker_name": "Other", "quality_id": 7, "quality": "H",
             "prod_kg": 45.0, "std_kg": 50.0, "eff_pct": 90.0, "hours": 8.0},
        ]
        with patch.object(la, "fetch_dept_rows", return_value=rows) as fetch:
            resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        args = fetch.call_args[0]
        assert args[1:] == ("WEAVING", 1, 2, date(2026, 8, 21), date(2026, 9, 20))
        d = resp.json()["data"]
        assert d["available"] is True and d["dept"] == "WEAVING"
        assert (d["date_from"], d["date_to"]) == ("2026-08-21", "2026-09-20")
        assert d["summary"]["days_worked"] == 2 and d["summary"]["avg_eff"] == 70.0
        assert d["summary"]["kg_per_hr"] == 5.0 and len(d["rows"]) == 2
        assert (c.perf_avg_eff, c.perf_kg_per_hr, c.perf_days_worked, c.perf_dept) == (70.0, 5.0, 2, "WEAVING")
        assert (c.perf_from, c.perf_to) == (date(2026, 8, 21), date(2026, 9, 20))
        self.db.commit.assert_called_once()

    def test_winding_by_department(self):
        self.db.put(make_case(designation="Helper", dept_desc="WINDING"))
        with patch.object(la, "fetch_dept_rows", return_value=[]) as fetch:
            resp = client.get(f"{PREFIX}/performance", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200 and resp.json()["data"]["dept"] == "WINDING"
        assert fetch.call_args[0][1] == "WINDING"
        assert resp.json()["data"]["summary"]["avg_eff"] is None


# =============================================================================
# Access levels (menu juteProduction/report/longAbsenteeism)
# =============================================================================


class TestAccess(_Base):
    """Every endpoint checks the caller's level on the menu for the branch: view
    (1) reads, create (3) adds, edit (4) changes. Id-based calls use the STORED
    case's branch (2), never a branch_id in the body."""

    REQUIRED = {
        "setup": LEVEL_VIEW, "candidates": LEVEL_VIEW, "list": LEVEL_VIEW, "get": LEVEL_VIEW,
        "events": LEVEL_VIEW, "performance": LEVEL_VIEW,
        "open": LEVEL_CREATE, "dismiss": LEVEL_CREATE, "verify": LEVEL_CREATE, "update": LEVEL_CREATE,
        "park": LEVEL_CREATE, "event_create": LEVEL_CREATE, "event_update_own": LEVEL_CREATE,
        "update_lwd": LEVEL_EDIT, "resume": LEVEL_EDIT, "exit": LEVEL_EDIT, "exit_leave": LEVEL_EDIT,
        "reopen": LEVEL_EDIT,
        "event_update_other": LEVEL_EDIT, "event_deactivate": LEVEL_EDIT,
    }

    def _arrange(self, name):
        status = {"resume": 2, "reopen": 0}.get(name, 1)
        self.db.put(make_case(status=status, exit_reason="RESIGNED" if status == 0 else None))
        self.db.put(JuteProdAbsentEvent(event_id=5, case_id=1, event_type="REMARK", event_date=date(2026, 10, 5),
                                        text="x", active=1, updated_by=99 if name == "event_update_other" else 7))
        self.db.on(Q_SNAPSHOT, [SNAPSHOT])
        self.db.on(Q_LAST_SEEN, [{"last_seen": date(2026, 9, 20)}])
        self.db.on(Q_EVENTS, [event_row(event_id=5)])
        self.db.on(Q_LEAVE_TYPE, [{"leave_type_id": 24, "leave_type_code": "L",
                                   "leave_type_description": "STL LEAVE", "payable": 0}])
        g = {"co_id": 1, "branch_id": 2}
        ids = {"co_id": 1, "case_id": 1}
        return {
            "setup": ("get", "setup", {"params": g}),
            "candidates": ("get", "candidates", {"params": g}),
            "list": ("get", "list", {"params": g}),
            "get": ("get", "get", {"params": ids}),
            "events": ("get", "events", {"params": ids}),
            "performance": ("get", "performance", {"params": ids}),
            "open": ("post", "open", {"json": {**g, "eb_id": 3}}),
            "dismiss": ("post", "dismiss", {"json": {**g, "eb_id": 3, "reason": "x"}}),
            "verify": ("post", "verify", {"json": {**ids, "branch_id": 99, "esi_stl_result": "NONE"}}),
            "update": ("post", "update", {"json": {**ids, "branch_id": 99, "remarks": "x"}}),
            "update_lwd": ("post", "update", {"json": {**ids, "last_working_date": "2026-09-19"}}),
            "park": ("post", "park", {"json": {**ids, "note": "x"}}),
            "resume": ("post", "resume", {"json": ids}),
            "exit": ("post", "exit", {"json": {**ids, "exit_reason": "RESIGNED", "exit_date": "2026-10-05"}}),
            "exit_leave": ("post", "exit", {"json": {
                **ids, "exit_reason": "LEAVE", "exit_date": "2026-10-05", "leave_type_id": 24}}),
            "reopen": ("post", "reopen", {"json": {**ids, "note": "x"}}),
            "event_create": ("post", "event_create", {"json": {
                **ids, "event_type": "REMARK", "event_date": "2026-10-05", "text": "x"}}),
            "event_update_own": ("post", "event_update", {"json": {"co_id": 1, "event_id": 5, "text": "y"}}),
            "event_update_other": ("post", "event_update", {"json": {"co_id": 1, "event_id": 5, "text": "y"}}),
            "event_deactivate": ("post", "event_deactivate", {"json": {"co_id": 1, "event_id": 5}}),
        }[name]

    @pytest.mark.parametrize("name", list(REQUIRED))
    def test_level_below_required_is_403(self, name):
        for level in range(0, self.REQUIRED[name]):
            self.teardown_method()      # fresh DB + patches per level (pytest stops the last set)
            self.setup_method()
            self.access.return_value = level
            method, path, kw = self._arrange(name)
            with patch.object(la, "fetch_dept_rows", return_value=[]):
                resp = getattr(client, method)(f"{PREFIX}/{path}", **kw)
            assert resp.status_code == 403, (name, level, resp.text)
            assert "access to Long Absenteeism for this branch" in resp.json()["detail"]
            assert self.access.call_args[0][1:] == (7, 2)
            self.db.commit.assert_not_called()
            assert not self.db.added

    @pytest.mark.parametrize("name", list(REQUIRED))
    def test_required_level_passes(self, name):
        self.access.return_value = self.REQUIRED[name]
        method, path, kw = self._arrange(name)
        with patch.object(la, "fetch_dept_rows", return_value=[]):
            resp = getattr(client, method)(f"{PREFIX}/{path}", **kw)
        assert resp.status_code == 200, (name, resp.text)

    @pytest.mark.parametrize("path", ["open", "dismiss", "verify", "update", "park", "resume", "exit",
                                      "reopen", "event_create", "event_update", "event_deactivate"])
    def test_post_body_must_be_a_json_object(self, path):
        for raw in (b"[1, 2]", b'"text"', b"42", b"null", b"{not json", b""):
            resp = client.post(f"{PREFIX}/{path}", content=raw, headers={"Content-Type": "application/json"})
            assert resp.status_code == 400, (path, raw, resp.text)
        assert not self.db.calls
        self.db.commit.assert_not_called()


def test_router_paths_match_the_contract():
    paths = sorted(r.path for r in long_absenteeism_router.routes)
    assert paths == sorted([
        "/setup", "/candidates", "/list", "/get", "/open", "/dismiss", "/verify", "/update", "/park",
        "/resume", "/exit", "/reopen", "/events", "/event_create", "/event_update", "/event_deactivate",
        "/performance",
    ])
    for r in long_absenteeism_router.routes:
        assert not inspect.iscoroutinefunction(r.endpoint), f"{r.path} must be a plain def"
