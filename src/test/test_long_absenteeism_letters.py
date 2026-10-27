"""Tests for the Long Absenteeism letters router (src/juteProduction/long_absenteeism_letters.py),
its attachment gate (src/common/attachments/router.py) and the HRMS lifecycle helper
(src/hrms/employee.apply_lifecycle_status) it calls on struck-off approval.

Portal persona: DB + auth mocked via app.dependency_overrides (routers mounted on their
own app). The session is a small fake that answers SQL by the ``/* la-... */`` tag each
statement carries and keeps ORM rows by primary key, so a test states the data it needs
instead of the order of the queries. Menu access is patched on long_absenteeism_common.

Covered (spec 2026-10-11-long-absenteeism-followup.md, section 9, BE-2):
  * pure: build_merge_fields (every placeholder, 2nd warning cites the 1st with its sent
    date, gate notice carries the three warnings + show cause, habitual note + earlier
    cases), the three warning wordings, render (no blank line for an empty
    previous_notices), unknown template tokens, letter gates in order (not open,
    wrong stage, already issued, previous not sent, not due, memo), struck-off send gates,
    rule validation, report shaping
  * endpoints: preview (merged text, blocked reason, can_force, editable false), issue
    (the template merged server-side — a client subject / body is ignored —, 409
    duplicate stage, 400 blocked, force with FORCED event, date order, access 403 / 404),
    batch (issued / skipped, batch_ref), letter_update (sent date recomputes next due,
    sent_date before issue / in the future), templates + rule (save / reset / validation),
    (six templates, the retired WARNING code refused), receipts, dispatch + reports bind
    values, struck-off send / approve (engine patched:
    3 -> case closed STRUCK_OFF + employee Terminated 41) / reject / reopen / permissions
  * attachment gate per module and action, through the generic /api/attachments routes
  * owner 2026-10-12: menu path resolution (HRMS row first, legacy Jute Production row
    until the menu migration runs), bulk reprint /letters_print (order, 404 unknown id,
    view per branch), /letters_pdf + build_letters_pdf (magic bytes, one page per letter,
    letterhead, logo), back-dated /letter_record (next stage only, no wait / sent-date
    gate, date order, recorded = 1, case advanced like /letter_issue), the deferred
    ``recorded`` column never named by the issue paths
  * apply_lifecycle_status + /employee_status_update unchanged behaviour
"""

import base64
import re
import zlib
from datetime import date, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

from src.common.attachments import router as attachments_router_module
from src.hrms import employee as hrms_employee
from src.juteProduction import long_absenteeism_common as lac
from src.juteProduction import long_absenteeism_letters as lal
from src.juteProduction.long_absenteeism_common import (
    LETTER_NO_OF_STAGE,
    PLACEHOLDERS,
    RULE_DEFAULTS,
    TEMPLATE_DEFAULTS,
    TEMPLATE_OF_STAGE,
    case_to_dict,
)
from src.juteProduction.long_absenteeism_letters import (
    build_merge_fields,
    build_monthly_trend,
    check_absent_attachment_access,
    clean_rule,
    get_current_user_with_refresh,
    get_tenant_db,
    letter_block,
    letter_block_reason,
    month_keys,
    render_letter,
    shape_stage_summary,
    struck_off_send_block,
    gate_notice_due,
    unknown_tokens,
)
from src.juteProduction.long_absenteeism_pdf import build_letters_pdf, decode_logo, letterhead_lines
from src.juteProduction.long_absenteeism_models import (
    JuteProdAbsentCase,
    JuteProdAbsentEvent,
    JuteProdAbsentLetter,
    JuteProdAbsentRule,
    JuteProdAbsentTemplate,
    JuteProdAbsentUnionReceipt,
)

PREFIX = "/api/longAbsenteeism"
ATT = "/api/attachments"
app = FastAPI()
app.include_router(lal.long_absenteeism_letters_router, prefix=PREFIX)
app.include_router(attachments_router_module.router, prefix=ATT)
client = TestClient(app)

hrms_app = FastAPI()
hrms_app.include_router(hrms_employee.router, prefix="/api/hrms")
hrms_client = TestClient(hrms_app)

TODAY = date(2026, 10, 11)
NOW = datetime(2026, 10, 11, 9, 30)
RULE = dict(RULE_DEFAULTS, co_id=1)
COMPANY = {"co_id": 1, "co_name": "EMPIRE JUTE MILLS", "co_address1": "1 River Road",
           "co_address2": "Titagarh", "co_zipcode": 700119, "branch_id": 2, "branch_name": "Factory"}


# =============================================================================
# Fake session
# =============================================================================


class Row(tuple):
    def __new__(cls, m):
        obj = super().__new__(cls, tuple(m.values()))
        obj._mapping = dict(m)
        return obj


class Result:
    def __init__(self, rows=None, lastrowid=None):
        self._rows = [r if isinstance(r, Row) else Row(r) for r in (rows or [])]
        self.lastrowid = lastrowid

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchone(self):
        return self.first()

    def fetchall(self):
        return list(self._rows)


def _pk_name(obj) -> str:
    return sa_inspect(type(obj)).primary_key[0].name


class FakeDB:
    """Answers db.execute by the tag found in the SQL; db.get / add / flush by PK."""

    def __init__(self):
        self.handlers = {}
        self.calls = []
        self.objects = {}
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self._ids = 500
        self.on("SELECT 1 FROM branch_mst", [{"one": 1}])  # verify_branch: branch is in co_id

    def on(self, tag, value):
        self.handlers[tag] = value

    def execute(self, stmt, params=None):
        sql = str(stmt)
        p = dict(params or {})
        self.calls.append((sql, p))
        for tag, value in self.handlers.items():
            if tag in sql:
                v = value(p) if callable(value) else value
                return v if isinstance(v, Result) else Result(v)
        return Result([])

    def params_of(self, tag) -> list:
        return [p for sql, p in self.calls if tag in sql]

    def put(self, obj):
        self.objects[(type(obj), int(getattr(obj, _pk_name(obj))))] = obj
        return obj

    def get(self, cls, pk, **kw):
        return self.objects.get((cls, int(pk)))

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, _pk_name(obj)) is not None:
            self.put(obj)

    def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, _pk_name(obj)) is None:
                self._ids += 1
                setattr(obj, _pk_name(obj), self._ids)
                self.put(obj)

    def commit(self):
        self.flush()
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def refresh(self, obj):
        pass

    def added_of(self, cls) -> list:
        return [o for o in self.added if isinstance(o, cls)]


def make_case(**kw) -> JuteProdAbsentCase:
    base = dict(
        case_id=1, co_id=1, branch_id=2, eb_id=3, case_no="LA/2/26/0001", emp_code="10001",
        emp_name="RAM KUMAR", dept_id=5, dept_desc="SPINNING", sub_dept_desc="SPG A",
        designation="SPINNER", category="PERMANENT", father_name="SHYAM KUMAR",
        address="Vill Kalna, Dist Burdwan, West Bengal, 713409", mobile="9000000000",
        last_working_date=date(2026, 8, 1), detected_date=date(2026, 8, 20), absent_days_at_open=19,
        rejoined_seen_date=None, stage_code="VERIFIED", stage_date=date(2026, 8, 21),
        next_due_date=date(2026, 8, 21), status=1, habitual=0, timekeeper_confirmed=1,
        exit_reason=None, exit_date=None, struck_off_date=None, status_id=None, approval_level=None,
        active=1, updated_by=7,
    )
    base.update(kw)
    return JuteProdAbsentCase(**base)


_letter_ids = iter(range(100, 10000))
REJOIN_TAG = "AS first_seen"   # get_rejoin_query (long_absenteeism_query, untagged)
OTHERS_TAG = "case_id <> :case_id"   # get_other_cases_query (long_absenteeism_query, untagged)
EARLIER = [  # the worker's other cases, newest first (get_other_cases_query)
    {"case_id": 0, "case_no": None, "detected_date": date(2026, 3, 1), "stage_code": "DETECTED", "status": 0,
     "exit_reason": "DISMISSED", "exit_date": date(2026, 3, 1), "struck_off_date": None},
    {"case_id": 3, "case_no": "LA/2/26/0003", "detected_date": date(2026, 2, 2), "stage_code": "VERIFIED",
     "status": 0, "exit_reason": "ESI", "exit_date": date(2026, 2, 10), "struck_off_date": None},
    {"case_id": 12, "case_no": "LA/2/25/0012", "detected_date": date(2025, 8, 5), "stage_code": "WARNING_2",
     "status": 0, "exit_reason": "REJOINED", "exit_date": date(2025, 9, 1), "struck_off_date": None},
]
EARLIER_SENTENCE = ("Earlier long-absence cases on your record: LA/2/25/0012 (detected 05.08.2025, rejoined); "
                    "LA/2/26/0003 (detected 02.02.2026, on ESI).")
HABITUAL_SENTENCE = "Your attendance record also shows that you have been habitually absent without leave."


def rejoin_rows(first_seen, case_id=1):
    return [{"case_id": case_id, "rejoined_seen_date": None, "first_seen": first_seen}]


def letter_row(stage, issue, sent=None, **kw) -> dict:
    m = dict(
        letter_id=next(_letter_ids), case_id=1, stage_code=stage, letter_no=LETTER_NO_OF_STAGE.get(stage),
        template_code=TEMPLATE_OF_STAGE[stage], issue_date=issue, subject="S", body="B",
        reply_due_date=None, dispatch_mode="REG_POST_AD", sent_date=sent, ad_no=None,
        delivery_status=None, delivery_date=None, gate_display_date=None, dept_display_date=None,
        batch_ref=None, forced=0, active=1, updated_by=7, updated_date_time=None,
    )
    m.update(kw)
    return m


def full_ladder(sent=True) -> list:
    """W1 .. SC issued 01.09 / 12.09 / 23.09 / 05.10, sent two days later."""
    def s(d):
        return d + timedelta(days=2) if sent else None
    return [
        letter_row("WARNING_1", date(2026, 9, 1), s(date(2026, 9, 1)), ad_no="RA1"),
        letter_row("WARNING_2", date(2026, 9, 12), s(date(2026, 9, 12))),
        letter_row("WARNING_3", date(2026, 9, 23), s(date(2026, 9, 23))),
        letter_row("SHOW_CAUSE", date(2026, 10, 5), s(date(2026, 10, 5))),
    ]


def _status(e) -> int:
    return e.value.status_code


# =============================================================================
# Merge fields and rendering (pure)
# =============================================================================


