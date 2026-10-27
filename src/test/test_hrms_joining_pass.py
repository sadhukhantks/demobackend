"""Unit tests for the Joining Pass (src/hrms/joiningPass.py, owner request 2026-10-24).

Pure helpers (working-day counting, the pass criterion, the fortnight, the pass
number, the letter text) and the request validation that runs before any DB call.
Sample checked against sls on 2026-10-24: EB 18084 last seen Sat 03-Jan-2026,
joining 16-Jan-2026, Sunday 04-Jan a non-working day -> absent from 04-Jan (the
sample letter's date), 10 working days, pass required.
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.hrms import joiningPass as jp
from src.hrms.joiningPassQuery import get_absentees_query, get_off_days_query


def d(s: str) -> date:
    return date.fromisoformat(s)


SUNDAYS_JAN = {d("2026-01-04"), d("2026-01-11")}


# ─── working days and the absence ────────────────────────────────────


def test_working_days_skip_off_days():
    days = jp.working_days(d("2026-01-04"), d("2026-01-11"), SUNDAYS_JAN)
    assert days == [d("2026-01-05") + timedelta(days=i) for i in range(6)]


def test_working_days_empty_when_range_reversed():
    assert jp.working_days(d("2026-01-05"), d("2026-01-04"), set()) == []


def test_absence_of_sample_letter():
    ab = jp.absence_of(d("2026-01-03"), d("2026-01-16"), SUNDAYS_JAN)
    assert ab == {"absent_from": d("2026-01-04"), "absent_working_days": 10, "absent_calendar_days": 12}
    assert jp.pass_required(ab["absent_working_days"], 3)


def test_absence_excludes_the_joining_day_and_the_last_seen_day():
    ab = jp.absence_of(d("2026-01-05"), d("2026-01-06"), set())
    assert ab == {"absent_from": None, "absent_working_days": 0, "absent_calendar_days": 0}


def test_holidays_do_not_count_toward_the_threshold():
    # Last seen Thu 15-Oct-2026; 18 Sun, 19 non-working, 20 + 21 puja holidays.
    off = {d("2026-10-18"), d("2026-10-19"), d("2026-10-20"), d("2026-10-21")}
    ab = jp.absence_of(d("2026-10-15"), d("2026-10-22"), off)
    assert ab["absent_working_days"] == 2   # 16, 17
    assert not jp.pass_required(ab["absent_working_days"], 3)
    ab = jp.absence_of(d("2026-10-15"), d("2026-10-24"), off)
    assert ab["absent_working_days"] == 4   # 16, 17, 22, 23
    assert jp.pass_required(ab["absent_working_days"], 3)


@pytest.mark.parametrize("days,needed", [(0, False), (3, False), (4, True)])
def test_pass_required_is_more_than_threshold(days, needed):
    assert jp.pass_required(days, 3) is needed


# ─── fortnight, pass number, rule ────────────────────────────────────


def test_fortnight_window_ends_on_last_attendance():
    assert jp.fortnight_window(d("2026-01-03"), 14) == (d("2025-12-21"), d("2026-01-03"))


def test_perf_window_capped_to_department_window(monkeypatch):
    monkeypatch.setitem(jp.MAX_DAYS_BY_DEPT, "WEAVING", 7)
    assert jp.perf_window(d("2025-12-21"), d("2026-01-03"), "WEAVING") == (d("2025-12-28"), d("2026-01-03"))


def test_make_pass_no():
    assert jp.make_pass_no(29, d("2026-01-15"), 7) == "JP/29/26/0007"
    assert jp.make_pass_no(29, d("2026-01-15"), 12345) == "JP/29/26/12345"


def test_clean_rule_validates_ranges():
    assert jp.clean_rule({"min_absent_days": "3", "lookback_days": 60, "fortnight_days": 14}) == {
        "min_absent_days": 3, "lookback_days": 60, "fortnight_days": 14,
    }
    with pytest.raises(HTTPException) as exc:
        jp.clean_rule({"min_absent_days": 0, "lookback_days": 60, "fortnight_days": 14})
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        jp.clean_rule({"min_absent_days": 3, "lookback_days": 60})


# ─── letter ──────────────────────────────────────────────────────────


COMPANY = {
    "co_name": "THE EMPIRE JUTE CO LTD.",
    "co_address1": "21A, Shakespeare Sarani",
    "co_zipcode": 700017,
    "branch_address1": "15, B.T. Road, Talpukur, Kolkata-700123",
    "branch_address2": "",
    "branch_zipcode": 700123,
}


def pass_row(**over):
    row = {
        "pass_no": "JP/29/26/0001",
        "emp_code": "18084",
        "emp_name": "BISWAJIT  BERA",
        "dept_desc": "BEAMING",
        "absent_from": d("2026-01-04"),
        "join_date": d("2026-01-16"),
        "issue_date": d("2026-01-15"),
        "fn_efficiency": None,
        "fn_days_present": 9,
        "fn_working_days": 10,
    }
    row.update(over)
    return row


def test_letter_matches_sample_layout():
    letter = jp.letter_of(pass_row(), COMPANY)
    assert letter["company_name"] == "THE EMPIRE JUTE CO LTD."
    assert letter["company_address"] == "15, B.T. Road, Talpukur, Kolkata-700123"   # PIN not doubled
    assert letter["date"] == "16-Jan-2026"
    assert letter["issued_on"] == "15-Jan-2026"
    assert letter["title"] == "JOINING PASS FOR DEPARTMENT"
    p1 = letter["paragraphs"][0]
    assert "from 04-Jan-2026" in p1
    assert f"working is {jp.BLANK}." in p1           # no efficiency -> blank line
    assert "9 out of 10 working days" in p1
    assert p1.endswith("w.e.f. 16-Jan-2026.")
    assert "final chance" in letter["paragraphs"][1]


def test_letter_fills_efficiency_and_blanks_missing_counts():
    letter = jp.letter_of(pass_row(fn_efficiency=58.123, fn_days_present=None, fn_working_days=None), COMPANY)
    p1 = letter["paragraphs"][0]
    assert "is 58.12%." in p1
    assert f"{jp.BLANK} out of {jp.BLANK} working days" in p1


def test_letterhead_falls_back_to_company_address():
    addr = jp.letterhead_address({"co_address1": "21A, Shakespeare Sarani", "co_zipcode": 700017})
    assert addr == "21A, Shakespeare Sarani - 700017"


def test_letterhead_adds_branch_pin_when_missing():
    addr = jp.letterhead_address({"branch_address1": "15, B.T. Road", "branch_zipcode": 700123})
    assert addr == "15, B.T. Road - 700123"


def test_preview_row_uses_sub_department():
    row = jp.preview_row({"dept_desc": "WEAVING(EJM)", "sub_dept_desc": "WEAVING"}, d("2026-10-24"))
    assert row["dept_desc"] == "WEAVING" and row["pass_no"] is None


def test_pass_out_shapes_dates():
    out = jp.pass_out({"join_date": d("2026-01-16"), "fn_efficiency": None, "updated_date_time": None})
    assert out["join_date"] == "2026-01-16" and out["fn_efficiency"] is None


# ─── SQL ─────────────────────────────────────────────────────────────


def test_off_days_query_uses_approved_holidays_and_active_non_working_days():
    s = str(get_off_days_query())
    assert "h.status = 3" in s
    assert "n.is_active = 1" in s and "n.branch_id IS NULL OR n.branch_id = :branch_id" in s


def test_absentees_query_keeps_separated_workers_out_and_links_pass_by_absence():
    s = str(get_absentees_query())
    assert "pd.status_id NOT IN (" in s
    assert "j2.last_seen_date = seen.last_seen" in s
    assert "seen.last_seen < :before_date" in s


# ─── request validation (before any DB call) ─────────────────────────


def req(**params):
    return SimpleNamespace(query_params=params)


@pytest.mark.parametrize("missing", ["co_id", "branch_id"])
def test_check_requires_company_and_branch(missing):
    params = {"co_id": "2", "branch_id": "29", "emp_code": "18084"}
    params.pop(missing)
    with pytest.raises(HTTPException) as exc:
        jp.jp_check(request=req(**params), db=None, token_data={})
    assert exc.value.status_code == 400


def test_check_rejects_bad_join_date():
    with pytest.raises(HTTPException) as exc:
        jp.jp_check(request=req(co_id="2", branch_id="29", join_date="16/01/2026"), db=None, token_data={})
    assert exc.value.status_code == 400


def test_resolve_eb_needs_eb_or_code():
    with pytest.raises(HTTPException) as exc:
        jp._resolve_eb(None, 29, None, None)
    assert exc.value.status_code == 400
    assert jp._resolve_eb(None, 29, 17504, None) == 17504


def test_letter_authority_defaults_to_welfare_officer():
    assert jp.letter_of(pass_row(), COMPANY)["authorised_label"] == "Authorised by Welfare Officer"
    assert jp.letter_of(pass_row(authority="Personnel Manager"), COMPANY)["authorised_label"] == "Authorised by Personnel Manager"


def test_pass_out_absent_to_is_day_before_joining():
    out = jp.pass_out({"join_date": d("2026-01-16"), "absent_from": d("2026-01-04"), "fn_efficiency": None})
    assert out["absent_to"] == "2026-01-15"   # reference screen: 04-Jan-2026 .. 15-Jan-2026, 10 days


def test_issue_rejects_unknown_authority():
    body = {"co_id": 2, "branch_id": 29, "eb_id": 1, "join_date": "2026-01-16", "authority": "Gatekeeper"}
    request = SimpleNamespace()
    import src.hrms.joiningPass as mod
    orig = mod.json_object
    mod.json_object = lambda _r: body
    try:
        with pytest.raises(HTTPException) as exc:
            mod.jp_issue(request=request, db=None, token_data={})
    finally:
        mod.json_object = orig
    assert exc.value.status_code == 400


def test_off_days_query_without_non_working_table():
    s = str(get_off_days_query(False))
    assert "holiday_master" in s and "tbl_non_working_days" not in s
