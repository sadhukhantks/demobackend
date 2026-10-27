"""Tests for the HRMS Period Master (period types, slots, generated periods)."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.periodMaster import (
    build_month_periods,
    build_week_periods,
    parse_type_body,
    slot_dates,
    slot_summary,
    validate_slots,
)
from src.main import app

client = TestClient(app)
PREFIX = "/api/hrmsMasters"


def _slot(no, code, frm, to, prev=0, name=None):
    return {"slot_no": no, "slot_name": name or code, "slot_code": code,
            "from_day": frm, "from_prev_month": prev, "to_day": to}


FORTNIGHT = [_slot(1, "F1", 1, 15, name="1st Fortnight"), _slot(2, "F2", 16, None, name="2nd Fortnight")]


# ─── Slot validation ────────────────────────────────────────────────


class TestValidateSlots:
    def test_fortnight_ok(self):
        assert [s["slot_no"] for s in validate_slots([dict(s) for s in FORTNIGHT])] == [1, 2]

    def test_month_ok(self):
        validate_slots([_slot(1, "M", 1, None)])

    def test_prev_month_cycle_ok(self):
        validate_slots([_slot(1, "M", 26, 25, prev=1)])

    def test_custom_three_way_ok(self):
        validate_slots([_slot(1, "A", 1, 10), _slot(2, "B", 11, 20), _slot(3, "C", 21, None)])

    @pytest.mark.parametrize("slots, msg", [
        ([], "at least one"),
        ([_slot(1, "F1", 2, None)], "must start on day 1"),
        ([_slot(1, "F1", 1, 15), _slot(2, "F2", 17, None)], "must start on day 16"),
        ([_slot(1, "F1", 1, 15), _slot(2, "F2", 16, 30)], "month end"),
        ([_slot(1, "F1", 1, None), _slot(2, "F2", 16, None)], "only the last slot"),
        ([_slot(1, "A", 1, 28), _slot(2, "B", 29, None)], "by day 27"),
        ([_slot(1, "M", 26, 24, prev=1)], "must end on day 25"),
        ([_slot(1, "M", 30, 29, prev=1)], "day 2 to 28"),
        ([_slot(1, "A", 1, 15), _slot(2, "A", 16, None)], "used twice"),
        ([_slot(1, "A", 1, 15), _slot(2, "B", 16, None, prev=1)], "only the first slot"),
    ])
    def test_rejects(self, slots, msg):
        with pytest.raises(ValueError, match=msg):
            validate_slots(slots)


# ─── Date generation ────────────────────────────────────────────────


class TestDates:
    def test_second_fortnight_ends_on_month_end(self):
        assert slot_dates(FORTNIGHT[1], 2026, 2) == (date(2026, 2, 16), date(2026, 2, 28))
        assert slot_dates(FORTNIGHT[1], 2028, 2) == (date(2028, 2, 16), date(2028, 2, 29))
        assert slot_dates(FORTNIGHT[1], 2026, 10) == (date(2026, 10, 16), date(2026, 10, 31))

    def test_prev_month_start_wraps_year(self):
        assert slot_dates(_slot(1, "M", 26, 25, prev=1), 2026, 1) == (date(2025, 12, 26), date(2026, 1, 25))

    def test_fortnight_year_is_contiguous(self):
        periods = build_month_periods(FORTNIGHT, 2026)
        assert len(periods) == 24
        assert periods[0]["from_date"] == date(2026, 1, 1)
        assert periods[-1]["to_date"] == date(2026, 12, 31)
        for a, b in zip(periods, periods[1:]):
            assert (b["from_date"] - a["to_date"]).days == 1
        assert periods[19]["period_code"] == "202610-F2"
        assert periods[19]["period_name"] == "Oct-2026 2nd Fortnight"

    def test_weeks_start_on_chosen_day(self):
        weeks = build_week_periods(7, 2026)  # Sunday
        assert weeks[0]["from_date"] == date(2026, 1, 4)
        assert all(w["from_date"].isoweekday() == 7 for w in weeks)
        assert all((w["to_date"] - w["from_date"]).days == 6 for w in weeks)
        assert weeks[-1]["from_date"].year == 2026
        assert weeks[0]["period_code"] == "2026-W01"
        assert len(weeks) == 52

    def test_summary(self):
        assert slot_summary("FORTNIGHTLY", None, FORTNIGHT) == "F1 1–15, F2 16–EOM"
        assert slot_summary("WEEKLY", 1, []) == "Mon–Sun"
        assert slot_summary("WEEKLY", 7, []) == "Sun–Sat"


# ─── Body parsing ───────────────────────────────────────────────────


class TestParseTypeBody:
    def test_fortnight_body(self):
        v = parse_type_body({"period_type_name": " Wages FN ", "frequency": "fortnightly",
                             "slots": [dict(s) for s in FORTNIGHT], "active": "1"})
        assert v["period_type_name"] == "Wages FN"
        assert v["frequency"] == "FORTNIGHTLY"
        assert len(v["slots"]) == 2 and v["week_start_day"] is None

    def test_weekly_ignores_slots(self):
        v = parse_type_body({"period_type_name": "Week", "frequency": "WEEKLY", "week_start_day": 7,
                             "slots": [dict(s) for s in FORTNIGHT]})
        assert v["slots"] == [] and v["week_start_day"] == 7

    @pytest.mark.parametrize("body, msg", [
        ({"frequency": "MONTHLY"}, "Name is required"),
        ({"period_type_name": "X", "frequency": "DAILY"}, "Frequency"),
        ({"period_type_name": "X", "frequency": "WEEKLY"}, "week starts"),
        ({"period_type_name": "X", "frequency": "FORTNIGHTLY", "slots": [_slot(1, "M", 1, None)]}, "exactly 2"),
        ({"period_type_name": "X", "frequency": "CUSTOM", "slots": [_slot(1, "M", 1, 20)]}, "month end"),
    ])
    def test_rejects(self, body, msg):
        with pytest.raises(HTTPException) as e:
            parse_type_body(body)
        assert e.value.status_code == 400 and msg in e.value.detail


# ─── Endpoints (DB mocked) ──────────────────────────────────────────


def _row(**kw):
    r = MagicMock()
    r._mapping = kw
    for k, v in kw.items():
        setattr(r, k, v)
    return r


@pytest.fixture
def db():
    mock = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: mock
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield mock
    app.dependency_overrides.clear()


class TestEndpoints:
    def test_create_requires_branch(self, db):
        r = client.post(f"{PREFIX}/period_type_create", json={"co_id": 1, "period_type_name": "X", "frequency": "MONTHLY"})
        assert r.status_code == 400 and "branch" in r.json()["detail"]

    def test_create_inserts_type_and_slots(self, db):
        dup = _row(cnt=0)
        ins = MagicMock(lastrowid=11)
        db.execute.side_effect = [MagicMock(fetchone=lambda: dup), ins, MagicMock(), MagicMock()]
        r = client.post(f"{PREFIX}/period_type_create", json={
            "co_id": 1, "branch_id": 2, "period_type_name": "Wages FN",
            "frequency": "FORTNIGHTLY", "slots": FORTNIGHT,
        })
        assert r.status_code == 200, r.text
        assert r.json()["period_type_id"] == 11
        assert db.execute.call_count == 4  # dup check, type, 2 slots
        db.commit.assert_called_once()

    def test_edit_locked_rule_rejected(self, db):
        ptype = _row(period_type_id=5, co_id=1, branch_id=2, frequency="FORTNIGHTLY",
                     week_start_day=None, period_count=24)
        slots = [_row(period_type_slot_id=i, period_type_id=5, **s) for i, s in enumerate(FORTNIGHT, 1)]
        db.execute.side_effect = [
            MagicMock(fetchone=lambda: ptype),
            MagicMock(fetchone=lambda: _row(cnt=0)),
            MagicMock(fetchall=lambda: slots),
        ]
        r = client.put(f"{PREFIX}/period_type_edit/5", json={
            "period_type_name": "Wages FN", "frequency": "CUSTOM",
            "slots": [_slot(1, "A", 1, 10), _slot(2, "B", 11, None)],
        })
        assert r.status_code == 400 and "locked" in r.json()["detail"]

    def test_generate_skips_existing_and_reports_overlap(self, db):
        ptype = _row(period_type_id=5, co_id=1, branch_id=2, frequency="MONTHLY",
                     week_start_day=None, period_count=2, active=1)
        slots = [_row(period_type_slot_id=9, period_type_id=5, **_slot(1, "M", 1, None, name="Month"))]
        existing = [
            _row(period_code="202601-M", from_date=date(2026, 1, 1), to_date=date(2026, 1, 31)),
            # hand-edited February that runs into March
            _row(period_code="202602-M", from_date=date(2026, 2, 2), to_date=date(2026, 3, 3)),
        ]
        db.execute.side_effect = [
            MagicMock(fetchone=lambda: ptype),
            MagicMock(fetchall=lambda: slots),
            MagicMock(fetchall=lambda: existing),
        ] + [MagicMock()] * 12
        r = client.post(f"{PREFIX}/period_generate", json={"period_type_id": 5, "year": 2026})
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["skipped"] == 1
        assert body["created"] == 9
        assert body["conflicts"] == ["202602-M overlaps 202602-M", "202603-M overlaps 202602-M"]

    def test_period_edit_rejects_reversed_dates(self, db):
        r = client.put(f"{PREFIX}/period_edit/3", json={
            "period_name": "X", "from_date": "2026-10-15", "to_date": "2026-10-01",
        })
        assert r.status_code == 400 and "before" in r.json()["detail"]

    def test_delete_missing(self, db):
        db.execute.return_value = MagicMock(rowcount=0)
        assert client.delete(f"{PREFIX}/period_delete/99").status_code == 404