class TestMergeFields:
    def test_every_placeholder_is_produced(self):
        f = build_merge_fields(case_to_dict(make_case(), TODAY), [], RULE, COMPANY, "WARNING_1",
                               date(2026, 9, 1))
        assert set(PLACEHOLDERS) <= set(f)
        assert f["name"] == "RAM KUMAR" and f["eb_no"] == "10001" and f["dept"] == "SPINNING"
        assert f["last_working_date"] == "01.08.2026"
        assert f["absent_days"] == 31                       # 01.08 -> 01.09
        assert f["letter_date"] == "01.09.2026"
        assert (f["letter_no"], f["letter_ordinal"]) == (1, "1st")
        assert (f["reply_days"], f["reply_due_date"]) == (7, "08.09.2026")
        assert f["previous_notices"] == "" and f["letters_issued"] == ""
        assert f["habitual_note"] == "" and f["earlier_cases"] == ""
        assert f["company_name"] == "EMPIRE JUTE MILLS"
        assert f["company_address"] == "1 River Road, Titagarh - 700119"
        assert f["signatory"] == "Personnel Manager" and f["sc_reply_days"] == 3
        assert f["clause_text"].startswith("Clause 14(C)(V)")

    def test_second_warning_cites_the_first_with_its_sent_date(self):
        letters = [letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3), ad_no="RA123")]
        f = build_merge_fields(case_to_dict(make_case(stage_code="WARNING_1")), letters, RULE, COMPANY,
                               "WARNING_2", date(2026, 9, 14))
        assert (f["letter_no"], f["letter_ordinal"]) == (2, "2nd")
        assert f["warning1_date"] == "01.09.2026" and f["warning1_sent"] == "03.09.2026"
        assert "1st warning letter dated 01.09.2026, sent on 03.09.2026 by registered post with A.D." \
            in f["previous_notices"]
        assert "(A.D. No. RA123)" in f["previous_notices"]
        assert "2nd notice" in f["previous_notices"]
        _, body = render_letter(TEMPLATE_DEFAULTS["WARNING_2"], f)
        assert "This is in continuation of our 1st warning letter dated 01.09.2026" in body
        assert "Ref: LA/2/26/0001 / 2nd warning letter" in body
        assert "Despite our earlier letter you have neither reported for duty" in body

    def test_gate_notice_has_the_warnings_and_the_show_cause(self):
        f = build_merge_fields(case_to_dict(make_case(stage_code="SHOW_CAUSE")), full_ladder(), RULE,
                               COMPANY, "GATE_NOTICE", date(2026, 10, 11))
        assert (f["warning1_date"], f["warning1_sent"]) == ("01.09.2026", "03.09.2026")
        assert (f["warning2_date"], f["warning2_sent"]) == ("12.09.2026", "14.09.2026")
        assert (f["warning3_date"], f["warning3_sent"]) == ("23.09.2026", "25.09.2026")
        assert (f["show_cause_date"], f["show_cause_sent"]) == ("05.10.2026", "07.10.2026")
        assert f["gate_notice_date"] is None                # the letter being written is not cited
        assert f["reply_days"] == 7 and f["reply_due_date"] == "18.10.2026"   # notice_reply_days
        _, body = render_letter(TEMPLATE_DEFAULTS["GATE_NOTICE"], f)
        assert "(1) 1st warning dated 01.09.2026, sent on 03.09.2026" in body
        assert "Show Cause Letter for unauthorised leave from duty dated 05.10.2026" in body
        assert "3 more days were given" in body
        assert "________" not in body

    def test_only_earlier_letters_are_cited(self):
        letters = full_ladder()
        f = build_merge_fields(case_to_dict(make_case(stage_code="WARNING_1")), letters, RULE, COMPANY,
                               "WARNING_2", date(2026, 9, 14))
        assert f["warning2_date"] is None and f["show_cause_date"] is None
        assert "2nd warning" not in f["previous_notices"]

    def test_absent_days_stop_at_the_rejoin_date(self):
        f = build_merge_fields(case_to_dict(make_case(rejoined_seen_date=date(2026, 8, 11))), [], RULE,
                               COMPANY, "WARNING_1", date(2026, 9, 1))
        assert f["absent_days"] == 10

    def test_first_warning_drops_the_empty_previous_notices_line(self):
        f = build_merge_fields(case_to_dict(make_case()), [], RULE, COMPANY, "WARNING_1", TODAY)
        assert f["previous_notices"] == ""
        subject, body = render_letter(TEMPLATE_DEFAULTS["WARNING_1"], f)
        assert subject == "NOTICE UNDER RULE 14(C)(V) OF STANDING ORDER FOR LONG ABSENCE"
        assert "________" not in body and "{previous_notices}" not in body
        # the emptied paragraph vanishes cleanly: a single gap remains
        assert "DESIGNATION: SPINNER\n\nIt is noticed from the records" in body
        # the signature gap of the template survives
        assert "as per the company norms.\n\n\nPersonnel Manager\nEMPIRE JUTE MILLS" in body
        assert body.startswith("Date: 11.10.2026\n")

    def test_three_warning_wordings(self):
        """Owner 2026-10-11 (later): mild 1st, firmer 2nd, the owner's last-and-final 3rd."""
        def body_of(stage, letters, case_stage):
            f = build_merge_fields(case_to_dict(make_case(stage_code=case_stage)), letters, RULE, COMPANY, stage,
                                   date(2026, 10, 11))
            subject, body = render_letter(TEMPLATE_DEFAULTS[stage], f)
            assert subject == "NOTICE UNDER RULE 14(C)(V) OF STANDING ORDER FOR LONG ABSENCE"
            assert "________" not in body and "{" not in body
            return body
        ladder = full_ladder()
        w1 = body_of("WARNING_1", [], "VERIFIED")
        w2 = body_of("WARNING_2", ladder[:1], "WARNING_1")
        w3 = body_of("WARNING_3", ladder[:2], "WARNING_2")
        assert len({w1, w2, w3}) == 3
        # 1st: records the absence, cites the clause, report or inform with proof
        assert "absent from duty since 01.08.2026 (71 days as on 11.10.2026) without any leave or information" in w1
        assert "more than 10 days is misconduct as per clause 14(C)(V)" in w1
        assert "Clause 14(C)(V): Habitual absence" in w1
        assert ("report for duty within 7 days from the receipt of this letter, or to inform the Personnel Office "
                "of the reason for your absence with valid proof") in w1
        assert "further action will be taken against you as per the company norms." in w1
        assert "struck off" not in w1 and "continuation" not in w1
        # 2nd: firmer, cites the 1st, final warning then struck off
        assert "This is in continuation of our 1st warning letter dated 01.09.2026" in w2
        assert "Despite our earlier letter you have neither reported for duty nor informed the Personnel Office" in w2
        assert "This is a second and serious warning." in w2
        assert ("a final warning will be issued to you and thereafter your name will be liable to be struck off "
                "from the muster roll of the company.") in w2
        # 3rd: the owner's text, citing both earlier warnings
        assert "our 1st warning letter dated 01.09.2026" in w3 and "our 2nd warning letter dated 12.09.2026" in w3
        assert "This is the 3rd notice" in w3
        assert "This is gross negligence on your part" in w3
        assert "you are hereby provided last and final opportunity to report to your duty within 7 days" in w3
        assert w3.endswith("struck off from the muster roll of the company.\n\n\nPersonnel Manager\nEMPIRE JUTE MILLS")
        for stage in ("WARNING_1", "WARNING_2", "WARNING_3"):
            assert TEMPLATE_DEFAULTS[stage]["body"].startswith("Date: {letter_date}\nRef: {case_no} / {letter_ordinal}")

    def test_habitual_note_and_earlier_cases(self):
        case = case_to_dict(make_case(stage_code="WARNING_2", habitual=1))
        f = build_merge_fields(case, full_ladder()[:2], RULE, COMPANY, "WARNING_3", TODAY, earlier_cases=EARLIER)
        assert f["habitual_note"] == HABITUAL_SENTENCE
        assert f["earlier_cases"] == EARLIER_SENTENCE
        _, body = render_letter(TEMPLATE_DEFAULTS["WARNING_3"], f)
        assert ("on the same matter.\n\n" + HABITUAL_SENTENCE + "\n\n" + EARLIER_SENTENCE
                + "\n\nIt reveals from the record") in body
        f = build_merge_fields(case_to_dict(make_case(stage_code="WARNING_3", habitual=1)), full_ladder()[:3], RULE,
                               COMPANY, "SHOW_CAUSE", TODAY, earlier_cases=EARLIER)
        _, body = render_letter(TEMPLATE_DEFAULTS["SHOW_CAUSE"], f)
        assert (HABITUAL_SENTENCE + "\n\n" + EARLIER_SENTENCE + "\n\nYou have remained absent") in body
        # neither: both paragraphs drop out with a single gap left
        f = build_merge_fields(case_to_dict(make_case(stage_code="WARNING_2")), full_ladder()[:2], RULE, COMPANY,
                               "WARNING_3", TODAY, earlier_cases=EARLIER[:1])
        assert f["habitual_note"] == "" and f["earlier_cases"] == ""
        _, body = render_letter(TEMPLATE_DEFAULTS["WARNING_3"], f)
        assert "on the same matter.\n\nIt reveals from the record" in body
        # only cases detected before the letter's own case are "earlier"
        later = [dict(EARLIER[2], detected_date=date(2026, 9, 1))]
        f = build_merge_fields(case_to_dict(make_case()), [], RULE, COMPANY, "WARNING_3", TODAY, earlier_cases=later)
        assert f["earlier_cases"] == ""
        assert {"habitual_note", "earlier_cases"} <= set(PLACEHOLDERS)

    def test_default_templates_use_known_placeholders(self):
        assert list(TEMPLATE_DEFAULTS) == ["WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE", "GATE_NOTICE",
                                           "STRUCK_OFF_MEMO"]
        for code, tpl in TEMPLATE_DEFAULTS.items():
            assert unknown_tokens(tpl["subject"] + "\n" + tpl["body"]) == [], code
        for code in ("WARNING_3", "SHOW_CAUSE"):
            body = TEMPLATE_DEFAULTS[code]["body"]
            assert body.index("{previous_notices}") < body.index("{habitual_note}") < body.index("{earlier_cases}")

    def test_struck_off_memo_lists_the_letters_neutrally(self):
        letters = full_ladder() + [letter_row("GATE_NOTICE", date(2026, 10, 9), date(2026, 10, 9),
                                              gate_display_date=date(2026, 10, 10))]
        case = make_case(stage_code="STRUCK_OFF", status=0, struck_off_date=date(2026, 10, 20))
        f = build_merge_fields(case_to_dict(case), letters, RULE, COMPANY, "STRUCK_OFF", date(2026, 10, 21))
        assert f["letters_issued"] == (
            "1st warning letter dated 01.09.2026, sent 03.09.2026 (A.D. No. RA1); "
            "2nd warning letter dated 12.09.2026, sent 14.09.2026; "
            "3rd warning letter dated 23.09.2026, sent 25.09.2026; "
            "show cause notice dated 05.10.2026, sent 07.10.2026; "
            "gate notice dated 09.10.2026, displayed 10.10.2026"
        )
        _, body = render_letter(TEMPLATE_DEFAULTS["STRUCK_OFF_MEMO"], f)
        assert "Letters issued: 1st warning letter dated 01.09.2026" in body
        assert "displayed 10.10.2026.\n" in body
        assert "with effect from 20.10.2026" in body
        # no second-person wording on the memo
        assert "{previous_notices}" not in TEMPLATE_DEFAULTS["STRUCK_OFF_MEMO"]["body"]
        assert " you" not in body.lower() and "continuation of our" not in body
        assert "letters_issued" in PLACEHOLDERS

    @pytest.mark.parametrize("address", [None, "", "   "])
    def test_missing_snapshot_value_shows_a_blank_to_fill(self, address):
        f = build_merge_fields(case_to_dict(make_case(address=address)), [], RULE, COMPANY, "WARNING_1", TODAY)
        assert f["address"] is None
        _, body = render_letter(TEMPLATE_DEFAULTS["WARNING_1"], f)
        assert "RAM KUMAR\n________" in body

    def test_missing_earlier_letter_dates_show_blanks_on_the_gate_notice(self):
        f = build_merge_fields(case_to_dict(make_case(stage_code="SHOW_CAUSE")), full_ladder(sent=False), RULE,
                               COMPANY, "GATE_NOTICE", TODAY)
        assert f["warning1_sent"] is None and f["warning1_date"] == "01.09.2026"
        _, body = render_letter(TEMPLATE_DEFAULTS["GATE_NOTICE"], f)
        assert "1st warning dated 01.09.2026, sent on ________" in body

    def test_unknown_tokens(self):
        assert unknown_tokens("Dear {name}, {Name} {xyz} {name} { eb_no } {}") == ["Name", "xyz", " eb_no ", ""]
        assert unknown_tokens(TEMPLATE_DEFAULTS["GATE_NOTICE"]["body"]) == []
        assert unknown_tokens(None) == []


# =============================================================================
# Letter gates (pure)
# =============================================================================


class TestLetterGates:
    def test_not_open_comes_first(self):
        parked = make_case(status=2, stage_code="DETECTED")
        assert "parked" in letter_block_reason(parked, [], RULE, "WARNING_1", TODAY)
        closed = make_case(status=0)
        assert "closed" in letter_block_reason(closed, [], RULE, "WARNING_1", TODAY)
        assert letter_block(closed, [], RULE, "WARNING_1", TODAY)["forceable"] is False

    def test_wrong_stage(self):
        b = letter_block(make_case(stage_code="DETECTED"), [], RULE, "WARNING_1", TODAY)
        assert b["code"] == "WRONG_STAGE" and b["forceable"] is False
        assert "Verified" in b["reason"] and "Detected" in b["reason"]
        b = letter_block(make_case(stage_code="WARNING_1"), [], RULE, "WARNING_3", TODAY)
        assert b["code"] == "WRONG_STAGE"

    def test_first_warning_has_no_wait(self):
        assert letter_block_reason(make_case(stage_date=TODAY), [], RULE, "WARNING_1", TODAY) is None

    def test_already_issued(self):
        letters = [letter_row("WARNING_2", date(2026, 9, 12))]
        b = letter_block(make_case(stage_code="WARNING_1"), letters, RULE, "WARNING_2", TODAY)
        assert b["code"] == "ALREADY_ISSUED" and "12.09.2026" in b["reason"]

    def test_previous_letter_needs_its_sent_date(self):
        letters = [letter_row("WARNING_1", date(2026, 9, 1))]
        b = letter_block(make_case(stage_code="WARNING_1"), letters, RULE, "WARNING_2", TODAY)
        assert b == {"code": "NOT_SENT", "reason": "Record the sent date of the 1st warning first",
                     "forceable": True}

    def test_wait_counts_from_the_sent_date(self):
        # sent 03.09 + a 10-day wait: the 2nd warning opens on 14.09 (day N + 1)
        letters = [letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))]
        case = make_case(stage_code="WARNING_1")
        assert letter_block_reason(case, letters, RULE, "WARNING_2", date(2026, 9, 13)) == "Due on 14.09.2026"
        assert letter_block(case, letters, RULE, "WARNING_2", date(2026, 9, 13))["forceable"] is True
        assert letter_block_reason(case, letters, RULE, "WARNING_2", date(2026, 9, 14)) is None

    def test_waits_per_stage(self):
        ladder = full_ladder()
        # show cause: 15 full days after the 3rd warning was sent (25.09) -> from 11.10
        sc_case = make_case(stage_code="WARNING_3")
        assert letter_block_reason(sc_case, ladder[:3], RULE, "SHOW_CAUSE", date(2026, 10, 10)) == "Due on 11.10.2026"
        assert letter_block_reason(sc_case, ladder[:3], RULE, "SHOW_CAUSE", date(2026, 10, 11)) is None
        # gate notice: after the show-cause reply window (3 days) from its sent date (07.10) -> from 11.10
        gn_case = make_case(stage_code="SHOW_CAUSE")
        assert letter_block_reason(gn_case, ladder, RULE, "GATE_NOTICE", date(2026, 10, 10)) == "Due on 11.10.2026"
        assert letter_block_reason(gn_case, ladder, dict(RULE, sc_reply_days=1), "GATE_NOTICE",
                                   date(2026, 10, 8)) == "Due on 09.10.2026"
        assert letter_block_reason(gn_case, ladder, dict(RULE, sc_reply_days=1), "GATE_NOTICE",
                                   date(2026, 10, 9)) is None

    def test_due_date_matches_the_case_next_due_date(self):
        """letter_due_date (the gate) and due_date_for_next (the stored
        next_due_date, which drives the watchlist band) give the same day."""
        sent_w1 = letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))
        case = make_case(stage_code="WARNING_1", stage_date=date(2026, 9, 1))
        orm_letter = JuteProdAbsentLetter(issue_date=date(2026, 9, 1), sent_date=date(2026, 9, 3))
        assert lal.letter_due_date(case, [sent_w1], RULE, "WARNING_2") == \
            lac.due_date_for_next(case, orm_letter, RULE) == date(2026, 9, 14)

    def test_rejoined_worker_gets_no_letter(self):
        seen = date(2026, 10, 9)
        b = letter_block(make_case(), [], RULE, "WARNING_1", TODAY, rejoined_on=seen)
        assert b == {"code": "REJOINED", "forceable": False,
                     "reason": "Worker attended on 09.10.2026 — exit the case as Rejoined instead"}
        assert letter_block(make_case(), [], RULE, "WARNING_1", TODAY) is None
        # the struck-off memo (a closed, struck-off case) is not affected
        done = make_case(stage_code="STRUCK_OFF", status=0, exit_reason="STRUCK_OFF")
        assert letter_block(done, [], RULE, "STRUCK_OFF", TODAY, rejoined_on=seen) is None

    def test_first_warning_not_dated_before_the_verification(self):
        case = make_case(stage_code="VERIFIED", stage_date=date(2026, 10, 1), verified_date=date(2026, 10, 2))
        b = letter_block(case, [], RULE, "WARNING_1", date(2026, 10, 1))
        assert b["code"] == "NOT_DUE" and b["forceable"] is True and "02.10.2026" in b["reason"]
        assert letter_block(case, [], RULE, "WARNING_1", date(2026, 10, 2)) is None
        # without a verified_date the VERIFIED stage date is the floor
        case = make_case(stage_code="VERIFIED", stage_date=date(2026, 10, 1))
        assert "01.10.2026" in letter_block_reason(case, [], RULE, "WARNING_1", date(2026, 9, 30))

    def test_struck_off_memo(self):
        b = letter_block(make_case(stage_code="GATE_NOTICE"), [], RULE, "STRUCK_OFF", TODAY)
        assert b["code"] == "WRONG_STAGE" and "after the struck-off approval" in b["reason"]
        done = make_case(stage_code="STRUCK_OFF", status=0, exit_reason="STRUCK_OFF")
        assert letter_block(done, [], RULE, "STRUCK_OFF", TODAY) is None
        memo = [letter_row("STRUCK_OFF", TODAY)]
        assert letter_block(done, memo, RULE, "STRUCK_OFF", TODAY)["code"] == "ALREADY_ISSUED"


class TestStruckOffSendGate:
    def _gate(self, **kw):
        return letter_row("GATE_NOTICE", date(2026, 10, 1), date(2026, 10, 2),
                          reply_due_date=date(2026, 10, 8), **kw)

    def test_rules(self):
        at_gate = make_case(stage_code="GATE_NOTICE")
        assert struck_off_send_block(at_gate, self._gate(), TODAY) is None
        # printed reply_due_date 08.10 is the LAST day to reply: sendable from 09.10
        assert struck_off_send_block(at_gate, self._gate(), date(2026, 10, 9)) is None
        assert struck_off_send_block(at_gate, self._gate(), date(2026, 10, 8)) == (
            "Struck off can be sent from 09.10.2026 (the gate notice reply window, counted from its "
            "sent date, runs to 08.10.2026)")
        assert "from 09.10.2026" in struck_off_send_block(at_gate, self._gate(), date(2026, 10, 7))
        assert struck_off_send_block(at_gate, self._gate(), TODAY, rejoined_on=date(2026, 10, 3)) == \
            "Worker attended on 03.10.2026 — exit the case as Rejoined instead"
        assert struck_off_send_block(at_gate, self._gate(sent_date=None) | {"sent_date": None}, TODAY) == \
            "Record the sent date of the gate notice first"
        assert "not found" in struck_off_send_block(at_gate, None, TODAY)
        assert "after the gate notice" in struck_off_send_block(make_case(stage_code="SHOW_CAUSE"),
                                                                self._gate(), TODAY)
        assert "parked" in struck_off_send_block(make_case(stage_code="GATE_NOTICE", status=2),
                                                 self._gate(), TODAY)
        assert "Already sent" in struck_off_send_block(make_case(stage_code="GATE_NOTICE", status_id=20),
                                                       self._gate(), TODAY)
        assert struck_off_send_block(make_case(stage_code="GATE_NOTICE", status_id=4), self._gate(), TODAY) is None

    def test_window_counts_from_the_sent_date_when_the_rule_is_known(self):
        # issued 01.10, SENT 02.10, printed reply_due 08.10 (issue + 7): with the
        # rule's 7-day window the real due date is sent + 7 = 09.10.
        at_gate = make_case(stage_code="GATE_NOTICE")
        assert "from 10.10.2026" in struck_off_send_block(at_gate, self._gate(), date(2026, 10, 9), 7)
        assert struck_off_send_block(at_gate, self._gate(), date(2026, 10, 10), 7) is None
        # gate_notice_due is the LAST day of the window (sent + 7)
        assert gate_notice_due(self._gate(), 7) == date(2026, 10, 9)
        assert gate_notice_due(self._gate(), None) == date(2026, 10, 8)
        assert gate_notice_due(None, 7) is None


