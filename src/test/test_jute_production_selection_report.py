"""Tests for the Selection vs Jute Issue report (src/juteProduction/selection_reports.py,
GET /api/juteProductionReports/selection_vs_issue) — spec
../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md §3.3.

Pure-builder tests cover the (date, item) merge: both-sided, issue-only and
selection-only cells, diff_pct None on zero issue, ordering, totals, and the
by_gate_entry section. Endpoint tests cover parameter validation and one happy path
through TestClient with the DB and auth dependencies overridden by a MagicMock
session — no database.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.selection_reports import (
    BAD_BRANCH_MSG,
    MAX_RANGE_DAYS,
    build_by_gate_entry,
    build_selection_vs_issue,
    router as selection_reports_router,
)
from src.main import app

PREFIX = "/api/juteProductionReports"
URL = f"{PREFIX}/selection_vs_issue"

# The router is registered in src/main.py under the shared jute-production-reports
# prefix; mount it here (idempotent) so the endpoint tests exercise the real prefix
# either way.
if URL not in {getattr(r, "path", None) for r in app.routes}:
    app.include_router(selection_reports_router, prefix=PREFIX, tags=["jute-production-reports"])

client = TestClient(app)

D1 = date(2026, 1, 23)
D2 = date(2026, 1, 24)
DTD5 = {"item_id": 269847, "item_code": "2", "item_name": "D TD 5"}
DTD6 = {"item_id": 269848, "item_code": "3", "item_name": "D TD 6"}
JTD7 = {"item_id": 269857, "item_code": "93", "item_name": "J TD 7 (US)"}


def _issue(q, d, kg):
    return {**q, "tran_date": d, "issue_kg": kg}


def _sel(q, d, kg, n):
    return {**q, "tran_date": d, "selection_kg": kg, "trolleys": n}


# =============================================================================
# build_selection_vs_issue
# =============================================================================


class TestBuildSelectionVsIssue:
    def test_both_sided_cell(self):
        r = build_selection_vs_issue([_issue(DTD5, D1, 1000.0)], [_sel(DTD5, D1, 950.25, 6)])
        assert r["by_quality"] == [
            {"item_id": 269847, "item_code": "2", "item_name": "D TD 5", "issue_kg": 1000.0,
             "selection_kg": 950.25, "trolleys": 6, "diff_kg": -49.75, "diff_pct": -4.98}
        ]
        assert r["by_day"] == [
            {"tran_date": "2026-01-23", "item_id": 269847, "item_name": "D TD 5", "issue_kg": 1000.0,
             "selection_kg": 950.25, "trolleys": 6, "diff_kg": -49.75}
        ]
        assert r["totals"] == {"issue_kg": 1000.0, "selection_kg": 950.25, "trolleys": 6,
                               "diff_kg": -49.75, "diff_pct": -4.98}

    def test_issue_only_quality_still_listed(self):
        r = build_selection_vs_issue([_issue(DTD6, D1, 400.0)], [])
        q = r["by_quality"][0]
        assert q["item_id"] == 269848 and q["issue_kg"] == 400.0
        assert q["selection_kg"] == 0.0 and q["trolleys"] == 0
        assert q["diff_kg"] == -400.0 and q["diff_pct"] == -100.0
        assert r["by_day"][0]["selection_kg"] == 0.0

    def test_selection_only_quality_diff_pct_is_none(self):
        r = build_selection_vs_issue([], [_sel(JTD7, D2, 120.5, 1)])
        q = r["by_quality"][0]
        assert q["item_id"] == 269857 and q["issue_kg"] == 0.0 and q["selection_kg"] == 120.5
        assert q["diff_kg"] == 120.5 and q["diff_pct"] is None
        assert r["totals"]["diff_pct"] is None
        assert r["totals"]["issue_kg"] == 0.0

    def test_empty(self):
        r = build_selection_vs_issue([], [])
        assert r == {
            "by_quality": [], "by_day": [],
            "totals": {"issue_kg": 0.0, "selection_kg": 0.0, "trolleys": 0, "diff_kg": 0.0, "diff_pct": None},
        }

    def test_merge_across_days_and_ordering(self):
        issue_rows = [
            _issue(DTD6, D1, 300.0),
            _issue(DTD5, D1, 1000.0),
            _issue(DTD5, D2, 500.0),
        ]
        selection_rows = [
            _sel(DTD5, D1, 980.0, 5),
            _sel(JTD7, D2, 200.0, 2),      # selection only
            _sel(DTD5, D2, 510.0, 3),
        ]
        r = build_selection_vs_issue(issue_rows, selection_rows)
        # by_quality ordered by item_name; per quality summed over the range
        assert [q["item_name"] for q in r["by_quality"]] == ["D TD 5", "D TD 6", "J TD 7 (US)"]
        d5 = r["by_quality"][0]
        assert d5["issue_kg"] == 1500.0 and d5["selection_kg"] == 1490.0 and d5["trolleys"] == 8
        assert d5["diff_kg"] == -10.0 and d5["diff_pct"] == pytest.approx(-0.67)
        # by_day ordered by date then item_name; a day/quality on one side still appears
        assert [(d["tran_date"], d["item_name"]) for d in r["by_day"]] == [
            ("2026-01-23", "D TD 5"), ("2026-01-23", "D TD 6"),
            ("2026-01-24", "D TD 5"), ("2026-01-24", "J TD 7 (US)"),
        ]
        d6 = r["by_day"][1]
        assert d6["issue_kg"] == 300.0 and d6["selection_kg"] == 0.0 and d6["diff_kg"] == -300.0
        assert r["totals"] == {"issue_kg": 1800.0, "selection_kg": 1690.0, "trolleys": 10,
                               "diff_kg": -110.0, "diff_pct": -6.11}

    def test_date_keys_accept_date_and_string(self):
        r = build_selection_vs_issue(
            [_issue(DTD5, "2026-01-23", 10.0)], [_sel(DTD5, date(2026, 1, 23), 9.0, 1)]
        )
        assert len(r["by_day"]) == 1
        assert r["by_day"][0]["diff_kg"] == -1.0

    def test_labels_fall_back_to_other_side(self):
        """A selection row without labels picks the item name from the issue side."""
        r = build_selection_vs_issue(
            [_issue(DTD5, D1, 10.0)],
            [{"item_id": 269847, "item_code": None, "item_name": None, "tran_date": D2,
              "selection_kg": 5.0, "trolleys": 1}],
        )
        assert r["by_quality"][0]["item_name"] == "D TD 5"
        assert r["by_day"][1]["item_name"] == "D TD 5"

    def test_weights_rounded_to_3(self):
        r = build_selection_vs_issue([_issue(DTD5, D1, 0.1 + 0.2)], [_sel(DTD5, D1, 0.3333333, 1)])
        assert r["by_quality"][0]["issue_kg"] == 0.3
        assert r["by_quality"][0]["selection_kg"] == 0.333
        assert r["by_quality"][0]["diff_kg"] == 0.033


# =============================================================================
# build_by_gate_entry
# =============================================================================


class TestBuildByGateEntry:
    def test_selection_with_matching_issue(self):
        sel = [{"jute_mr_id": 27755, "gate_entry_no": 36, "gate_entry_date": date(2026, 1, 18),
                "item_id": 269847, "item_name": "D TD 5", "selection_kg": 900.0, "trolleys": 5}]
        iss = [{"jute_mr_id": 27755, "item_id": 269847, "issue_kg": 1000.0},
               {"jute_mr_id": 27755, "item_id": 269848, "issue_kg": 50.0}]   # other quality, ignored
        assert build_by_gate_entry(sel, iss) == [
            {"jute_mr_id": 27755, "gate_entry_no": 36, "gate_entry_date": "2026-01-18",
             "item_id": 269847, "item_name": "D TD 5", "issue_kg": 1000.0,
             "selection_kg": 900.0, "trolleys": 5, "diff_kg": -100.0}
        ]

    def test_selection_without_issue_and_ordering(self):
        sel = [
            {"jute_mr_id": 27754, "gate_entry_no": 35, "gate_entry_date": "2026-01-13",
             "item_id": 269857, "item_name": "J TD 7 (US)", "selection_kg": 100.0, "trolleys": 1},
            {"jute_mr_id": 27758, "gate_entry_no": 39, "gate_entry_date": "2026-01-23",
             "item_id": 269848, "item_name": "D TD 6", "selection_kg": 200.0, "trolleys": 2},
            {"jute_mr_id": 27758, "gate_entry_no": 39, "gate_entry_date": "2026-01-23",
             "item_id": 269847, "item_name": "D TD 5", "selection_kg": 300.0, "trolleys": 3},
        ]
        out = build_by_gate_entry(sel, [])
        assert [(r["gate_entry_no"], r["item_name"]) for r in out] == [
            (39, "D TD 5"), (39, "D TD 6"), (35, "J TD 7 (US)"),
        ]
        assert all(r["issue_kg"] == 0.0 and r["diff_kg"] == r["selection_kg"] for r in out)

    def test_issue_only_mr_not_listed(self):
        assert build_by_gate_entry([], [{"jute_mr_id": 1, "item_id": 2, "issue_kg": 3.0}]) == []


# =============================================================================
# Endpoint
# =============================================================================


@pytest.fixture
def session():
    mock_session = MagicMock()
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: mock_session
    yield mock_session
    app.dependency_overrides.clear()


def _mapping_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _res(*, fetchall=None, fetchone=None):
    res = MagicMock()
    res.fetchall.return_value = fetchall if fetchall is not None else []
    res.fetchone.return_value = fetchone
    return res


class TestEndpoint:
    def test_happy_path(self, session):
        branch = _mapping_row({"branch_id": 2, "branch_name": "Factory"})
        session.execute.side_effect = [
            _res(fetchone=branch),
            _res(fetchall=[_mapping_row(_issue(DTD5, D1, 1000.0))]),
            _res(fetchall=[_mapping_row(_sel(DTD5, D1, 950.0, 6))]),
            _res(fetchall=[_mapping_row({
                "jute_mr_id": 27755, "gate_entry_no": 36, "gate_entry_date": date(2026, 1, 18),
                "item_id": 269847, "item_name": "D TD 5", "selection_kg": 950.0, "trolleys": 6})]),
            _res(fetchall=[_mapping_row({"jute_mr_id": 27755, "item_id": 269847, "issue_kg": 1000.0})]),
        ]

        r = client.get(f"{URL}?co_id=1&branch_id=2&date_from=2026-01-01&date_to=2026-01-31")

        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert set(data) == {"date_from", "date_to", "branch_id", "branch_name",
                             "by_quality", "by_day", "by_gate_entry", "totals"}
        assert data["date_from"] == "2026-01-01" and data["date_to"] == "2026-01-31"
        assert data["branch_id"] == 2 and data["branch_name"] == "Factory"
        assert data["by_quality"][0]["diff_kg"] == -50.0 and data["by_quality"][0]["diff_pct"] == -5.0
        assert data["by_day"][0]["tran_date"] == "2026-01-23"
        assert data["by_gate_entry"][0]["gate_entry_no"] == 36
        assert data["by_gate_entry"][0]["issue_kg"] == 1000.0
        assert data["totals"]["trolleys"] == 6
        # branch guard is co-scoped; the issue side is branch-scoped (jute_issue has no co_id)
        calls = session.execute.call_args_list
        assert calls[0].args[1] == {"co_id": 1, "branch_id": 2}
        issue_binds = calls[1].args[1]
        assert issue_binds["branch_id"] == 2 and "co_id" not in issue_binds
        assert str(issue_binds["date_from"]) == "2026-01-01" and str(issue_binds["date_to"]) == "2026-01-31"
        assert "actual_item_id" in str(calls[1].args[0]) and "status_id <> 4" in str(calls[1].args[0])
        sel_binds = calls[2].args[1]
        assert sel_binds["co_id"] == 1 and sel_binds["branch_id"] == 2
        assert "jute_prod_selection_trolly_wt" in str(calls[2].args[0])
        assert "sw.jute_mr_id IS NOT NULL" in str(calls[3].args[0])

    def test_empty_range_returns_zero_totals(self, session):
        session.execute.side_effect = [
            _res(fetchone=_mapping_row({"branch_id": 2, "branch_name": "Factory"})),
            _res(), _res(), _res(), _res(),
        ]
        r = client.get(f"{URL}?co_id=1&branch_id=2&date_from=2026-02-01&date_to=2026-02-28")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["by_quality"] == [] and data["by_day"] == [] and data["by_gate_entry"] == []
        assert data["totals"]["diff_pct"] is None

    @pytest.mark.parametrize("missing", ["co_id", "branch_id", "date_from", "date_to"])
    def test_missing_param_400(self, session, missing):
        params = {"co_id": "1", "branch_id": "2", "date_from": "2026-01-01", "date_to": "2026-01-31"}
        params.pop(missing)
        r = client.get(URL, params=params)
        assert r.status_code == 400
        assert missing in r.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_date_format_400(self, session):
        r = client.get(f"{URL}?co_id=1&branch_id=2&date_from=01-01-2026&date_to=2026-01-31")
        assert r.status_code == 400
        assert "date_from" in r.json()["detail"]

    def test_reversed_range_400(self, session):
        r = client.get(f"{URL}?co_id=1&branch_id=2&date_from=2026-01-31&date_to=2026-01-01")
        assert r.status_code == 400
        assert "before" in r.json()["detail"]
        session.execute.assert_not_called()

    def test_range_too_large_400(self, session):
        r = client.get(f"{URL}?co_id=1&branch_id=2&date_from=2025-01-01&date_to=2026-01-31")
        assert r.status_code == 400
        assert str(MAX_RANGE_DAYS) in r.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_branch_400(self, session):
        session.execute.side_effect = [_res(fetchone=None)]
        r = client.get(f"{URL}?co_id=1&branch_id=99&date_from=2026-01-01&date_to=2026-01-31")
        assert r.status_code == 400
        assert r.json()["detail"] == BAD_BRANCH_MSG
        assert session.execute.call_count == 1
