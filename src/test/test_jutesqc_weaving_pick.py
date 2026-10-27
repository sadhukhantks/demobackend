"""Tests for ITM-013 — Weaving SQC picks validation (src/juteSQC/weaving_sqc.py,
prefix /api/juteSQC).

Covers the two new endpoints, the pure helpers and the "no prefill" contract:

  standards -> GET /weaving_sqc_pick_standards?co_id&entry_date
               {"data": {"entry_date": ..., "standards": [{weaving_quality_id,
                 std_picks_ppi, std_picks_dm, effective_date}]}}  (one execute)
  coverage  -> GET /weaving_pick_coverage?co_id&branch_id&date_from&date_to
               {"data": [rows], "meta": {..., "totals": {...}}}   (two executes)

Portal persona: DB + auth are mocked exactly as in src/test/test_weaving_sqc.py
(dependency_overrides keyed on get_tenant_db / get_current_user_with_refresh,
MagicMock session, rows with a plain-dict ``._mapping``, fetchall.side_effect in
execute order). The setup / save handlers are exercised only to prove the server
never hands the client a pick default and never substitutes a standard for the
typed value.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.juteSQC.weaving_sqc import (
    COVERAGE_ORDER_MSG,
    COVERAGE_RANGE_MSG,
    MAX_COVERAGE_DAYS,
    PICKS_DM_TO_PPI,
    build_pick_coverage,
    coverage_totals,
    get_current_user_with_refresh,
    get_tenant_db,
    picks_equal_to_std,
    resolve_std_picks_on,
    std_history_by_quality,
)

client = TestClient(app)

ENTRY_DATE = "2026-10-18"
DATE_FROM = "2026-10-01"
DATE_TO = "2026-10-15"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


# --- row factories -----------------------------------------------------------

def _std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10):
    """get_weaving_pick_std_history_query row."""
    return _mock_row(
        {
            "ref_id": ref_id,
            "value": value,
            "effective_date": effective_date,
            "weaving_target_map_id": map_id,
        }
    )


def _group_row(entry_date="2026-10-15", qid=5, code="Q5", name="Red",
               picks_2dp=12.37, n=1):
    """get_weaving_pick_coverage_groups_query row."""
    return _mock_row(
        {
            "entry_date": entry_date,
            "weaving_quality_id": qid,
            "weaving_quality_code": code,
            "weaving_quality_name": name,
            "picks_2dp": picks_2dp,
            "n": n,
        }
    )


def _quality_option_row():
    return _mock_row(
        {
            "weaving_quality_id": 5,
            "item_id": 10,
            "item_code": "JC",
            "item_name": "Jute Cloth",
            "weaving_quality_code": "Q5",
            "weaving_quality_name": "Red",
        }
    )


def _loom_option_row():
    return _mock_row(
        {
            "machine_id": 7,
            "machine_name": "Loom-1",
            "mech_code": "L01",
            "line_no": "1",
            "branch_id": 2,
        }
    )


# =============================================================================
# Pure helpers (no mocks)
# =============================================================================


class TestPureHelpers:

    def test_picks_equal_to_std_at_two_decimals(self):
        assert picks_equal_to_std(12.370, 12.37) is True
        # Stored picks are DECIMAL(10,3): the 3rd decimal is absorbed at 2 dp.
        assert picks_equal_to_std(12.374, 12.37) is True
        assert picks_equal_to_std(12.38, 12.37) is False
        # Decimal-ish inputs (what PyMySQL returns) behave the same.
        from decimal import Decimal
        assert picks_equal_to_std(Decimal("12.370"), Decimal("12.3700")) is True

    def test_picks_equal_to_std_without_standard(self):
        assert picks_equal_to_std(12.37, None) is False
        assert picks_equal_to_std(12.37, 0) is False
        assert picks_equal_to_std(None, 12.37) is False
        assert picks_equal_to_std(None, None) is False

    def test_resolve_std_picks_on_last_date(self):
        history = {
            5: [
                (date(2026, 10, 10), 13.0, 12),
                (date(2026, 9, 1), 12.37, 10),
            ]
        }
        assert resolve_std_picks_on(history, 5, date(2026, 9, 15)) == 12.37
        assert resolve_std_picks_on(history, 5, date(2026, 10, 10)) == 13.0
        assert resolve_std_picks_on(history, 5, "2026-10-11") == 13.0
        assert resolve_std_picks_on(history, 5, date(2026, 8, 1)) is None
        # Unknown quality -> None.
        assert resolve_std_picks_on(history, 99, date(2026, 10, 10)) is None
        # Latest row value 0 (or NULL) -> no standard, NO fallback to the older row.
        zero_latest = {5: [(date(2026, 10, 10), 0.0, 12), (date(2026, 9, 1), 12.37, 10)]}
        assert resolve_std_picks_on(zero_latest, 5, date(2026, 10, 12)) is None
        null_latest = {5: [(date(2026, 10, 10), None, 12), (date(2026, 9, 1), 12.37, 10)]}
        assert resolve_std_picks_on(null_latest, 5, date(2026, 10, 12)) is None
        # ...but before the zero row the older standard still applies.
        assert resolve_std_picks_on(zero_latest, 5, date(2026, 10, 1)) == 12.37

    def test_std_history_by_quality_sorts_desc(self):
        rows = [
            _std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10),
            _std_row(ref_id=6, value=8.53, effective_date=date(2026, 7, 1), map_id=11),
            _std_row(ref_id=5, value=13.0, effective_date=date(2026, 10, 10), map_id=12),
            # Same effective_date as id 12 -> newest id wins the tiebreak.
            _std_row(ref_id=5, value=13.5, effective_date="2026-10-10", map_id=14),
            _std_row(ref_id=5, value=None, effective_date="2026-08-01", map_id=9),
        ]
        history = std_history_by_quality(rows)
        assert set(history) == {5, 6}
        assert history[5] == [
            (date(2026, 10, 10), 13.5, 14),
            (date(2026, 10, 10), 13.0, 12),
            (date(2026, 9, 1), 12.37, 10),
            (date(2026, 8, 1), None, 9),
        ]
        assert history[6] == [(date(2026, 7, 1), 8.53, 11)]
        # Plain dict rows are accepted too (pure-function callers).
        plain = std_history_by_quality(
            [{"ref_id": 7, "value": 9.0, "effective_date": "2026-01-01",
              "weaving_target_map_id": 1}]
        )
        assert plain == {7: [(date(2026, 1, 1), 9.0, 1)]}

    def test_build_pick_coverage(self):
        groups = [
            _group_row(qid=5, picks_2dp=12.37, n=110),
            _group_row(qid=5, picks_2dp=12.5, n=4),
            _group_row(qid=6, code="Q6", name="Blue", picks_2dp=8.53, n=20),
        ]
        history = {5: [(date(2026, 9, 1), 12.37, 10)]}  # Q6 has no standard

        rows = build_pick_coverage(groups, history)

        assert len(rows) == 2
        q5 = next(r for r in rows if r["weaving_quality_id"] == 5)
        q6 = next(r for r in rows if r["weaving_quality_id"] == 6)
        assert q5 == {
            "date": "2026-10-15",
            "weaving_quality_id": 5,
            "weaving_quality_code": "Q5",
            "weaving_quality_name": "Red",
            "quality": "Q5 — Red",
            "readings": 114,
            "equal_to_std": 110,
            "measured_share_pct": 3.5,
            "std_picks_ppi": 12.37,
            "std_picks_dm": 48.7,
            "has_std": True,
        }
        assert q6["readings"] == 20
        assert q6["equal_to_std"] == 0
        assert q6["measured_share_pct"] is None   # never a fake 100
        assert q6["has_std"] is False
        assert q6["std_picks_ppi"] is None
        assert q6["std_picks_dm"] is None
        assert q6["quality"] == "Q6 — Blue"
        # Order: date asc, quality name, quality id -> Blue before Red.
        assert [r["weaving_quality_id"] for r in rows] == [6, 5]

    def test_build_pick_coverage_resolves_std_per_reading_date(self):
        """The standard is effective-dated: a reading before the std's
        effective_date has no standard, one after it does."""
        groups = [
            _group_row(entry_date="2026-10-05", qid=5, picks_2dp=12.37, n=3),
            _group_row(entry_date=date(2026, 10, 12), qid=5, picks_2dp=12.37, n=5),
        ]
        history = {5: [(date(2026, 10, 10), 12.37, 10)]}
        rows = build_pick_coverage(groups, history)
        assert [r["date"] for r in rows] == ["2026-10-05", "2026-10-12"]
        assert rows[0]["has_std"] is False and rows[0]["equal_to_std"] == 0
        assert rows[1]["has_std"] is True and rows[1]["equal_to_std"] == 5
        assert rows[1]["measured_share_pct"] == 0.0

    def test_build_pick_coverage_quality_label_fallback(self):
        rows = build_pick_coverage(
            [_group_row(qid=9, code=None, name=None, picks_2dp=10.0, n=1)], {}
        )
        assert rows[0]["quality"] == "Quality #9"
        assert build_pick_coverage([], {}) == []

    def test_coverage_totals(self):
        groups = [
            _group_row(qid=5, picks_2dp=12.37, n=110),
            _group_row(qid=5, picks_2dp=12.5, n=4),
            _group_row(qid=6, code="Q6", name="Blue", picks_2dp=8.53, n=20),
        ]
        rows = build_pick_coverage(groups, {5: [(date(2026, 9, 1), 12.37, 10)]})
        assert coverage_totals(rows) == {
            "readings": 134,
            "readings_with_std": 114,
            "equal_to_std": 110,
            "measured_share_pct": 3.5,
            "days": 1,
            "qualities": 2,
        }
        assert coverage_totals([]) == {
            "readings": 0,
            "readings_with_std": 0,
            "equal_to_std": 0,
            "measured_share_pct": None,
            "days": 0,
            "qualities": 0,
        }


# =============================================================================
# GET /weaving_pick_coverage
# =============================================================================


class TestWeavingPickCoverage:

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    URL = "/api/juteSQC/weaving_pick_coverage"

    def test_coverage_success(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [
                _group_row(qid=5, picks_2dp=12.37, n=110),
                _group_row(qid=5, picks_2dp=12.5, n=4),
                _group_row(qid=6, code="Q6", name="Blue", picks_2dp=8.53, n=20),
            ],
            [_std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10)],
        ]

        response = client.get(
            f"{self.URL}?co_id=1&branch_id=2&date_from={DATE_FROM}&date_to={DATE_TO}"
        )

        assert response.status_code == 200
        body = response.json()
        rows = body["data"]
        assert len(rows) == 2
        q5 = next(r for r in rows if r["weaving_quality_id"] == 5)
        assert q5["date"] == "2026-10-15"
        assert q5["quality"] == "Q5 — Red"
        assert q5["readings"] == 114
        assert q5["equal_to_std"] == 110
        assert q5["measured_share_pct"] == 3.5
        assert q5["std_picks_ppi"] == 12.37
        assert q5["std_picks_dm"] == 48.7
        assert q5["has_std"] is True
        q6 = next(r for r in rows if r["weaving_quality_id"] == 6)
        assert q6["has_std"] is False
        assert q6["measured_share_pct"] is None
        meta = body["meta"]
        assert meta["co_id"] == 1
        assert meta["branch_id"] == 2
        assert meta["date_from"] == DATE_FROM
        assert meta["date_to"] == DATE_TO
        assert meta["max_days"] == MAX_COVERAGE_DAYS
        assert "value_role=standard" in meta["std_rule"]
        assert meta["totals"] == {
            "readings": 134,
            "readings_with_std": 114,
            "equal_to_std": 110,
            "measured_share_pct": 3.5,
            "days": 1,
            "qualities": 2,
        }

        # Exactly two executes: groups, then the std history up to date_to.
        assert self._mock_session.execute.call_count == 2
        groups_sql, groups_binds = self._mock_session.execute.call_args_list[0][0]
        assert groups_binds == {
            "co_id": 1,
            "branch_id": 2,
            "date_from": date.fromisoformat(DATE_FROM),
            "date_to": date.fromisoformat(DATE_TO),
        }
        assert "jute_sqc_weaving_pick" in str(groups_sql)
        assert "picks IS NOT NULL" in str(groups_sql)
        assert "vw_weaving_pick_act" not in str(groups_sql)
        std_sql, std_binds = self._mock_session.execute.call_args_list[1][0]
        assert "jute_prod_weaving_target_map" in str(std_sql)
        assert std_binds["co_id"] == 1
        assert std_binds["id_type"] == "qid"
        assert std_binds["value_role"] == "standard"
        assert std_binds["param"] == "picks"
        assert std_binds["on_date"] == date.fromisoformat(DATE_TO)
        assert "branch_id" not in std_binds  # the standard is company-wide

    def test_coverage_without_branch(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [[], []]
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}&date_to={DATE_TO}")
        assert response.status_code == 200
        assert response.json()["data"] == []
        assert response.json()["meta"]["branch_id"] is None
        assert self._mock_session.execute.call_args_list[0][0][1]["branch_id"] is None

    def test_coverage_missing_co_id(self):
        response = client.get(f"{self.URL}?date_from={DATE_FROM}&date_to={DATE_TO}")
        assert response.status_code == 400
        assert response.json()["detail"] == "co_id is required"
        self._mock_session.execute.assert_not_called()

    def test_coverage_invalid_co_id(self):
        response = client.get(f"{self.URL}?co_id=x&date_from={DATE_FROM}&date_to={DATE_TO}")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid co_id"

    def test_coverage_invalid_branch_id(self):
        response = client.get(
            f"{self.URL}?co_id=1&branch_id=abc&date_from={DATE_FROM}&date_to={DATE_TO}"
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid branch_id"

    def test_coverage_missing_date_from(self):
        response = client.get(f"{self.URL}?co_id=1&date_to={DATE_TO}")
        assert response.status_code == 400
        assert response.json()["detail"] == "date_from is required"

    def test_coverage_missing_date_to(self):
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}")
        assert response.status_code == 400
        assert response.json()["detail"] == "date_to is required"

    def test_coverage_invalid_date(self):
        response = client.get(f"{self.URL}?co_id=1&date_from=15-10-2026&date_to={DATE_TO}")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid date_from format"
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}&date_to=nope")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid date_to format"
        self._mock_session.execute.assert_not_called()

    def test_coverage_reversed_dates(self):
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_TO}&date_to={DATE_FROM}")
        assert response.status_code == 400
        assert response.json()["detail"] == COVERAGE_ORDER_MSG
        assert response.json()["detail"] == "date_to is before date_from"
        self._mock_session.execute.assert_not_called()

    def test_coverage_range_too_large(self):
        start = date.fromisoformat(DATE_FROM)
        too_far = (start + timedelta(days=MAX_COVERAGE_DAYS)).isoformat()  # 94 days
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}&date_to={too_far}")
        assert response.status_code == 400
        assert response.json()["detail"] == COVERAGE_RANGE_MSG
        assert response.json()["detail"] == "Date range too large — at most 93 days"
        self._mock_session.execute.assert_not_called()

        # Exactly 93 days (inclusive) is allowed.
        self._mock_session.execute.return_value.fetchall.side_effect = [[], []]
        at_cap = (start + timedelta(days=MAX_COVERAGE_DAYS - 1)).isoformat()
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}&date_to={at_cap}")
        assert response.status_code == 200
        assert self._mock_session.execute.call_count == 2

    def test_coverage_single_day_range(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [[], []]
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_TO}&date_to={DATE_TO}")
        assert response.status_code == 200
        assert response.json()["meta"]["totals"]["days"] == 0

    def test_coverage_db_error(self):
        self._mock_session.execute.side_effect = Exception("DB connection failed")
        response = client.get(f"{self.URL}?co_id=1&date_from={DATE_FROM}&date_to={DATE_TO}")
        assert response.status_code == 500


# =============================================================================
# GET /weaving_sqc_pick_standards
# =============================================================================


class TestWeavingSqcPickStandards:

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    URL = "/api/juteSQC/weaving_sqc_pick_standards"

    def test_standards_success(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10)],
        ]

        response = client.get(f"{self.URL}?co_id=1&entry_date={ENTRY_DATE}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["entry_date"] == ENTRY_DATE
        assert data["standards"] == [
            {
                "weaving_quality_id": 5,
                "std_picks_ppi": 12.37,
                "std_picks_dm": 48.7,
                "effective_date": "2026-09-01",
            }
        ]
        # Nothing but the standard — no pick default, no readings.
        assert set(data) == {"entry_date", "standards"}
        assert self._mock_session.execute.call_count == 1
        sql, binds = self._mock_session.execute.call_args_list[0][0]
        assert "jute_prod_weaving_target_map" in str(sql)
        assert binds == {
            "co_id": 1,
            "id_type": "qid",
            "value_role": "standard",
            "param": "picks",
            "on_date": date.fromisoformat(ENTRY_DATE),
        }

    def test_standards_latest_row_wins_and_sorted(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [
                _std_row(ref_id=6, value=8.53, effective_date="2026-07-01", map_id=11),
                _std_row(ref_id=5, value=13.0, effective_date="2026-10-10", map_id=12),
                _std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10),
            ],
        ]
        response = client.get(f"{self.URL}?co_id=1&entry_date={ENTRY_DATE}")
        assert response.status_code == 200
        standards = response.json()["data"]["standards"]
        assert [s["weaving_quality_id"] for s in standards] == [5, 6]
        assert standards[0]["std_picks_ppi"] == 13.0
        assert standards[0]["effective_date"] == "2026-10-10"
        assert standards[0]["std_picks_dm"] == round(13.0 / PICKS_DM_TO_PPI, 2)
        assert standards[1]["std_picks_ppi"] == 8.53

    def test_standards_zero_value_omitted(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [
                # Latest row for Q5 is 0 -> omitted, no fallback to the 12.37 row.
                _std_row(ref_id=5, value=0, effective_date="2026-10-10", map_id=12),
                _std_row(ref_id=5, value=12.37, effective_date="2026-09-01", map_id=10),
                _std_row(ref_id=6, value=None, effective_date="2026-07-01", map_id=11),
                _std_row(ref_id=7, value=9.0, effective_date="2026-07-01", map_id=13),
            ],
        ]
        response = client.get(f"{self.URL}?co_id=1&entry_date={ENTRY_DATE}")
        assert response.status_code == 200
        standards = response.json()["data"]["standards"]
        assert [s["weaving_quality_id"] for s in standards] == [7]

    def test_standards_empty(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [[]]
        response = client.get(f"{self.URL}?co_id=1&entry_date={ENTRY_DATE}")
        assert response.status_code == 200
        assert response.json()["data"] == {"entry_date": ENTRY_DATE, "standards": []}

    def test_standards_missing_co_id(self):
        response = client.get(f"{self.URL}?entry_date={ENTRY_DATE}")
        assert response.status_code == 400
        assert response.json()["detail"] == "co_id is required"

    def test_standards_missing_entry_date(self):
        response = client.get(f"{self.URL}?co_id=1")
        assert response.status_code == 400
        assert response.json()["detail"] == "entry_date is required"

    def test_standards_invalid_entry_date(self):
        response = client.get(f"{self.URL}?co_id=1&entry_date=18/10/2026")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid entry_date format"
        self._mock_session.execute.assert_not_called()

    def test_standards_db_error(self):
        self._mock_session.execute.side_effect = Exception("DB connection failed")
        response = client.get(f"{self.URL}?co_id=1&entry_date={ENTRY_DATE}")
        assert response.status_code == 500


# =============================================================================
# No-prefill contract (existing setup / save handlers, unchanged)
# =============================================================================


class TestNoPrefillContract:

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_setup_payload_has_no_pick_default(self):
        # qualities, looms, readings, summary -> the 4 fetchall calls, unchanged.
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_quality_option_row()],
            [_loom_option_row()],
            [],
            [],
        ]
        response = client.get(
            f"/api/juteSQC/weaving_sqc_pick_setup?co_id=1&entry_date={ENTRY_DATE}"
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert "standards" not in data
        assert set(data) == {"qualities", "looms", "readings", "summary"}
        forbidden = {"picks", "picks_dm", "std_picks", "std_picks_ppi",
                     "default_picks", "is_default"}
        assert data["qualities"]
        for q in data["qualities"]:
            assert not (forbidden & set(q)), q
        # No target-map query sneaked into setup.
        assert self._mock_session.execute.call_count == 4
        for c in self._mock_session.execute.call_args_list:
            assert "jute_prod_weaving_target_map" not in str(c[0][0])

    def test_save_stores_typed_value_not_standard(self):
        payload = {
            "co_id": 1,
            "branch_id": 2,
            "entry_date": ENTRY_DATE,
            "entries": [
                {"weaving_quality_id": 5, "machine_id": 7, "width": 48.0, "picks_dm": 48.7},
            ],
        }
        response = client.post("/api/juteSQC/weaving_sqc_pick_save", json=payload)
        assert response.status_code == 200
        assert response.json()["data"]["saved"] == 1
        # Exactly insert + stale flag: the server never looks the standard up,
        # never substitutes it and never flags the row.
        assert self._mock_session.execute.call_count == 2
        insert_sql, insert_binds = self._mock_session.execute.call_args_list[0][0]
        assert "INSERT INTO jute_sqc_weaving_pick" in str(insert_sql)
        assert insert_binds["picks"] == round(48.7 * PICKS_DM_TO_PPI, 3) == 12.37
        assert insert_binds["picks_dm"] == 48.7
        assert "is_default" not in insert_binds
        for c in self._mock_session.execute.call_args_list:
            assert "jute_prod_weaving_target_map" not in str(c[0][0])