class TestRuleValidation:
    def test_defaults_pass_and_texts_only_when_sent(self):
        out = clean_rule(RULE, {})
        assert out["detect_days"] == 10 and "clause_text" not in out
        out = clean_rule(RULE, {"signatory": "  ", "extra_1_label": "Union"})
        assert out["signatory"] is None and out["extra_1_label"] == "Union"

    @pytest.mark.parametrize("body,msg", [
        ({"detect_days": 0}, "detect_days must be at least 1"),
        ({"detect_days": 30, "lookback_days": 20}, "lookback_days must be 0 (no limit) or at least detect_days"),
        ({"lookback_days": 3651}, "lookback_days must be between 0 and 3650"),
        ({"lookback_days": -1}, "between 0 and 3650"),
        ({"wait_w2_days": 366}, "between 0 and 365"),
        ({"detect_days": 366}, "detect_days must be between 0 and 365"),
        ({"wait_w3_days": -1}, "between 0 and 365"),
        ({"wait_sc_days": "abc"}, "whole number"),
        ({"sc_reply_days": 2.5}, "whole number"),
        ({"notice_reply_days": True}, "whole number"),
        ({"dismiss_snooze_days": None}, "whole number"),
        ({"clause_text": "x" * 501}, "at most 500"),
    ])
    def test_rejects(self, body, msg):
        with pytest.raises(HTTPException) as e:
            clean_rule(RULE, body)
        assert _status(e) == 400 and msg in e.value.detail

    @pytest.mark.parametrize("detect,lookback", [(30, 0), (10, 0), (30, 30), (10, 400), (10, 3650), (365, 3650)])
    def test_lookback_zero_is_no_limit_else_detect_to_3650(self, detect, lookback):
        out = clean_rule(RULE, {"detect_days": detect, "lookback_days": lookback})
        assert (out["detect_days"], out["lookback_days"]) == (detect, lookback)

    def test_lookback_default_is_zero(self):
        assert RULE["lookback_days"] == 0 and clean_rule(RULE, {"detect_days": 45})["lookback_days"] == 0

    def test_accepts_numeric_strings_and_ignores_the_retired_flag(self):
        out = clean_rule(RULE, {"detect_days": "12", "lookback_days": 60.0, "exclude_t_codes": False})
        assert (out["detect_days"], out["lookback_days"]) == (12, 60)
        assert "exclude_t_codes" not in out          # retired (amendment 11.17): never stored again

    @pytest.mark.parametrize("sent,stored", [
        ("T,C,R,L", "T,C,R,L"),
        (" t c, r ,T ", "T,C,R"),          # case, spaces, repeats -> canonical
        ("l", "L"),
        ("", None), ("   ", None), (" , ", None), (None, None),   # blank / null -> NULL = the default
    ])
    def test_exclude_prefixes_canonical(self, sent, stored):
        assert clean_rule(RULE, {"exclude_prefixes": sent})["exclude_prefixes"] == stored

    def test_exclude_prefixes_only_when_sent(self):
        assert "exclude_prefixes" not in clean_rule(RULE, {"detect_days": 10})

    @pytest.mark.parametrize("sent", ["TX", "T,1", "T;C", "é", "T-C", "C*", 5, ["T"], True, "T," * 25 + "C"])
    def test_exclude_prefixes_rejects_anything_but_letters(self, sent):
        with pytest.raises(HTTPException) as e:
            clean_rule(RULE, {"exclude_prefixes": sent})
        assert _status(e) == 400 and "exclude_prefixes" in e.value.detail


class TestReportShaping:
    def test_stage_summary(self):
        live = [
            {"dept_id": 5, "dept_desc": "SPINNING", "stage_code": "VERIFIED", "parked": 0, "n": 2},
            {"dept_id": 5, "dept_desc": "SPINNING", "stage_code": "WARNING_1", "parked": 1, "n": 3},
            {"dept_id": 4, "dept_desc": "BATCHING", "stage_code": "DETECTED", "parked": 0, "n": 1},
        ]
        exits = [{"exit_reason": "REJOINED", "n": 4}, {"exit_reason": "STRUCK_OFF", "n": 1}]
        out = shape_stage_summary(live, exits)
        assert [d["dept_desc"] for d in out["by_dept"]] == ["BATCHING", "SPINNING"]
        spg = out["by_dept"][1]
        assert spg["counts"]["VERIFIED"] == 2 and spg["counts"]["WARNING_1"] == 3
        assert spg["counts"]["STRUCK_OFF"] == 0 and spg["parked"] == 1 and spg["total_live"] == 5
        assert out["totals"]["total_live"] == 6 and out["totals"]["counts"]["DETECTED"] == 1
        assert out["exits"][0] == {"exit_reason": "REJOINED", "label": "Rejoined", "count": 4}

    def test_monthly_trend_fills_every_month(self):
        keys = month_keys(date(2026, 2, 15), 3)
        assert keys == ["2025-12", "2026-01", "2026-02"]
        rows = [{"month": "2026-01", "kind": "opened", "n": 4}, {"month": "2026-01", "kind": "struck_off", "n": 1},
                {"month": "2025-11", "kind": "opened", "n": 9}]
        out = build_monthly_trend(rows, keys)
        assert out[0] == {"month": "2025-12", "opened": 0, "struck_off": 0, "rejoined": 0, "other_exits": 0}
        assert out[1]["opened"] == 4 and out[1]["struck_off"] == 1


# =============================================================================
# Endpoints
# =============================================================================


class Base:
    level = lac.LEVEL_EDIT

    def setup_method(self):
        self.db = FakeDB()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        self.patches = [
            patch.object(lac, "access_level", side_effect=lambda db, uid, br: self.level),
            patch.object(lac, "menu_id", return_value=77),
            patch.object(lal, "today_ist", return_value=TODAY),
            patch.object(lac, "today_ist", return_value=TODAY),
            patch.object(lal, "now_ist", return_value=NOW),
        ]
        for p in self.patches:
            p.start()

    def teardown_method(self):
        for p in self.patches:
            p.stop()
        app.dependency_overrides.clear()

    def put_case(self, **kw) -> JuteProdAbsentCase:
        return self.db.put(make_case(**kw))

    def set_letters(self, letters):
        self.db.on("la-case-letters", letters)

    def events(self) -> list:
        return self.db.added_of(JuteProdAbsentEvent)


class TestPreview(Base):
    def test_preview_merges_the_next_letter(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))])
        self.db.on("la-letterhead", [COMPANY])
        resp = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert (d["stage_code"], d["template_code"], d["letter_no"]) == ("WARNING_2", "WARNING_2", 2)
        assert d["template_label"] == "2nd warning letter"
        assert d["subject"] == TEMPLATE_DEFAULTS["WARNING_2"]["subject"]
        assert "Ref: LA/2/26/0001 / 2nd warning letter" in d["body"] and "EMPIRE JUTE MILLS" in d["body"]
        assert "This is a second and serious warning." in d["body"]
        assert d["editable"] is False                       # the text is never edited by hand
        assert d["blocked_reason"] is None and d["can_force"] is True
        assert d["due_date"] == "2026-09-14" and d["reply_due_date"] == "2026-10-18"   # sent 03.09 + 10, then
        assert d["fields"]["warning1_sent"] == "03.09.2026"

    def test_saved_template_and_unsaved_override(self):
        self.put_case()
        self.db.on("la-templates", [{"template_id": 1, "template_code": "WARNING_1", "lang": "en",
                                     "subject": "Warn {eb_no}", "body": "Saved {name}",
                                     "updated_by": 7, "updated_date_time": None}])
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert (d["subject"], d["body"]) == ("Warn 10001", "Saved RAM KUMAR")
        d = client.post(f"{PREFIX}/letter_preview", json={
            "co_id": 1, "case_id": 1, "template_body": "Draft {dept}"}).json()["data"]
        assert d["body"] == "Draft SPINNING"

    def test_earlier_cases_and_habitual_note_reach_the_letter(self):
        self.put_case(stage_code="WARNING_3", habitual=1)
        self.set_letters(full_ladder()[:3])
        self.db.on(OTHERS_TAG, EARLIER)
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["stage_code"] == "SHOW_CAUSE"
        assert d["fields"]["earlier_cases"] == EARLIER_SENTENCE and EARLIER_SENTENCE in d["body"]
        assert d["fields"]["habitual_note"] == HABITUAL_SENTENCE and HABITUAL_SENTENCE in d["body"]
        # the same query BE-1's /get uses for earlier_cases
        assert self.db.params_of(OTHERS_TAG) == [{"co_id": 1, "branch_id": 2, "eb_id": 3, "case_id": 1}]

    def test_blocked_reason_and_can_force(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1))])
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["blocked_code"] == "NOT_SENT" and d["can_force"] is True
        assert d["body"]                                     # the text is still merged
        self.level = lac.LEVEL_VIEW
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["can_force"] is False
        self.level = lac.LEVEL_EDIT
        d = client.post(f"{PREFIX}/letter_preview", json={
            "co_id": 1, "case_id": 1, "stage_code": "SHOW_CAUSE"}).json()["data"]
        assert d["blocked_code"] == "WRONG_STAGE" and d["can_force"] is False

    def test_rejoined_worker_preview_is_blocked_and_not_forceable(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))])
        self.db.on(REJOIN_TAG, rejoin_rows(date(2026, 10, 9)))
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["blocked_code"] == "REJOINED" and d["can_force"] is False
        assert d["blocked_reason"] == "Worker attended on 09.10.2026 — exit the case as Rejoined instead"
        assert self.db.params_of(REJOIN_TAG) == [{"co_id": 1, "branch_id": 2, "case_id": 1}]

    def test_detected_case_previews_the_first_warning_blocked(self):
        self.put_case(stage_code="DETECTED")
        d = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["stage_code"] == "WARNING_1" and d["blocked_code"] == "WRONG_STAGE"

    def test_by_case_number(self):
        self.put_case(case_id=9)
        self.db.on("la-case-by-no", [{"case_id": 9}])
        resp = client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_no": "LA/2/26/0009"})
        assert resp.status_code == 200 and resp.json()["data"]["case_id"] == 9
        assert self.db.params_of("la-case-by-no") == [{"co_id": 1, "case_no": "LA/2/26/0009"}]

    def test_access_and_company(self):
        self.put_case(co_id=9)
        assert client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).status_code == 404
        self.put_case()
        self.level = 0
        assert client.post(f"{PREFIX}/letter_preview", json={"co_id": 1, "case_id": 1}).status_code == 403
        assert client.post(f"{PREFIX}/letter_preview", json={"co_id": 1}).status_code == 400


class TestIssue(Base):
    def _body(self, **kw):
        b = {"co_id": 1, "case_id": 1, "stage_code": "WARNING_1"}
        b.update(kw)
        return b

    def test_happy_path(self):
        c = self.put_case(stage_code="VERIFIED", stage_date=date(2026, 10, 1))
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body())
        assert resp.status_code == 200, resp.text
        assert "FOR UPDATE" in self.db.calls[0][0]           # the lock is the first statement
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert (letter.stage_code, letter.letter_no, letter.template_code) == ("WARNING_1", 1, "WARNING_1")
        # the default 1st-warning template merged on the server (no letterhead row: blank company)
        assert letter.subject == TEMPLATE_DEFAULTS["WARNING_1"]["subject"]
        assert letter.body.startswith("Date: 11.10.2026\nRef: LA/2/26/0001 / 1st warning letter\n")
        assert "absent from duty since 01.08.2026 (71 days as on 11.10.2026)" in letter.body
        assert letter.issue_date == TODAY and letter.reply_due_date == TODAY + timedelta(days=7)
        assert letter.dispatch_mode == "REG_POST_AD" and letter.forced == 0 and letter.batch_ref is None
        assert (c.stage_code, c.stage_date) == ("WARNING_1", TODAY)
        assert c.next_due_date == TODAY + timedelta(days=11)   # 10 full days after the issue date until sent
        (ev,) = self.events()
        assert ev.event_type == "STAGE" and ev.ref_letter_id == letter.letter_id
        assert ev.text == "1st warning issued, letter dated 11.10.2026" and ev.event_date == TODAY
        assert self.db.commits == 1
        d = resp.json()["data"]
        assert d["letter"]["letter_id"] == letter.letter_id and d["case"]["stage_code"] == "WARNING_1"
        # the shared case shape (case_out): next step + band
        assert (d["case"]["next_stage_code"], d["case"]["next_stage_label"], d["case"]["due_band"]) == (
            "WARNING_2", "2nd warning", "NONE")

    def test_client_text_is_ignored_the_template_is_stored(self):
        """Owner 2026-10-11: letters are not edited by hand — subject / body in the request are ignored."""
        self.put_case(stage_code="WARNING_2", habitual=1)
        self.set_letters(full_ladder()[:2])
        self.db.on("la-letterhead", [COMPANY])
        self.db.on(OTHERS_TAG, EARLIER)
        self.db.on("la-templates", [{"template_id": 1, "template_code": "WARNING_3", "lang": "en",
                                     "subject": "LAST WARNING {eb_no}", "body": "Dear {name},\n{earlier_cases}",
                                     "updated_by": 7, "updated_date_time": None}])
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            stage_code="WARNING_3", subject="Typed subject", body="x" * 30000))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert (letter.template_code, letter.subject) == ("WARNING_3", "LAST WARNING 10001")
        assert letter.body == "Dear RAM KUMAR,\n" + EARLIER_SENTENCE
        assert resp.json()["data"]["letter"]["body"] == letter.body
        assert self.db.params_of(OTHERS_TAG) == [{"co_id": 1, "branch_id": 2, "eb_id": 3, "case_id": 1}]
        assert self.db.params_of("la-letterhead") == [{"branch_id": 2}]

    def test_default_third_warning_with_habitual_note(self):
        self.put_case(stage_code="WARNING_2", habitual=1)
        self.set_letters(full_ladder()[:2])
        self.db.on("la-letterhead", [COMPANY])
        self.db.on(OTHERS_TAG, EARLIER)
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(stage_code="WARNING_3"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert HABITUAL_SENTENCE + "\n\n" + EARLIER_SENTENCE in letter.body
        assert "last and final opportunity" in letter.body and letter.body.endswith("EMPIRE JUTE MILLS")
        assert "________" not in letter.body

    def test_gate_notice_reply_window_and_next_due(self):
        c = self.put_case(stage_code="SHOW_CAUSE")
        self.set_letters(full_ladder())
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(stage_code="GATE_NOTICE"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.dispatch_mode == "GATE_BOARD" and letter.letter_no is None
        assert letter.reply_due_date == TODAY + timedelta(days=7)    # last day to reply
        assert c.next_due_date == TODAY + timedelta(days=8)   # struck off may be sent the day after

    def test_duplicate_stage_409(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3)),
                          letter_row("WARNING_2", date(2026, 9, 14))])
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(stage_code="WARNING_2"))
        assert resp.status_code == 409 and "14.09.2026" in resp.json()["detail"]
        assert self.db.commits == 0 and not self.db.added

    def test_blocked_without_force_400(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 10, 5))])
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(stage_code="WARNING_2"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Record the sent date of the 1st warning first"
        assert self.db.commits == 0

    def test_force_path(self):
        c = self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 10, 5))])
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            stage_code="WARNING_2", force=True, force_reason="AD slip lost, posted 06.10"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.forced == 1 and c.stage_code == "WARNING_2"
        (ev,) = self.events()
        assert "FORCED: AD slip lost, posted 06.10" in ev.text

    def test_rejoined_worker_cannot_be_issued_a_letter_even_forced(self):
        c = self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))])
        self.db.on(REJOIN_TAG, rejoin_rows(date(2026, 10, 9)))
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            stage_code="WARNING_2", force=True, force_reason="x"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Worker attended on 09.10.2026 — exit the case as Rejoined instead"
        assert c.stage_code == "WARNING_1" and self.db.commits == 0 and not self.db.added

    def test_first_warning_not_before_the_verification_unless_forced(self):
        c = self.put_case(stage_code="VERIFIED", stage_date=date(2026, 10, 5), verified_date=date(2026, 10, 5))
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(issue_date="2026-10-04"))
        assert resp.status_code == 400 and "before the verification (05.10.2026)" in resp.json()["detail"]
        assert self.db.commits == 0
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            issue_date="2026-10-04", force=True, force_reason="posted before the entry"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.forced == 1 and letter.issue_date == date(2026, 10, 4) and c.stage_code == "WARNING_1"
        assert "FORCED: posted before the entry" in self.events()[0].text

    def test_force_without_block_is_not_marked(self):
        self.put_case()
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(force=True, force_reason="x"))
        assert resp.status_code == 200
        assert self.db.added_of(JuteProdAbsentLetter)[0].forced == 0
        assert "FORCED" not in self.events()[0].text

    def test_force_cannot_skip_a_stage_or_reopen(self):
        self.put_case(stage_code="WARNING_1")
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            stage_code="SHOW_CAUSE", force=True, force_reason="x"))
        assert resp.status_code == 400 and "can only follow" in resp.json()["detail"]
        self.put_case(status=2)
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(force=True, force_reason="x"))
        assert resp.status_code == 400 and "parked" in resp.json()["detail"]

    def test_forced_letter_still_dated_after_the_previous_one(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 10, 5))])
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(
            stage_code="WARNING_2", issue_date="2026-10-04", force=True, force_reason="x"))
        assert resp.status_code == 400 and "previous letter's date (05.10.2026)" in resp.json()["detail"]

    @pytest.mark.parametrize("kw,msg", [
        ({"issue_date": "2026-10-12"}, "future"),
        ({"stage_code": "VERIFIED"}, "Invalid stage_code"),
        ({"force": True}, "force_reason is required"),
        ({"dispatch_mode": "EMAIL"}, "Invalid dispatch_mode"),
    ])
    def test_validation_before_db(self, kw, msg):
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(**kw))
        assert resp.status_code == 400 and msg in resp.json()["detail"]
        assert self.db.calls == []

    def test_access_and_company(self):
        self.put_case()
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/letter_issue", json=self._body()).status_code == 403
        self.level = lac.LEVEL_EDIT
        self.put_case(co_id=5)
        assert client.post(f"{PREFIX}/letter_issue", json=self._body()).status_code == 404
        assert self.db.commits == 0

    def test_memo_after_struck_off_keeps_the_case_closed(self):
        c = self.put_case(stage_code="STRUCK_OFF", status=0, exit_reason="STRUCK_OFF",
                          struck_off_date=date(2026, 10, 10))
        self.set_letters(full_ladder())
        resp = client.post(f"{PREFIX}/letter_issue", json=self._body(stage_code="STRUCK_OFF"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.template_code == "STRUCK_OFF_MEMO" and letter.reply_due_date is None
        assert (c.stage_code, c.status) == ("STRUCK_OFF", 0)
        assert self.events()[0].text.startswith("Struck-off memo issued")


class TestBatch(Base):
    def test_issued_and_skipped(self):
        self.put_case(case_id=1, stage_code="VERIFIED")
        self.put_case(case_id=2, stage_code="WARNING_1", emp_name="SITA")
        self.put_case(case_id=3, branch_id=8)                          # another branch
        self.put_case(case_id=4, rejoined_seen_date=date(2026, 10, 9))
        self.put_case(case_id=5, stage_code="WARNING_1")               # changed by another user
        locks = {1: "VERIFIED", 2: "WARNING_1", 3: "VERIFIED", 4: "VERIFIED", 5: "WARNING_2"}
        self.db.on("la-lock-case", lambda p: [{"case_id": p["case_id"], "stage_code": locks[p["case_id"]],
                                                "status": 1, "active": 1}])
        self.db.on("la-case-letters", lambda p: (
            [letter_row("WARNING_1", date(2026, 10, 5), case_id=2)] if p["case_id"] == 2 else []))
        self.db.on("la-letterhead", [COMPANY])
        resp = client.post(f"{PREFIX}/letter_issue_batch", json={
            "co_id": 1, "branch_id": 2, "case_ids": [1, 2, 3, 4, 5, 1]})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["batch_ref"] == "B20261011-0930-7" and d["issue_date"] == "2026-10-11"
        assert [i["case_id"] for i in d["issued"]] == [1]
        issued = d["issued"][0]
        assert issued["stage_code"] == "WARNING_1" and "RAM KUMAR" in issued["body"]
        assert "It is noticed from the records" in issued["body"]
        assert "________" not in issued["body"]
        reasons = {s["case_id"]: s["reason"] for s in d["skipped"]}
        assert reasons[2] == "Record the sent date of the 1st warning first"
        assert reasons[3] == "Case not found in this branch"
        assert reasons[4].startswith("Marked present on 09.10.2026")
        assert "changed by another user" in reasons[5]
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.batch_ref == "B20261011-0930-7" and letter.forced == 0
        assert "batch B20261011-0930-7" in self.events()[0].text
        assert self.db.commits == 1

    @pytest.mark.parametrize("body", [
        {"co_id": 1, "branch_id": 2, "case_ids": []},
        {"co_id": 1, "branch_id": 2, "case_ids": "1,2"},
        {"co_id": 1, "branch_id": 2, "case_ids": ["x"]},
        {"co_id": 1, "case_ids": [1]},
        {"co_id": 1, "branch_id": 2, "case_ids": list(range(201))},
    ])
    def test_validation(self, body):
        assert client.post(f"{PREFIX}/letter_issue_batch", json=body).status_code == 400

    def test_needs_edit(self):
        self.level = lac.LEVEL_CREATE
        resp = client.post(f"{PREFIX}/letter_issue_batch", json={"co_id": 1, "branch_id": 2, "case_ids": [1]})
        assert resp.status_code == 403

    def test_struck_off_memo_and_rejoined_workers_are_skipped(self):
        self.put_case(case_id=6, stage_code="STRUCK_OFF", status=0, exit_reason="STRUCK_OFF")
        self.put_case(case_id=7, stage_code="VERIFIED")
        locks = {6: ("STRUCK_OFF", 0), 7: ("VERIFIED", 1)}
        self.db.on("la-lock-case", lambda p: [{"case_id": p["case_id"], "stage_code": locks[p["case_id"]][0],
                                                "status": locks[p["case_id"]][1], "active": 1}])
        self.db.on(REJOIN_TAG, lambda p: rejoin_rows(date(2026, 10, 9), p["case_id"]))
        resp = client.post(f"{PREFIX}/letter_issue_batch", json={"co_id": 1, "branch_id": 2, "case_ids": [6, 7]})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["issued"] == []
        reasons = {s["case_id"]: s["reason"] for s in d["skipped"]}
        assert reasons[6] == "The struck-off memo is issued from the case page only"
        assert reasons[7] == "Worker attended on 09.10.2026 — exit the case as Rejoined instead"
        assert not self.db.added_of(JuteProdAbsentLetter)


class TestLetterUpdate(Base):
    def _letter(self, **kw) -> JuteProdAbsentLetter:
        base = dict(letter_id=40, case_id=1, stage_code="WARNING_1", letter_no=1, template_code="WARNING_1",
                    issue_date=date(2026, 9, 1), body="B", active=1, forced=0, sent_date=None,
                    dispatch_mode="REG_POST_AD", ad_no=None)
        base.update(kw)
        return self.db.put(JuteProdAbsentLetter(**base))

    def test_sent_date_recomputes_next_due(self):
        c = self.put_case(stage_code="WARNING_1", next_due_date=date(2026, 9, 11))
        l = self._letter()
        resp = client.post(f"{PREFIX}/letter_update", json={
            "co_id": 1, "letter_id": 40, "sent_date": "2026-09-04", "ad_no": "RA1"})
        assert resp.status_code == 200, resp.text
        assert l.sent_date == date(2026, 9, 4) and l.ad_no == "RA1"
        assert c.next_due_date == date(2026, 9, 15)            # 04.09 + 10 full days -> from 15.09
        (ev,) = self.events()
        assert ev.event_type == "SYSTEM" and ev.text == "1st warning sent on 04.09.2026 (A.D. No. RA1)"
        assert ev.event_date == date(2026, 9, 4) and ev.ref_letter_id == 40
        assert resp.json()["data"]["case"]["next_due_date"] == "2026-09-15"
        assert resp.json()["data"]["case"]["due_band"] == "LETTER_DUE"   # case_out shape

    def test_older_letter_does_not_move_the_due_date(self):
        c = self.put_case(stage_code="WARNING_2", next_due_date=date(2026, 9, 30))
        self._letter()
        assert client.post(f"{PREFIX}/letter_update", json={
            "co_id": 1, "letter_id": 40, "sent_date": "2026-09-04"}).status_code == 200
        assert c.next_due_date == date(2026, 9, 30) and len(self.events()) == 1

    def test_other_fields_add_no_event(self):
        self.put_case(stage_code="WARNING_1")
        l = self._letter(sent_date=date(2026, 9, 4))
        resp = client.post(f"{PREFIX}/letter_update", json={
            "co_id": 1, "letter_id": 40, "delivery_status": "delivered", "delivery_date": "2026-09-08"})
        assert resp.status_code == 200
        assert (l.delivery_status, l.delivery_date) == ("DELIVERED", date(2026, 9, 8))
        assert self.events() == []

    @pytest.mark.parametrize("body,msg", [
        ({"sent_date": "2026-08-31"}, "before the letter date (01.09.2026)"),
        ({"sent_date": "2026-10-12"}, "future"),
        ({"sent_date": "2026-09-10", "delivery_date": "2026-09-05"}, "delivery_date cannot be before sent_date"),
        ({"delivery_status": "LOST"}, "Invalid delivery_status"),
        ({}, "Nothing to update"),
    ])
    def test_rejects(self, body, msg):
        self.put_case(stage_code="WARNING_1")
        self._letter()
        resp = client.post(f"{PREFIX}/letter_update", json={"co_id": 1, "letter_id": 40, **body})
        assert resp.status_code == 400 and msg in resp.json()["detail"]
        assert self.db.commits == 0

    def test_access(self):
        self.put_case(stage_code="WARNING_1")
        self._letter()
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/letter_update", json={
            "co_id": 1, "letter_id": 40, "sent_date": "2026-09-04"}).status_code == 403
        self.level = lac.LEVEL_EDIT
        assert client.post(f"{PREFIX}/letter_update", json={
            "co_id": 2, "letter_id": 40, "sent_date": "2026-09-04"}).status_code == 404


class TestLettersAndLists(Base):
    def test_letters_with_receipts_and_attachments(self):
        self.put_case(stage_code="GATE_NOTICE")
        gn = letter_row("GATE_NOTICE", date(2026, 10, 1), letter_id=60)
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), letter_id=59), gn])
        self.db.on("la-case-receipts", [{"receipt_id": 3, "letter_id": 60, "union_name": "INTUC",
                                         "received_by": "Sec", "received_date": date(2026, 10, 2),
                                         "remarks": None, "active": 1, "updated_by": 7,
                                         "updated_date_time": None}])
        self.db.on("la-case-attachments", [
            {"module": "jute_prod_absent_letter", "entity_id": 59, "file_type": "ad_card",
             "attachment_id": 11, "original_filename": "ad.pdf"},
            {"module": "jute_prod_absent_receipt", "entity_id": 3, "file_type": "union_receipt",
             "attachment_id": 12, "original_filename": "r.pdf"},
        ])
        resp = client.get(f"{PREFIX}/letters", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        w1, g = resp.json()["data"]
        assert w1["attachments"] == {"signed_copy": None, "ad_card": {"attachment_id": 11, "filename": "ad.pdf"}}
        assert w1["receipts"] == [] and w1["stage_label"] == "1st warning"
        assert g["receipts"][0]["union_name"] == "INTUC"
        assert g["receipts"][0]["attachment"] == {"attachment_id": 12, "filename": "r.pdf"}
        assert self.db.params_of("la-case-attachments") == [{
            "co_id": 1, "case_id": 1, "letter_module": "jute_prod_absent_letter",
            "receipt_module": "jute_prod_absent_receipt"}]

    def test_letters_due(self):
        rows = [
            {"case_id": 1, "case_no": "A", "eb_id": 1, "emp_code": "1", "emp_name": "A", "dept_id": 5,
             "dept_desc": "D", "designation": "X", "last_working_date": date(2026, 9, 1),
             "rejoined_seen_date": None, "stage_code": "VERIFIED", "stage_date": date(2026, 10, 1),
             "next_due_date": None, "status": 1, "status_id": None},
            {"case_id": 2, "case_no": "B", "eb_id": 2, "emp_code": "2", "emp_name": "B", "dept_id": 5,
             "dept_desc": "D", "designation": "X", "last_working_date": date(2026, 9, 1),
             "rejoined_seen_date": None, "stage_code": "WARNING_1", "stage_date": date(2026, 10, 5),
             "next_due_date": None, "status": 1, "status_id": None},
            {"case_id": 3, "case_no": "C", "eb_id": 3, "emp_code": "3", "emp_name": "C", "dept_id": 5,
             "dept_desc": "D", "designation": "X", "last_working_date": date(2026, 9, 1),
             "rejoined_seen_date": date(2026, 10, 2), "stage_code": "VERIFIED",
             "stage_date": date(2026, 10, 1), "next_due_date": None, "status": 1, "status_id": None},
            {"case_id": 4, "case_no": "D", "eb_id": 4, "emp_code": "4", "emp_name": "D", "dept_id": 5,
             "dept_desc": "D", "designation": "X", "last_working_date": date(2026, 8, 1),
             "rejoined_seen_date": None, "stage_code": "WARNING_1", "stage_date": date(2026, 9, 1),
             "next_due_date": None, "status": 1, "status_id": None},
        ]
        self.db.on("la-branch-open-cases", rows)
        self.db.on("la-branch-open-letters", [
            letter_row("WARNING_1", date(2026, 10, 5), case_id=2),                         # not sent
            letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 2), case_id=4),        # due from 13.09
        ])
        resp = client.get(f"{PREFIX}/letters_due", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert [(r["case_id"], r["next_stage_code"]) for r in d] == [(1, "WARNING_1"), (4, "WARNING_2")]
        assert d[1]["due_date"] == "2026-09-13" and d[1]["absent_days"] == 71
        d = client.get(f"{PREFIX}/letters_due", params={
            "co_id": 1, "branch_id": 2, "stage_code": "WARNING_2"}).json()["data"]
        assert [r["case_id"] for r in d] == [4]

    def test_dispatch_binds(self):
        self.db.on("la-dispatch", [{"letter_id": 1, "case_id": 1, "stage_code": "WARNING_1",
                                    "template_code": "WARNING", "issue_date": date(2026, 10, 1),
                                    "dispatch_mode": "REG_POST_AD", "delivery_status": None}])
        resp = client.get(f"{PREFIX}/dispatch", params={"co_id": 1, "branch_id": 2})
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["dispatch_mode_label"] == "Registered post with AD" and row["issue_date"] == "2026-10-01"
        assert row["template_label"] == "Warning letter (1st / 2nd / 3rd)"   # issued before the split
        assert self.db.params_of("la-dispatch")[0] == {
            "co_id": 1, "branch_id": 2, "date_from": date(2026, 9, 11), "date_to": TODAY,
            "batch_ref": None, "stage_code": None}
        client.get(f"{PREFIX}/dispatch", params={"co_id": 1, "branch_id": 2, "date_from": "2026-10-01",
                                                 "date_to": "2026-10-05", "batch_ref": "B1",
                                                 "stage_code": "show_cause"})
        p = self.db.params_of("la-dispatch")[1]
        assert (p["date_from"], p["date_to"], p["batch_ref"], p["stage_code"]) == (
            date(2026, 10, 1), date(2026, 10, 5), "B1", "SHOW_CAUSE")
        assert client.get(f"{PREFIX}/dispatch", params={
            "co_id": 1, "branch_id": 2, "date_from": "2026-10-05", "date_to": "2026-10-01"}).status_code == 400

    def test_list_endpoints_need_view_on_a_branch_of_the_company(self):
        self.level = 0
        assert client.get(f"{PREFIX}/dispatch", params={"co_id": 1, "branch_id": 2}).status_code == 403
        self.level = lac.LEVEL_VIEW
        self.db.on("SELECT 1 FROM branch_mst", [])
        assert client.get(f"{PREFIX}/letters_due", params={"co_id": 1, "branch_id": 2}).status_code == 400


class TestTemplatesAndRule(Base):
    def test_templates_list(self):
        self.db.on("la-templates", [{"template_id": 1, "template_code": "SHOW_CAUSE", "lang": "en",
                                     "subject": "SC", "body": "Body {name}", "updated_by": 7,
                                     "updated_date_time": datetime(2026, 10, 11, 10, 0)}])
        with patch.object(lal, "company_access_level", return_value=1) as acc:
            resp = client.get(f"{PREFIX}/templates", params={"co_id": 1})
        assert resp.status_code == 200 and acc.call_args[0][1:] == (7, 1)
        d = resp.json()["data"]
        assert [t["template_code"] for t in d["templates"]] == ["WARNING_1", "WARNING_2", "WARNING_3", "SHOW_CAUSE",
                                                                "GATE_NOTICE", "STRUCK_OFF_MEMO"]
        assert [t["label"] for t in d["templates"][:3]] == [
            "1st warning letter", "2nd warning letter", "3rd & last warning letter"]
        sc = d["templates"][3]
        assert (sc["subject"], sc["body"], sc["is_default"]) == ("SC", "Body {name}", False)
        assert d["templates"][0]["is_default"] is True
        assert d["templates"][2]["body"] == TEMPLATE_DEFAULTS["WARNING_3"]["body"]
        assert {"code": "previous_notices", "label": PLACEHOLDERS["previous_notices"]} in d["placeholders"]
        codes = [p["code"] for p in d["placeholders"]]
        assert "habitual_note" in codes and "earlier_cases" in codes

    def test_company_level_access(self):
        with patch.object(lal, "company_access_level", return_value=0):
            assert client.get(f"{PREFIX}/templates", params={"co_id": 1}).status_code == 403
        # with branch_id the branch's level is used
        self.level = lac.LEVEL_VIEW
        assert client.get(f"{PREFIX}/rule", params={"co_id": 1, "branch_id": 2}).status_code == 200

    def test_template_save_rejects_unknown_tokens(self):
        resp = client.post(f"{PREFIX}/template_save", json={
            "co_id": 1, "branch_id": 2, "template_code": "WARNING_1", "subject": "S {nme}",
            "body": "Dear {name}, {xyz} and {nme}"})
        assert resp.status_code == 400
        assert "{nme}, {xyz}" in resp.json()["detail"]
        assert self.db.calls == []

    def test_template_save_insert_and_update(self):
        resp = client.post(f"{PREFIX}/template_save", json={
            "co_id": 1, "branch_id": 2, "template_code": "show_cause", "subject": "SC {eb_no}",
            "body": "Dear {name}"})
        assert resp.status_code == 200, resp.text
        (t,) = self.db.added_of(JuteProdAbsentTemplate)
        assert (t.co_id, t.template_code, t.lang, t.body, t.active) == (1, "SHOW_CAUSE", "en", "Dear {name}", 1)
        assert resp.json()["data"]["is_default"] is False
        existing = self.db.put(JuteProdAbsentTemplate(template_id=5, co_id=1, template_code="WARNING_3",
                                                      lang="en", body="old", active=1))
        self.db.on("la-template-id", [{"template_id": 5}])
        client.post(f"{PREFIX}/template_save", json={
            "co_id": 1, "branch_id": 2, "template_code": "warning_3", "body": "new {case_no} {habitual_note}"})
        assert existing.body == "new {case_no} {habitual_note}" and len(self.db.added_of(JuteProdAbsentTemplate)) == 1
        assert self.db.params_of("la-template-id")[-1] == {"co_id": 1, "template_code": "WARNING_3"}

    def test_template_save_validation(self):
        for body in ({"template_code": "LETTER", "body": "x"}, {"template_code": "WARNING_1", "body": " "},
                     {"template_code": "WARNING_2", "body": "x" * 20001}):
            resp = client.post(f"{PREFIX}/template_save", json={"co_id": 1, "branch_id": 2, **body})
            assert resp.status_code == 400

    @pytest.mark.parametrize("path", ["template_save", "template_reset"])
    @pytest.mark.parametrize("code", ["WARNING", "warning "])
    def test_retired_warning_code_is_refused(self, path, code):
        resp = client.post(f"{PREFIX}/{path}", json={"co_id": 1, "branch_id": 2, "template_code": code,
                                                     "subject": "S", "body": "B"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == ("template_code WARNING is no longer used — the warnings are WARNING_1, "
                                         "WARNING_2 and WARNING_3")
        assert self.db.calls == [] and self.db.commits == 0

    def test_template_reset(self):
        resp = client.post(f"{PREFIX}/template_reset", json={"co_id": 1, "branch_id": 2,
                                                             "template_code": "GATE_NOTICE"})
        assert resp.status_code == 200
        assert resp.json()["data"]["body"] == TEMPLATE_DEFAULTS["GATE_NOTICE"]["body"]
        assert self.db.params_of("la-template-delete") == [{"co_id": 1, "template_code": "GATE_NOTICE"}]
        assert self.db.commits == 1
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/template_reset", json={
            "co_id": 1, "branch_id": 2, "template_code": "GATE_NOTICE"}).status_code == 403

    def test_rule_save(self):
        resp = client.post(f"{PREFIX}/rule_save", json={
            "co_id": 1, "branch_id": 2, "detect_days": 12, "lookback_days": 60, "signatory": "Labour Officer",
            "extra_1_label": "Union"})
        assert resp.status_code == 200, resp.text
        row = self.db.get(JuteProdAbsentRule, 1)
        assert (row.detect_days, row.lookback_days, row.wait_sc_days) == (12, 60, 15)
        assert row.signatory == "Labour Officer" and row.clause_text is None and row.updated_by == 7
        d = resp.json()["data"]
        assert d["detect_days"] == 12 and d["signatory"] == "Labour Officer"
        assert d["clause_text"] == RULE_DEFAULTS["clause_text"] and d["extra_1_label"] == "Union"

    def test_rule_save_exclude_prefixes(self):
        resp = client.post(f"{PREFIX}/rule_save", json={"co_id": 1, "branch_id": 2, "exclude_prefixes": "t, c r,l,T"})
        assert resp.status_code == 200, resp.text
        row = self.db.get(JuteProdAbsentRule, 1)
        assert row.exclude_prefixes == "T,C,R,L"                      # stored canonical
        assert resp.json()["data"]["exclude_prefixes"] == "T,C,R,L"
        assert client.get(f"{PREFIX}/rule", params={"co_id": 1, "branch_id": 2}).json()["data"][
            "exclude_prefixes"] == "T,C,R,L"
        # another list is stored as typed (canonical); blank clears it back to the default
        client.post(f"{PREFIX}/rule_save", json={"co_id": 1, "branch_id": 2, "exclude_prefixes": "t"})
        assert row.exclude_prefixes == "T"
        assert client.get(f"{PREFIX}/rule", params={"co_id": 1, "branch_id": 2}).json()["data"][
            "exclude_prefixes"] == "T"
        resp = client.post(f"{PREFIX}/rule_save", json={"co_id": 1, "branch_id": 2, "exclude_prefixes": " "})
        assert row.exclude_prefixes is None and resp.json()["data"]["exclude_prefixes"] == "T,C,R,L"
        assert self.db.commits == 3

    def test_rule_save_rejects_bad_prefixes(self):
        resp = client.post(f"{PREFIX}/rule_save", json={"co_id": 1, "branch_id": 2, "exclude_prefixes": "T,CX"})
        assert resp.status_code == 400 and "exclude_prefixes" in resp.json()["detail"]
        assert "CX" in resp.json()["detail"]
        assert self.db.commits == 0 and self.db.get(JuteProdAbsentRule, 1) is None

    def test_rule_save_validation_and_access(self):
        resp = client.post(f"{PREFIX}/rule_save", json={"co_id": 1, "branch_id": 2, "detect_days": 0})
        assert resp.status_code == 400 and "detect_days" in resp.json()["detail"]
        assert self.db.commits == 0
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/rule_save", json={
            "co_id": 1, "branch_id": 2, "detect_days": 5}).status_code == 403


class TestReceipts(Base):
    def _gate(self, **kw):
        base = dict(letter_id=60, case_id=1, stage_code="GATE_NOTICE", template_code="GATE_NOTICE",
                    issue_date=date(2026, 10, 1), body="B", active=1, forced=0)
        base.update(kw)
        return self.db.put(JuteProdAbsentLetter(**base))

    def test_create_update_deactivate(self):
        self.put_case(stage_code="GATE_NOTICE")
        self._gate()
        resp = client.post(f"{PREFIX}/receipt_create", json={
            "co_id": 1, "letter_id": 60, "union_name": " INTUC ", "received_by": "Secy",
            "received_date": "2026-10-02"})
        assert resp.status_code == 200, resp.text
        (r,) = self.db.added_of(JuteProdAbsentUnionReceipt)
        assert (r.letter_id, r.union_name, r.received_date) == (60, "INTUC", date(2026, 10, 2))
        rid = resp.json()["data"]["receipt_id"]
        assert client.post(f"{PREFIX}/receipt_update", json={
            "co_id": 1, "receipt_id": rid, "remarks": "copy signed"}).status_code == 200
        assert r.remarks == "copy signed" and r.union_name == "INTUC"
        resp = client.post(f"{PREFIX}/receipt_deactivate", json={"co_id": 1, "receipt_id": rid})
        assert resp.json()["data"] == {"receipt_id": rid, "active": 0} and r.active == 0
        assert client.post(f"{PREFIX}/receipt_update", json={
            "co_id": 1, "receipt_id": rid, "remarks": "x"}).status_code == 404

    def test_rules(self):
        self.put_case(stage_code="SHOW_CAUSE")
        self._gate(stage_code="SHOW_CAUSE")
        resp = client.post(f"{PREFIX}/receipt_create", json={"co_id": 1, "letter_id": 60, "union_name": "CITU"})
        assert resp.status_code == 400 and "gate notice" in resp.json()["detail"]
        self._gate()
        self.put_case(status=0, exit_reason="REJOINED", stage_code="GATE_NOTICE")
        resp = client.post(f"{PREFIX}/receipt_create", json={"co_id": 1, "letter_id": 60, "union_name": "CITU"})
        assert resp.status_code == 400 and "closed" in resp.json()["detail"]
        self.put_case(status=0, exit_reason="STRUCK_OFF", stage_code="STRUCK_OFF")
        assert client.post(f"{PREFIX}/receipt_create", json={
            "co_id": 1, "letter_id": 60, "union_name": "CITU"}).status_code == 200
        for body in ({"union_name": ""}, {"union_name": "CITU", "received_date": "2026-09-30"},
                     {"union_name": "CITU", "received_date": "2026-10-12"}):
            assert client.post(f"{PREFIX}/receipt_create", json={
                "co_id": 1, "letter_id": 60, **body}).status_code == 400
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/receipt_create", json={
            "co_id": 1, "letter_id": 60, "union_name": "CITU"}).status_code == 403

    def test_union_names(self):
        self.db.on("la-union-names", [{"union_name": "INTUC", "uses": 5}, {"union_name": "CITU", "uses": 2}])
        resp = client.get(f"{PREFIX}/union_names", params={"co_id": 1, "branch_id": 2})
        assert resp.json()["data"] == ["INTUC", "CITU"]
        assert self.db.params_of("la-union-names") == [{"co_id": 1, "branch_id": 2}]


class TestStruckOff(Base):
    def _ready(self, **kw):
        c = self.put_case(stage_code="GATE_NOTICE", **kw)
        self.set_letters(full_ladder() + [letter_row("GATE_NOTICE", date(2026, 10, 1), date(2026, 10, 2),
                                                     reply_due_date=date(2026, 10, 8))])
        return c

    def test_send(self):
        c = self._ready()
        resp = client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert (c.status_id, c.approval_level) == (20, 1)
        assert self.events()[0].text == "Sent for struck-off approval" and self.db.commits == 1
        assert resp.json()["data"]["case"]["due_band"] == "APPROVAL"           # case_out shape

    def test_send_refused_for_a_worker_who_came_back(self):
        c = self._ready()
        self.db.on(REJOIN_TAG, rejoin_rows(date(2026, 10, 5)))
        resp = client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Worker attended on 05.10.2026 — exit the case as Rejoined instead"
        assert c.status_id is None and self.db.commits == 0 and not self.events()
        assert self.db.params_of(REJOIN_TAG) == [{"co_id": 1, "branch_id": 2, "case_id": 1}]

    @pytest.mark.parametrize("path,arrange,body", [
        ("send", {}, {}),
        ("approve", {"status_id": 20, "approval_level": 1}, {}),
        ("reject", {"status_id": 20, "approval_level": 1}, {"reason": "x"}),
        ("reopen", {"status_id": 4}, {}),
    ])
    def test_every_struck_off_step_locks_the_case_first(self, path, arrange, body):
        self._ready(**arrange)
        engine = {"status": "success", "new_status_id": 20, "new_approval_level": 2, "message": "m"}
        with patch.object(lal, "process_approval", return_value=engine), \
                patch.object(lal, "process_rejection", return_value=dict(engine, new_status_id=4)):
            resp = client.post(f"{PREFIX}/struck_off/{path}", json={"co_id": 1, "case_id": 1, **body})
        assert resp.status_code == 200, resp.text
        assert "FOR UPDATE" in self.db.calls[0][0] and self.db.calls[0][1] == {"case_id": 1}
        assert "due_band" in resp.json()["data"]["case"]

    def test_send_preconditions(self):
        self.put_case(stage_code="SHOW_CAUSE")
        self.set_letters(full_ladder())
        resp = client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 400 and "after the gate notice" in resp.json()["detail"]
        self.put_case(stage_code="GATE_NOTICE")
        self.set_letters([letter_row("GATE_NOTICE", date(2026, 10, 1), reply_due_date=date(2026, 10, 8))])
        resp = client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 400 and "sent date of the gate notice" in resp.json()["detail"]
        self.set_letters([letter_row("GATE_NOTICE", date(2026, 10, 4), date(2026, 10, 4),
                                     reply_due_date=date(2026, 10, 11))])
        resp = client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1})
        # sent 04.10 + 7: the window runs to 11.10 (today) -> sendable from 12.10
        assert resp.status_code == 400 and "can be sent from 12.10.2026" in resp.json()["detail"]
        self._ready()
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/struck_off/send", json={"co_id": 1, "case_id": 1}).status_code == 403
        assert self.db.commits == 0

    def test_approve_final_strikes_off_and_terminates(self):
        c = self._ready(status_id=20, approval_level=1)
        result = {"status": "success", "new_status_id": 3, "new_approval_level": 1, "message": "ok"}
        with patch.object(lal, "process_approval", return_value=result) as eng, \
                patch.object(lal, "apply_lifecycle_status") as life:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={
                "co_id": 1, "case_id": 1, "struck_off_date": "2026-10-10"})
        assert resp.status_code == 200, resp.text
        kw = eng.call_args.kwargs
        assert (kw["doc_id"], kw["user_id"], kw["menu_id"], kw["id_param_name"], kw["commit"]) == (
            1, 7, 77, "case_id", False)
        assert kw["get_doc_fn"] is lal.get_case_approval_doc_query
        assert kw["update_status_fn"] is lal.update_case_approval_status_query
        so = date(2026, 10, 10)
        assert (c.stage_code, c.status, c.exit_reason, c.exit_date, c.struck_off_date, c.stage_date) == (
            "STRUCK_OFF", 0, "STRUCK_OFF", so, so, so)
        assert c.next_due_date is None and c.status_id == 3
        life.assert_called_once_with(self.db, 3, 41, so, "Struck off from muster roll, long absence",
                                     "Case LA/2/26/0001", 7)
        assert self.events()[0].text == "Struck off approved; employee marked Terminated"
        assert self.db.commits == 1
        d = resp.json()["data"]
        assert d["approval"]["new_status_id"] == 3 and d["case"]["exit_reason"] == "STRUCK_OFF"

    def test_approve_intermediate_level(self):
        c = self._ready(status_id=20, approval_level=1)
        with patch.object(lal, "process_approval", return_value={
                "status": "success", "new_status_id": 20, "new_approval_level": 2, "message": "m"}), \
                patch.object(lal, "apply_lifecycle_status") as life:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200
        life.assert_not_called()
        assert (c.status, c.stage_code, c.status_id, c.approval_level) == (1, "GATE_NOTICE", 20, 2)
        assert self.events()[0].text == "Approved at level 1"

    def test_approve_guards(self):
        self._ready()
        with patch.object(lal, "process_approval") as eng:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
            assert resp.status_code == 400 and "not pending" in resp.json()["detail"]
            self._ready(status_id=20, approval_level=1)
            resp = client.post(f"{PREFIX}/struck_off/approve", json={
                "co_id": 1, "case_id": 1, "struck_off_date": "2026-10-07"})
            # window counts from the gate notice's SENT date (02.10 + 7 = runs to 09.10), not the
            # printed 08.10: struck off can be dated from 10.10
            assert resp.status_code == 400 and "dated from 10.10.2026" in resp.json()["detail"]
            resp = client.post(f"{PREFIX}/struck_off/approve", json={
                "co_id": 1, "case_id": 1, "struck_off_date": "2026-10-09"})      # the last day itself
            assert resp.status_code == 400 and "runs to 09.10.2026" in resp.json()["detail"]
            assert client.post(f"{PREFIX}/struck_off/approve", json={
                "co_id": 1, "case_id": 1, "struck_off_date": "2026-10-12"}).status_code == 400
            with patch.object(lac, "menu_id", return_value=None):
                resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
            assert resp.status_code == 400 and resp.json()["detail"] == "Long Absenteeism menu not found"
        eng.assert_not_called()

    def test_approve_refused_for_a_worker_who_came_back(self):
        c = self._ready(status_id=20, approval_level=1)
        self.db.on(REJOIN_TAG, rejoin_rows(date(2026, 10, 10)))
        with patch.object(lal, "process_approval") as eng, patch.object(lal, "apply_lifecycle_status") as life:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Worker attended on 10.10.2026 — exit the case as Rejoined instead"
        eng.assert_not_called()
        life.assert_not_called()
        assert (c.status, c.status_id) == (1, 20) and self.db.commits == 0

    @pytest.mark.parametrize("hr_status,label", [(39, "Resigned"), (41, "Terminated"), (42, "Retired"),
                                                  (46, "Blacklisted")])
    def test_approve_never_overwrites_an_hrms_separation(self, hr_status, label):
        c = self._ready(status_id=20, approval_level=1)
        self.db.on("la-emp-status", [{"status_id": hr_status}])
        result = {"status": "success", "new_status_id": 3, "new_approval_level": 1, "message": "ok"}
        with patch.object(lal, "process_approval", return_value=result), \
                patch.object(lal, "apply_lifecycle_status") as life:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 400
        assert resp.json()["detail"] == f"Already separated in HRMS (status {label}) — exit the case instead"
        life.assert_not_called()
        assert (c.status, c.stage_code, c.status_id, c.exit_reason) == (1, "GATE_NOTICE", 20, None)
        assert self.db.commits == 0 and self.db.rollbacks >= 1 and not self.events()
        assert self.db.params_of("la-emp-status") == [{"eb_id": 3}]

    def test_approve_on_a_working_employee_proceeds(self):
        self._ready(status_id=20, approval_level=1)
        self.db.on("la-emp-status", [{"status_id": 35}])                  # Joined
        result = {"status": "success", "new_status_id": 3, "new_approval_level": 1, "message": "ok"}
        with patch.object(lal, "process_approval", return_value=result), \
                patch.object(lal, "apply_lifecycle_status") as life:
            resp = client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        life.assert_called_once()
        assert resp.json()["data"]["case"]["due_band"] == "NONE"

    def test_engine_refusal_rolls_back(self):
        self._ready(status_id=20, approval_level=1)
        with patch.object(lal, "process_approval", side_effect=HTTPException(status_code=403, detail="no")), \
                patch.object(lal, "apply_lifecycle_status") as life:
            assert client.post(f"{PREFIX}/struck_off/approve", json={"co_id": 1, "case_id": 1}).status_code == 403
        life.assert_not_called()
        assert self.db.commits == 0 and self.db.rollbacks >= 1

    def test_reject(self):
        c = self._ready(status_id=20, approval_level=1)
        with patch.object(lal, "process_rejection", return_value={
                "status": "success", "new_status_id": 4, "new_approval_level": None, "message": "r"}) as eng:
            resp = client.post(f"{PREFIX}/struck_off/reject", json={
                "co_id": 1, "case_id": 1, "reason": "union says he is ill"})
        assert resp.status_code == 200, resp.text
        assert eng.call_args.kwargs["menu_id"] == 77 and eng.call_args.kwargs["reason"] == "union says he is ill"
        assert self.events()[0].text == "Struck-off rejected: union says he is ill"
        assert (c.status_id, c.stage_code, c.status) == (4, "GATE_NOTICE", 1)
        assert client.post(f"{PREFIX}/struck_off/reject", json={"co_id": 1, "case_id": 1}).status_code == 400

    def test_reopen(self):
        c = self._ready(status_id=4)
        resp = client.post(f"{PREFIX}/struck_off/reopen", json={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200 and c.status_id is None and c.approval_level is None
        assert self.events()[0].event_type == "APPROVAL"
        assert client.post(f"{PREFIX}/struck_off/reopen", json={"co_id": 1, "case_id": 1}).status_code == 400

    def test_permissions(self):
        self._ready()
        self.db.on("COUNT(*) as count", [{"count": 0}])
        engine = {"canApprove": True, "canReject": False, "canOpen": False, "canSendForApproval": False,
                  "canCancelDraft": False, "canReopen": False, "canViewApprovalLog": False,
                  "canClone": False, "canSave": True}
        with patch.object(lal, "calculate_approval_permissions", return_value=engine) as calc:
            resp = client.get(f"{PREFIX}/struck_off/permissions", params={"co_id": 1, "case_id": 1})
        assert resp.status_code == 200, resp.text
        assert calc.call_args[0][:5] == (7, 77, 2, 1, None)        # status NULL -> 1 for the engine
        d = resp.json()["data"]
        assert d["canApprove"] is False and d["canSave"] is False   # not sent yet: no direct approve
        assert d["can_send"] is True and d["canSendForApproval"] is True and d["send_blocked_reason"] is None
        assert (d["status_id"], d["hierarchy_exists"], d["menu_id"]) == (None, False, 77)

        self._ready(status_id=20, approval_level=1)
        self.db.on("COUNT(*) as count", [{"count": 2}])
        self.db.on("MAX(am.approval_level)", [{"max_level": 1}])
        self.level = lac.LEVEL_VIEW
        with patch.object(lal, "calculate_approval_permissions",
                          return_value=dict(engine, canReject=True)) as calc:
            d = client.get(f"{PREFIX}/struck_off/permissions", params={"co_id": 1, "case_id": 1}).json()["data"]
        assert calc.call_args[0][3:5] == (20, 1)
        assert d["canApprove"] is True and d["canReject"] is True
        assert d["can_send"] is False and "Already sent" in d["send_blocked_reason"]
        assert d["hierarchy_exists"] is True and d["max_approval_level"] == 1

    def test_permissions_rejoined_worker_cannot_be_sent(self):
        self._ready()
        self.db.on(REJOIN_TAG, rejoin_rows(date(2026, 10, 5)))
        with patch.object(lal, "calculate_approval_permissions", return_value={}):
            d = client.get(f"{PREFIX}/struck_off/permissions", params={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["can_send"] is False and d["canSendForApproval"] is False
        assert d["send_blocked_reason"] == "Worker attended on 05.10.2026 — exit the case as Rejoined instead"

    def test_permissions_rejected_case_can_reopen_with_edit(self):
        self._ready(status_id=4)
        with patch.object(lal, "calculate_approval_permissions", return_value={"canReopen": True}):
            d = client.get(f"{PREFIX}/struck_off/permissions", params={"co_id": 1, "case_id": 1}).json()["data"]
            assert d["canReopen"] is True and d["can_send"] is True
            self.level = lac.LEVEL_VIEW
            d = client.get(f"{PREFIX}/struck_off/permissions", params={"co_id": 1, "case_id": 1}).json()["data"]
        assert d["canReopen"] is False and d["can_send"] is False
        assert "edit access" in d["send_blocked_reason"]


class TestReports(Base):
    def test_struck_off_register(self):
        self.db.on("la-struck-off-register", [{"case_id": 1, "case_no": "A", "struck_off_date": date(2026, 10, 10),
                                               "warning1_date": date(2026, 9, 1), "remarks": "r"}])
        resp = client.get(f"{PREFIX}/report/struck_off_register", params={
            "co_id": 1, "branch_id": 2, "date_from": "2026-10-01", "date_to": "2026-10-31", "dept_id": 5})
        assert resp.status_code == 200
        assert resp.json()["data"][0]["warning1_date"] == "2026-09-01"
        assert self.db.params_of("la-struck-off-register") == [{
            "co_id": 1, "branch_id": 2, "date_from": date(2026, 10, 1), "date_to": date(2026, 10, 31),
            "dept_id": 5}]
        client.get(f"{PREFIX}/report/struck_off_register", params={"co_id": 1, "branch_id": 2})
        p = self.db.params_of("la-struck-off-register")[1]
        assert (p["date_from"], p["date_to"], p["dept_id"]) == (TODAY - timedelta(days=365), TODAY, None)

    def test_stage_summary(self):
        self.db.on("la-stage-summary-live", [{"dept_id": 5, "dept_desc": "SPINNING", "stage_code": "WARNING_2",
                                              "parked": 1, "n": 2}])
        self.db.on("la-stage-summary-exits", [{"exit_reason": "STRUCK_OFF", "n": 3}])
        resp = client.get(f"{PREFIX}/report/stage_summary", params={"co_id": 1, "branch_id": 2,
                                                                     "as_on": "2026-10-01"})
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["as_on"] == "2026-10-01" and d["exits_from"] == "2026-07-04"
        assert d["by_dept"][0]["counts"]["WARNING_2"] == 2 and d["exits"][0]["label"] == "Struck off"
        assert self.db.params_of("la-stage-summary-live") == [{"co_id": 1, "branch_id": 2}]
        assert self.db.params_of("la-stage-summary-exits") == [{
            "co_id": 1, "branch_id": 2, "date_from": date(2026, 7, 4), "as_on": date(2026, 10, 1)}]

    def test_monthly_trend(self):
        self.db.on("la-monthly-trend", [{"month": "2026-10", "kind": "rejoined", "n": 2}])
        resp = client.get(f"{PREFIX}/report/monthly_trend", params={"co_id": 1, "branch_id": 2, "months": 3})
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert [r["month"] for r in d] == ["2026-08", "2026-09", "2026-10"] and d[2]["rejoined"] == 2
        assert self.db.params_of("la-monthly-trend") == [{
            "co_id": 1, "branch_id": 2, "date_from": date(2026, 8, 1), "date_to": TODAY}]
        for bad in (0, 37, "x"):
            assert client.get(f"{PREFIX}/report/monthly_trend", params={
                "co_id": 1, "branch_id": 2, "months": bad}).status_code == 400

    def test_reports_need_view(self):
        self.level = 0
        for path in ("struck_off_register", "stage_summary", "monthly_trend"):
            assert client.get(f"{PREFIX}/report/{path}", params={"co_id": 1, "branch_id": 2}).status_code == 403


# =============================================================================
# Attachment gate (generic /api/attachments)
# =============================================================================


def _scope(**kw):
    m = {"entity_id": 40, "entity_active": 1, "case_id": 1, "co_id": 1, "branch_id": 2,
         "case_active": 1, "status": 1, "event_type": "REPLY"}
    m.update(kw)
    return [m]


class TestAttachmentGate:
    @pytest.mark.parametrize("module,action,level,expected", [
        ("jute_prod_absent_letter", "read", 1, None),
        ("jute_prod_absent_letter", "read", 0, 403),
        ("jute_prod_absent_letter", "upload", 3, 403),
        ("jute_prod_absent_letter", "upload", 4, None),
        ("jute_prod_absent_letter", "delete", 3, 403),
        ("jute_prod_absent_letter", "delete", 4, None),
        ("jute_prod_absent_receipt", "upload", 3, 403),
        ("jute_prod_absent_receipt", "upload", 4, None),
        ("jute_prod_absent_event", "upload", 2, 403),
        ("jute_prod_absent_event", "upload", 3, None),
        ("jute_prod_absent_event", "delete", 3, 403),
        ("jute_prod_absent_event", "read", 1, None),
    ])
    def test_levels(self, module, action, level, expected):
        db = FakeDB()
        db.on("la-scope", _scope())
        with patch.object(lac, "access_level", return_value=level) as acc:
            if expected is None:
                check_absent_attachment_access(db, {"user_id": 7}, 1, 40, action, module)
            else:
                with pytest.raises(HTTPException) as e:
                    check_absent_attachment_access(db, {"user_id": 7}, 1, 40, action, module)
                assert _status(e) == expected
        assert acc.call_args[0][1:] == (7, 2)                       # the case's branch
        tag = {"jute_prod_absent_letter": "la-scope-letter", "jute_prod_absent_receipt": "la-scope-receipt",
               "jute_prod_absent_event": "la-scope-event"}[module]
        assert db.params_of(tag) == [{"entity_id": 40}]

    @pytest.mark.parametrize("event_type", ["SYSTEM", "STAGE", "APPROVAL", "VERIFY", "PARK", None])
    def test_event_uploads_only_on_user_entries(self, event_type):
        db = FakeDB()
        db.on("la-scope", _scope(event_type=event_type))
        with patch.object(lac, "access_level", return_value=4):
            with pytest.raises(HTTPException) as e:
                check_absent_attachment_access(db, {"user_id": 7}, 1, 40, "upload", "jute_prod_absent_event")
            assert _status(e) == 400 and "remarks, feedback and replies" in e.value.detail
            # reading / removing what is already there stays possible
            check_absent_attachment_access(db, {"user_id": 7}, 1, 40, "read", "jute_prod_absent_event")
            check_absent_attachment_access(db, {"user_id": 7}, 1, 40, "delete", "jute_prod_absent_event")

    @pytest.mark.parametrize("event_type", ["REMARK", "FEEDBACK", "REPLY"])
    def test_event_uploads_on_user_entries(self, event_type):
        db = FakeDB()
        db.on("la-scope", _scope(event_type=event_type))
        with patch.object(lac, "access_level", return_value=3):
            check_absent_attachment_access(db, {"user_id": 7}, 1, 40, "upload", "jute_prod_absent_event")

    @pytest.mark.parametrize("scope", [[], _scope(co_id=9), _scope(entity_active=0), _scope(case_active=0)])
    def test_not_found(self, scope):
        db = FakeDB()
        db.on("la-scope", scope)
        with patch.object(lac, "access_level", return_value=4):
            with pytest.raises(HTTPException) as e:
                check_absent_attachment_access(db, {"user_id": 7}, 1, 40, "read", "jute_prod_absent_event")
        assert _status(e) == 404


class TestAttachmentRoutes(Base):
    def _upload(self, module="jute_prod_absent_letter", file_type="ad_card"):
        return client.post(
            f"{ATT}/upload?co_id=1",
            data={"module": module, "entity_id": "40", "file_type": file_type},
            files={"file": ("a.pdf", b"%PDF-1.4 ad card", "application/pdf")},
            headers={"host": "dev3.vowerp.co.in"},
        )

    def test_upload_letter_needs_edit(self):
        self.db.on("la-scope-letter", _scope())
        self.db.on("INSERT INTO file_attachment", Result([], lastrowid=77))
        self.level = lac.LEVEL_CREATE
        with patch.object(attachments_router_module.s3_client, "upload_bytes") as up:
            assert self._upload().status_code == 403
        up.assert_not_called()
        self.level = lac.LEVEL_EDIT
        with patch.object(attachments_router_module.s3_client, "upload_bytes") as up, \
                patch.object(attachments_router_module.s3_client, "get_bucket", return_value="b"):
            resp = self._upload()
        assert resp.status_code == 200, resp.text
        up.assert_called_once()
        assert resp.json()["data"]["attachment_id"] == 77

    def test_upload_event_with_create(self):
        self.db.on("la-scope-event", _scope())
        self.db.on("INSERT INTO file_attachment", Result([], lastrowid=78))
        self.level = lac.LEVEL_CREATE
        with patch.object(attachments_router_module.s3_client, "upload_bytes"), \
                patch.object(attachments_router_module.s3_client, "get_bucket", return_value="b"):
            assert self._upload("jute_prod_absent_event", "medical").status_code == 200

    def test_file_types_are_allow_listed(self):
        assert self._upload("jute_prod_absent_receipt", "ad_card").status_code == 400
        assert self._upload("jute_prod_absent_event", "notice").status_code == 400

    def test_list_needs_view_and_company(self):
        self.db.on("la-scope-receipt", _scope())
        self.level = 0
        assert client.get(ATT, params={"co_id": 1, "module": "jute_prod_absent_receipt",
                                       "entity_id": 40}).status_code == 403
        self.level = lac.LEVEL_VIEW
        assert client.get(ATT, params={"co_id": 1, "module": "jute_prod_absent_receipt",
                                       "entity_id": 40}).status_code == 200
        self.db.on("la-scope-receipt", _scope(co_id=9))
        assert client.get(ATT, params={"co_id": 1, "module": "jute_prod_absent_receipt",
                                       "entity_id": 40}).status_code == 404

    def test_delete_needs_edit(self):
        self.db.on("FROM file_attachment\n         WHERE attachment_id", [{
            "attachment_id": 77, "co_id": 1, "module": "jute_prod_absent_letter", "entity_id": 40,
            "file_type": "signed_copy", "s3_key": "k", "active": 1}])
        self.db.on("la-scope-letter", _scope())
        self.level = lac.LEVEL_CREATE
        assert client.delete(f"{ATT}/77?co_id=1").status_code == 403
        self.level = lac.LEVEL_EDIT
        assert client.delete(f"{ATT}/77?co_id=1").status_code == 200
        assert self.db.commits == 1


# =============================================================================
# HRMS lifecycle helper (extracted from /employee_status_update)
# =============================================================================


def _hrms_db(personal):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = personal
    return db


class TestLifecycleHelper:
    def test_terminate_deactivates_and_records_resign_details(self):
        personal = MagicMock(status_id=35, active=1)
        db = _hrms_db(personal)
        out = hrms_employee.apply_lifecycle_status(db, 3, 41, date(2026, 10, 10),
                                                   "Struck off from muster roll, long absence", "Case X", 7)
        assert out is personal and personal.status_id == 41 and personal.active == 0
        db.query.return_value.filter.return_value.update.assert_called_once_with({"active": 0})
        rec = db.add.call_args[0][0]
        assert (rec.eb_id, rec.type_of_resign, rec.resigned_date, rec.date_of_inactive) == (
            3, "41", date(2026, 10, 10), date(2026, 10, 10))
        assert (rec.resign_reasons, rec.resign_remarks, rec.updated_by, rec.active) == (
            "Struck off from muster roll, long absence", "Case X", 7, 1)
        db.commit.assert_not_called()

    def test_joined_keeps_active_and_no_resign_row(self):
        personal = MagicMock(status_id=21, active=1)
        db = _hrms_db(personal)
        hrms_employee.apply_lifecycle_status(db, 3, 35, None, None, None, 7)
        assert personal.status_id == 35 and personal.active == 1
        db.add.assert_not_called()

    def test_invalid_status_and_missing_employee(self):
        with pytest.raises(HTTPException) as e:
            hrms_employee.apply_lifecycle_status(_hrms_db(MagicMock()), 3, 99, None, None, None, 7)
        assert _status(e) == 400
        with pytest.raises(HTTPException) as e:
            hrms_employee.apply_lifecycle_status(_hrms_db(None), 3, 41, None, None, None, 7)
        assert _status(e) == 404


class TestEmployeeStatusEndpoint:
    def setup_method(self):
        self.personal = MagicMock(status_id=35, active=1)
        self.db = _hrms_db(self.personal)
        hrms_app.dependency_overrides[hrms_employee.get_tenant_db] = lambda: self.db
        hrms_app.dependency_overrides[hrms_employee.get_current_user_with_refresh] = lambda: {"user_id": 7}

    def teardown_method(self):
        hrms_app.dependency_overrides.clear()

    def test_behaviour_unchanged(self):
        resp = hrms_client.post("/api/hrms/employee_status_update", json={
            "eb_id": 3, "status_id": 39, "date": "2026-10-10", "reason": "personal", "remarks": "r"})
        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "Status updated successfully", "status_id": 39}}
        rec = self.db.add.call_args[0][0]
        assert (rec.resigned_date, rec.resign_reasons, rec.resign_remarks) == ("2026-10-10", "personal", "r")
        self.db.commit.assert_called_once()
        assert hrms_client.post("/api/hrms/employee_status_update", json={"eb_id": 3}).status_code == 400
        assert hrms_client.post("/api/hrms/employee_status_update", json={
            "eb_id": 3, "status_id": 2}).status_code == 400
        self.db.query.return_value.filter.return_value.first.return_value = None
        assert hrms_client.post("/api/hrms/employee_status_update", json={
            "eb_id": 3, "status_id": 41}).status_code == 404


# =============================================================================
# Owner decisions 2026-10-12: HRMS menu, bulk reprint, back-dated entries, PDF
# =============================================================================


class TestMenuPath:
    """The page moved to HRMS: menu_id() takes the active hrms/longAbsenteeism row,
    else the active legacy juteProduction/report/longAbsenteeism row (until the menu
    migration has run), so the access checks keep working across the flip."""

    def _db(self, rows):
        db = FakeDB()
        db.on("FROM menu_mst", rows)
        return db

    def test_paths(self):
        assert lac.MENU_PATH == "hrms/longAbsenteeism"
        assert lac.MENU_PATHS == ("hrms/longAbsenteeism", "juteProduction/report/longAbsenteeism")

    def test_hrms_row_wins_then_legacy_then_none(self):
        both = [{"menu_id": 40, "menu_path": "juteProduction/report/longAbsenteeism"},
                {"menu_id": 90, "menu_path": "hrms/longAbsenteeism"}]
        db = self._db(both)
        assert lac.menu_id(db) == 90
        assert db.params_of("FROM menu_mst") == [
            {"menu_path": "hrms/longAbsenteeism", "legacy_menu_path": "juteProduction/report/longAbsenteeism"}]
        assert lac.menu_id(self._db(both[:1])) == 40
        assert lac.menu_id(self._db([])) is None
        sql = " ".join(str(lac.get_menu_id_query()).split())
        assert "active = 1" in sql and "IN (:menu_path, :legacy_menu_path)" in sql

    def test_pick_menu_id(self):
        assert lac.pick_menu_id([(7, "/hrms/longAbsenteeism/"), (5, "hrms/longAbsenteeism")]) == 5
        assert lac.pick_menu_id([(7, "juteProduction/report/longAbsenteeism"), (None, "hrms/longAbsenteeism")]) == 7
        assert lac.pick_menu_id([(3, "hrms/other")]) is None

    def test_access_level_uses_the_resolved_menu(self):
        db = self._db([{"menu_id": 90, "menu_path": "hrms/longAbsenteeism"}])
        db.on("max_access_type_id", [{"max_access_type_id": 4}])
        with patch.object(lac, "get_user_edit_access", return_value="max_access_type_id /* edit */"):
            assert lac.access_level(db, 7, 2) == 4
        assert db.params_of("max_access_type_id") == [{"user_id": 7, "branch_id": 2, "menu_id": 90}]


def print_row(letter_id, branch_id=2, **kw) -> dict:
    """A get_letters_by_ids_query row: l.* + case co_id / branch_id / case_no / emp."""
    m = letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3), letter_id=letter_id,
                   subject=f"Subject {letter_id}", body=f"Dear RAM KUMAR,\nLetter {letter_id}\n\nPersonnel Manager")
    m.update(co_id=1, branch_id=branch_id, case_no="LA/2/26/0001", emp_code="10001", emp_name="RAM KUMAR")
    m.update(kw)
    return m


class TestParseLetterIds:
    def test_order_kept_and_repeats_dropped(self):
        assert lal.parse_letter_ids("3, 1,3,,2") == [3, 1, 2]
        assert lal.parse_letter_ids(" 7 ") == [7]

    @pytest.mark.parametrize("raw, msg", [
        (None, "letter_ids is required"), ("", "letter_ids is required"), (" , ", "letter_ids is required"),
        ("1,a", "Invalid letter_ids format"), ("0", "Invalid letter_ids format"), ("-2", "Invalid letter_ids format"),
        (",".join(str(i) for i in range(1, 202)), "At most 200 letters at a time"),
    ])
    def test_rejects(self, raw, msg):
        with pytest.raises(HTTPException) as e:
            lal.parse_letter_ids(raw)
        assert _status(e) == 400 and e.value.detail == msg

    def test_two_hundred_is_fine(self):
        assert len(lal.parse_letter_ids(",".join(str(i) for i in range(1, 201)))) == 200


class TestLettersPrint(Base):
    def setup_method(self):
        super().setup_method()
        self.branches_checked = []

        def level(db, uid, br):
            self.branches_checked.append(br)
            return self.level

        self.patches[0].stop()
        self.patches[0] = patch.object(lac, "access_level", side_effect=level)
        self.patches[0].start()

    def test_requested_order_and_shape(self):
        # the DB returns them in its own order; one is a letter recorded after the fact
        self.db.on("la-letters-by-ids", [print_row(5), print_row(9, recorded=1, sent_date=None)])
        resp = client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "9,5"})
        assert resp.status_code == 200, resp.text
        a, b = resp.json()["data"]
        assert (a["letter_id"], b["letter_id"]) == (9, 5)
        assert a == {
            "letter_id": 9, "case_id": 1, "branch_id": 2, "case_no": "LA/2/26/0001", "emp_code": "10001",
            "emp_name": "RAM KUMAR", "stage_code": "WARNING_1", "stage_label": "1st warning",
            "subject": "Subject 9", "body": "Dear RAM KUMAR,\nLetter 9\n\nPersonnel Manager",
            "issue_date": "2026-09-01", "sent_date": None, "recorded": 1,
        }
        assert b["recorded"] == 0 and b["sent_date"] == "2026-09-03"
        assert self.db.params_of("la-letters-by-ids") == [{"co_id": 1, "letter_ids": [9, 5]}]
        assert self.branches_checked == [2]                  # view checked once per branch

    def test_rows_without_the_recorded_column_still_print(self):
        """Before migration 20261012b the l.* row has no recorded key: 0."""
        row = print_row(5)
        row.pop("recorded", None)
        self.db.on("la-letters-by-ids", [row])
        resp = client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "5"})
        assert resp.status_code == 200 and resp.json()["data"][0]["recorded"] == 0

    def test_every_branch_is_checked(self):
        self.db.on("la-letters-by-ids", [print_row(5, branch_id=4), print_row(6, branch_id=2), print_row(7, branch_id=4)])
        assert client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "5,6,7"}).status_code == 200
        assert self.branches_checked == [2, 4]
        self.level = lac.LEVEL_VIEW - 1
        self.branches_checked.clear()
        resp = client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "5,6,7"})
        assert resp.status_code == 403 and "access to Long Absenteeism for this branch" in resp.json()["detail"]

    def test_unknown_inactive_or_other_company_letter_is_404(self):
        # the SQL keeps only active letters of active cases of co_id: a missing id is any of those
        self.db.on("la-letters-by-ids", [print_row(5)])
        resp = client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "5,8,12"})
        assert resp.status_code == 404 and resp.json()["detail"] == "Letter not found: 8, 12"
        assert self.branches_checked == []                   # 404 before any access answer
        sql = " ".join(str(lal.get_letters_by_ids_query()).split())
        assert "l.active = 1" in sql and "c.co_id = :co_id AND c.active = 1" in sql and "SELECT l.*" in sql

    def test_validation(self):
        assert client.get(f"{PREFIX}/letters_print", params={"co_id": 1}).status_code == 400
        assert client.get(f"{PREFIX}/letters_print", params={"letter_ids": "1"}).status_code == 400
        assert client.get(f"{PREFIX}/letters_print", params={"co_id": 1, "letter_ids": "x"}).status_code == 400
        assert not self.db.calls


def pdf_text(pdf: bytes) -> bytes:
    """The decoded page content of a reportlab PDF (ASCII85 + Flate streams)."""
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        data = m.group(1).strip()
        try:
            out += zlib.decompress(base64.a85decode(data, adobe=True))
        except Exception:
            try:
                out += zlib.decompress(data)
            except Exception:
                out += data
    return out


def page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf))


def png_data_url(w=300, h=100) -> str:
    from PIL import Image as PImage
    buf = BytesIO()
    PImage.new("RGB", (w, h), (120, 0, 0)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


PDF_COMPANY = dict(COMPANY, state_name="West Bengal", gst_no="19AAACE1234F1Z5", co_cin_no="U17110WB1950PLC000001",
                   co_logo=None)


class TestBuildLettersPdf:
    LETTER = {"subject": "NOTICE UNDER RULE 14(C)(V) OF STANDING ORDER FOR LONG ABSENCE",
              "body": "Date: 01.09.2026\nTo,\nRAM KUMAR\n\n    Indented line\nIt is noticed — absent.\n\n\n"
                      "Personnel Manager\nEMPIRE JUTE MILLS"}

    def test_one_page_per_letter_with_letterhead_subject_and_body(self):
        pdf = build_letters_pdf([self.LETTER, dict(self.LETTER, body="Second letter\nPersonnel Manager")], PDF_COMPANY)
        assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
        assert page_count(pdf) == 2
        txt = pdf_text(pdf)
        for needle in (b"EMPIRE JUTE MILLS", b"1 River Road, Titagarh, West Bengal - 700119",
                       b"GSTIN: 19AAACE1234F1Z5", b"CIN: U17110WB1950PLC000001",
                       b"NOTICE UNDER RULE 14\\(C\\)\\(V\\)", b"RAM KUMAR", b"Personnel Manager", b"Second letter",
                       b"Indented line"):
            assert needle in txt, needle
        assert b"/F" in txt and b"Times" in pdf               # Times-Roman / Times-Bold

    def test_a_long_body_flows_to_a_second_page(self):
        long_body = "\n".join(f"Line {i} of a very long letter body" for i in range(90))
        pdf = build_letters_pdf([{"subject": "S", "body": long_body}, self.LETTER], PDF_COMPANY)
        assert page_count(pdf) == 3
        assert b"Line 89 of a very long letter body" in pdf_text(pdf)

    def test_long_lines_wrap(self):
        line = "word " * 400
        pdf = build_letters_pdf([{"subject": None, "body": line}], {})
        assert page_count(pdf) == 1 and pdf_text(pdf).count(b"Tj") > 5

    def test_logo_from_a_data_url_else_skipped(self):
        with_logo = build_letters_pdf([self.LETTER], dict(PDF_COMPANY, co_logo=png_data_url()))
        assert b"/Subtype /Image" in with_logo
        for bad in (None, "", "not a data url", "data:image/png;base64,@@@", "data:image/png;base64,aGVsbG8="):
            pdf = build_letters_pdf([self.LETTER], dict(PDF_COMPANY, co_logo=bad))
            assert b"/Subtype /Image" not in pdf and page_count(pdf) == 1, bad

    def test_decode_logo(self):
        assert decode_logo(png_data_url())[:8] == b"\x89PNG\r\n\x1a\n"
        assert decode_logo("data:image/png;base64,aGVsbG8=") is None      # base64 of 'hello', not an image
        assert decode_logo(12) is None

    def test_letterhead_lines(self):
        assert letterhead_lines(PDF_COMPANY) == (
            "EMPIRE JUTE MILLS", "1 River Road, Titagarh, West Bengal - 700119",
            "GSTIN: 19AAACE1234F1Z5  |  CIN: U17110WB1950PLC000001")
        assert letterhead_lines({"co_name": "X", "co_zipcode": 0, "co_address1": " "}) == ("X", "", "")
        assert letterhead_lines(None) == ("", "", "")

    def test_no_letter(self):
        with pytest.raises(ValueError):
            build_letters_pdf([], PDF_COMPANY)

    def test_filename(self):
        assert lal.letters_pdf_filename(date(2026, 10, 12)) == "long-absenteeism-letters-20261012.pdf"


class TestLettersPdf(Base):
    def test_pdf_of_the_letters_in_the_requested_order(self):
        self.db.on("la-letters-by-ids", [print_row(5), print_row(9)])
        self.db.on("la-pdf-letterhead", [PDF_COMPANY])
        resp = client.get(f"{PREFIX}/letters_pdf", params={"co_id": 1, "letter_ids": "9,5"})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"] == 'attachment; filename="long-absenteeism-letters-20261011.pdf"'
        pdf = resp.content
        assert pdf.startswith(b"%PDF-") and page_count(pdf) == 2
        txt = pdf_text(pdf)
        assert txt.index(b"Letter 9") < txt.index(b"Letter 5")
        assert b"GSTIN: 19AAACE1234F1Z5" in txt
        assert self.db.params_of("la-pdf-letterhead") == [{"branch_id": 2}]

    def test_letters_of_several_branches_leave_the_branch_gstin_out(self):
        self.db.on("la-letters-by-ids", [print_row(5, branch_id=4), print_row(6, branch_id=2)])
        self.db.on("la-pdf-letterhead", [PDF_COMPANY])
        resp = client.get(f"{PREFIX}/letters_pdf", params={"co_id": 1, "letter_ids": "5,6"})
        assert resp.status_code == 200
        assert b"GSTIN" not in pdf_text(resp.content) and b"CIN: U17110WB1950PLC000001" in pdf_text(resp.content)
        assert self.db.params_of("la-pdf-letterhead") == [{"branch_id": 2}]

    def test_errors_are_json(self):
        self.db.on("la-letters-by-ids", [print_row(5)])
        resp = client.get(f"{PREFIX}/letters_pdf", params={"co_id": 1, "letter_ids": "5,6"})
        assert resp.status_code == 404 and resp.json()["detail"] == "Letter not found: 6"
        self.level = 0
        assert client.get(f"{PREFIX}/letters_pdf", params={"co_id": 1, "letter_ids": "5"}).status_code == 403
        assert client.get(f"{PREFIX}/letters_pdf", params={"co_id": 1}).status_code == 400

    def test_letterhead_query(self):
        sql = " ".join(str(lal.get_pdf_letterhead_query()).split())
        for col in ("cm.co_name", "cm.co_address1", "cm.co_address2", "cm.co_zipcode", "cm.co_cin_no",
                    "cm.co_logo", "sm.state AS state_name", "bm.gst_no"):
            assert col in sql


class TestLetterRecord(Base):
    """Back-dated entries (owner 2026-10-12): a letter issued outside VoWERP is
    entered as the case's NEXT stage — order enforced, no wait / sent-date gate."""

    def _body(self, **kw):
        b = {"co_id": 1, "case_id": 1, "stage_code": "WARNING_1", "issue_date": "2026-08-20"}
        b.update(kw)
        return b

    def test_happy_path(self):
        c = self.put_case(stage_code="VERIFIED", stage_date=date(2026, 10, 1), verified_date=date(2026, 10, 1))
        self.db.on("la-letterhead", [COMPANY])
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(
            sent_date="2026-08-22", dispatch_mode="hand", ad_no=" RA9 ", delivery_status="DELIVERED",
            delivery_date="2026-08-25", note="From the 2026 register"))
        assert resp.status_code == 200, resp.text
        assert "FOR UPDATE" in self.db.calls[0][0]           # the lock is the first statement
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert (letter.stage_code, letter.letter_no, letter.template_code) == ("WARNING_1", 1, "WARNING_1")
        assert (letter.recorded, letter.forced, letter.batch_ref) == (1, 0, None)
        assert (letter.issue_date, letter.sent_date, letter.delivery_date) == (
            date(2026, 8, 20), date(2026, 8, 22), date(2026, 8, 25))
        assert (letter.dispatch_mode, letter.ad_no, letter.delivery_status) == ("HAND", "RA9", "DELIVERED")
        assert letter.reply_due_date == date(2026, 8, 27)
        # the text is the template merged now, dated as the letter (before the verification: no gate)
        assert letter.subject == TEMPLATE_DEFAULTS["WARNING_1"]["subject"]
        assert letter.body.startswith("Date: 20.08.2026\nRef: LA/2/26/0001 / 1st warning letter\n")
        assert "absent from duty since 01.08.2026 (19 days as on 20.08.2026)" in letter.body
        assert "EMPIRE JUTE MILLS" in letter.body
        # the case advances exactly as /letter_issue does (the wait counts from the sent date)
        assert (c.stage_code, c.stage_date) == ("WARNING_1", date(2026, 8, 20))
        assert c.next_due_date == date(2026, 9, 2)           # sent 22.08 + 10 days -> from 02.09
        (ev,) = self.events()
        assert (ev.event_type, ev.event_date, ev.ref_letter_id) == ("STAGE", date(2026, 8, 20), letter.letter_id)
        assert ev.text == ("1st warning recorded as issued 20.08.2026, sent 22.08.2026 (back-dated entry): "
                           "From the 2026 register")
        assert self.db.commits == 1
        d = resp.json()["data"]
        assert d["letter"]["recorded"] == 1 and d["letter"]["sent_date"] == "2026-08-22"
        assert (d["case"]["stage_code"], d["case"]["next_stage_code"], d["case"]["due_band"]) == (
            "WARNING_1", "WARNING_2", "LETTER_DUE")

    def test_no_wait_and_no_sent_date_gate(self):
        """/letter_issue would refuse (1st warning not sent, wait not over): a record does not."""
        c = self.put_case(stage_code="WARNING_1", stage_date=date(2026, 10, 5))
        self.set_letters([letter_row("WARNING_1", date(2026, 10, 5))])
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(stage_code="WARNING_2", issue_date="2026-10-06"))
        assert resp.status_code == 200, resp.text
        (letter,) = self.db.added_of(JuteProdAbsentLetter)
        assert letter.sent_date is None and letter.forced == 0 and letter.recorded == 1
        assert c.stage_code == "WARNING_2" and c.next_due_date == date(2026, 10, 17)   # 06.10 + 10 + 1
        assert self.events()[0].text == "2nd warning recorded as issued 06.10.2026 (back-dated entry)"
        assert client.post(f"{PREFIX}/letter_issue", json={
            "co_id": 1, "case_id": 1, "stage_code": "WARNING_3"}).status_code == 400   # issue still gated

    @pytest.mark.parametrize("stage, case_stage, msg", [
        ("WARNING_2", "VERIFIED",
         "The next letter of this case is the 1st warning (the case is at Verified) — record the letters in order"),
        ("WARNING_1", "WARNING_1",
         "The next letter of this case is the 2nd warning (the case is at 1st warning) — record the letters in order"),
        ("SHOW_CAUSE", "GATE_NOTICE", "The gate notice is the last letter — the case goes for struck-off approval next"),
        ("WARNING_1", "DETECTED", "No letter can be recorded for this case (it is at the Detected stage)"),
    ])
    def test_order_is_enforced(self, stage, case_stage, msg):
        self.put_case(stage_code=case_stage)
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(stage_code=stage))
        assert resp.status_code == 400 and resp.json()["detail"] == msg
        assert not self.db.added and self.db.commits == 0

    def test_memo_is_never_recorded(self):
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(stage_code="STRUCK_OFF"))
        assert resp.status_code == 400 and "follows the struck-off approval" in resp.json()["detail"]
        assert not self.db.calls

    @pytest.mark.parametrize("status, msg", [(2, "parked"), (0, "closed")])
    def test_case_must_be_open(self, status, msg):
        self.put_case(status=status)
        resp = client.post(f"{PREFIX}/letter_record", json=self._body())
        assert resp.status_code == 400 and msg in resp.json()["detail"]

    def test_duplicate_stage_409(self):
        self.put_case(stage_code="VERIFIED")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1))])
        resp = client.post(f"{PREFIX}/letter_record", json=self._body())
        assert resp.status_code == 409 and "already been issued" in resp.json()["detail"]

    def test_dates_against_the_case(self):
        self.put_case(stage_code="WARNING_1")
        self.set_letters([letter_row("WARNING_1", date(2026, 9, 1), date(2026, 9, 3))])
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(stage_code="WARNING_2", issue_date="2026-08-31"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "issue_date cannot be before the previous letter's date (01.09.2026)"
        self.put_case(stage_code="VERIFIED")
        self.set_letters([])
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(issue_date="2026-07-31"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "issue_date cannot be before the last working date (01.08.2026)"
        assert not self.db.added

    @pytest.mark.parametrize("kw, msg", [
        ({"issue_date": None}, "issue_date is required"),
        ({"issue_date": "2026-10-12"}, "issue_date cannot be in the future"),
        ({"sent_date": "2026-08-19"}, "sent_date cannot be before the letter date (20.08.2026)"),
        ({"sent_date": "2026-10-12"}, "sent_date cannot be in the future"),
        ({"sent_date": "2026-08-22", "delivery_date": "2026-08-21"}, "delivery_date cannot be before sent_date"),
        ({"delivery_date": "2026-08-19"}, "delivery_date cannot be before the letter date (20.08.2026)"),
        ({"delivery_date": "2026-10-12"}, "delivery_date cannot be in the future"),
        ({"dispatch_mode": "PIGEON"}, "Invalid dispatch_mode: PIGEON"),
        ({"delivery_status": "LOST"}, "Invalid delivery_status: LOST"),
        ({"ad_no": "x" * 51}, "ad_no must be at most 50 characters"),
        ({"note": "x" * 501}, "note must be at most 500 characters"),
        ({"stage_code": "VERIFIED"}, "Invalid stage_code: VERIFIED"),
    ])
    def test_validation_before_db(self, kw, msg):
        resp = client.post(f"{PREFIX}/letter_record", json=self._body(**kw))
        assert resp.status_code == 400 and resp.json()["detail"] == msg
        assert not self.db.calls

    def test_access_and_company(self):
        self.put_case(co_id=9)
        assert client.post(f"{PREFIX}/letter_record", json=self._body()).status_code == 404
        self.put_case()
        self.level = lac.LEVEL_CREATE
        assert client.post(f"{PREFIX}/letter_record", json=self._body()).status_code == 403
        assert not self.db.added


class TestRecordedColumn:
    """jute_prod_absent_letter.recorded is deferred: the issue paths never name it (they
    run before migration 20261012b) and letter_to_dict reports it only when loaded."""

    def test_issue_never_sets_it(self):
        letter = lal._insert_letter(FakeDB(), make_case(), "WARNING_1", "S", "B", TODAY, None, RULE, 7, False, None)
        assert "recorded" not in sa_inspect(letter).dict
        assert "recorded" not in lac.letter_to_dict(letter)

    def test_deferred_and_reported_when_loaded(self):
        prop = sa_inspect(JuteProdAbsentLetter).column_attrs["recorded"]
        assert prop.deferred is True
        assert lac.letter_to_dict(JuteProdAbsentLetter(**letter_row("WARNING_1", TODAY, recorded=1)))["recorded"] == 1

    def test_letter_reads_use_every_column(self):
        sql = " ".join(str(lal.get_case_letters_query()).split())
        assert "SELECT l.*" in sql
