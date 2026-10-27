"""Tests for jute WASTE as a second stock source.

Process waste booked on the Waste Entry page becomes issuable jute stock
alongside purchased jute. Three things have to hold and are covered here:

  1. the two stock queries are column-compatible, because the endpoint
     concatenates their rows into one picker list;
  2. an issue line draws on EXACTLY ONE source, and a waste line is priced from
     jute_waste_rate_mst rather than from a (non-existent) MR line;
  3. the waste write guards — unknown entry, wrong branch, future booking,
     over-issue — each answer 400 rather than writing a bad row;
  4. a GROUPED waste pick (one balance per item, TKT-000147) is split FIFO
     across that item's bookings into one jute_issue row each, summing back to
     exactly what was asked for.

Follows test_jute_issue.py: FastAPI dependency overrides + a MagicMock session,
so nothing here touches a database.
"""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import JUTE_MR_ISSUABLE_STATUSES, JUTE_WASTE_ITEM_TYPE_ID
from src.juteProcurement.query import (
    get_jute_stock_outstanding_by_item_query,
    get_jute_stock_outstanding_query,
    get_jute_waste_entries_for_allocation_query,
    get_jute_waste_entry_for_issue_query,
    get_jute_waste_rate_expression,
    get_jute_waste_stock_outstanding_query,
    get_jute_issues_by_date_query,
)
from src.juteProduction.waste_query import (
    get_waste_rate_by_id_query,
    get_waste_rate_list_query,
    insert_waste_rate_query,
    update_waste_rate_query,
)
from src.main import app

client = TestClient(app)


def _select_columns(sql: str) -> list[str]:
    """Column aliases of the OUTERMOST SELECT list, in order.

    Everything nested — the recursive CTE that resolves group-name paths and the
    correlated rate sub-select — sits inside parentheses, so the outer
    projection is bounded by the first SELECT and the first FROM found at paren
    depth 0. Commas are likewise split only at depth 0 so a sub-select stays
    whole. Regex alone cannot do this: the rate expression contains both its own
    SELECT/FROM and its own commas.
    """
    depth = 0
    start = end = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if start is None and sql.startswith("SELECT", i):
                start = i + len("SELECT")
                i += len("SELECT")
                continue
            if start is not None and sql.startswith("FROM", i):
                end = i
                break
        i += 1
    assert start is not None and end is not None, "could not locate the outer projection"

    depth, current, parts = 0, "", []
    for ch in sql[start:end]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)

    cols = []
    for p in parts:
        p = " ".join(p.split())
        m = re.search(r"(?:\bAS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*$", p, re.IGNORECASE)
        cols.append(m.group(1).lower())
    return cols


def test_select_columns_helper_finds_the_outer_projection():
    """The helper is load-bearing for the parity tests — check it on a known shape."""
    assert _select_columns(
        "WITH x AS (SELECT a, b FROM t) "
        "SELECT p.one, (SELECT z FROM q WHERE q.i = p.i, 1) AS two, p.three FROM p"
    ) == ["one", "two", "three"]


class TestStockSourceParity:
    """The endpoint concatenates MR rows and waste rows into ONE list."""

    def test_waste_and_mr_stock_expose_identical_columns(self):
        mr = _select_columns(str(get_jute_stock_outstanding_query()))
        waste = _select_columns(str(get_jute_waste_stock_outstanding_query()))
        assert mr == waste, (
            "stock sources drifted — the picker would render ragged rows.\n"
            f"only in MR:    {set(mr) - set(waste)}\n"
            f"only in WASTE: {set(waste) - set(mr)}"
        )

    def test_by_item_variant_matches_too(self):
        assert _select_columns(str(get_jute_stock_outstanding_by_item_query())) == (
            _select_columns(str(get_jute_waste_stock_outstanding_query()))
        )

    def test_rows_are_keyed_on_stock_key_not_mr_line(self):
        """A waste row has no jute_mr_li_id, so stock_key is the picker's key.

        Since TKT-000147 the waste key is the ITEM, not the entry: the picker
        offers one grouped balance per waste item and the server fans the issue
        back out across that item's bookings.
        """
        mr = str(get_jute_stock_outstanding_query())
        waste = str(get_jute_waste_stock_outstanding_query())
        assert "CONCAT('MR-', vso.jute_mr_li_id) AS stock_key" in mr
        assert "CONCAT('WI-', v.item_id) AS stock_key" in waste
        assert "'MR' AS source" in mr
        assert "'WASTE' AS source" in waste

    def test_waste_is_kg_only(self):
        """Waste has no bale/drum count: qty == weight and the unit is KG."""
        waste = str(get_jute_waste_stock_outstanding_query())
        assert "'KG' AS unit_conversion" in waste

    def test_waste_stock_is_company_and_date_scoped(self):
        waste = str(get_jute_waste_stock_outstanding_query())
        assert "v.co_id = :co_id" in waste
        assert "v.branch_id = :branch_id" in waste
        assert "v.inward_date <= :issue_date" in waste
        assert "ROUND(v.bal_weight, 2) > 0" in waste

    def test_waste_item_filter_is_optional(self):
        """One builder serves both the all-items and by-item picker calls."""
        assert "(:item_id IS NULL OR v.item_id = :item_id)" in str(
            get_jute_waste_stock_outstanding_query()
        )

    def test_mr_arm_excludes_purchased_waste_lines(self):
        """Owner ruling 2026-09-25 (D4): an MR line of a type-3 item is waste
        stock and lives in the grouped 'WI-<item>' row. Listing it per MR line
        too would offer the same kg twice. `ig` is LEFT JOINed, hence the
        COALESCE — a line whose item has no group must stay listed."""
        predicate = f"COALESCE(ig.item_type_id, 0) <> {JUTE_WASTE_ITEM_TYPE_ID}"
        for builder in (get_jute_stock_outstanding_query, get_jute_stock_outstanding_by_item_query):
            sql = " ".join(str(builder()).split())
            assert predicate in sql, builder.__name__
            assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in sql
            assert "ig.item_type_id = 2" not in sql, "would drop group-less lines"

    def test_waste_picker_keeps_purchased_waste(self):
        """The grouped row is the ONE place purchased waste is offered."""
        sql = " ".join(str(get_jute_waste_stock_outstanding_query()).split())
        assert "item_type_id" not in sql

    def test_waste_picker_renarrows_the_mr_leg_to_issuable_statuses(self):
        """The view's MR leg is as broad as vw_jute_stock_outstanding (in-flight
        MRs included, for reports); the picker stops at the same tuple the MR
        arm uses, and a PROD row (no MR line) always passes."""
        sql = " ".join(str(get_jute_waste_stock_outstanding_query()).split())
        tuple_sql = ", ".join(str(s) for s in JUTE_MR_ISSUABLE_STATUSES)
        assert f"(v.jute_mr_li_id IS NULL OR vjm.status_id IN ({tuple_sql}))" in sql
        assert "LEFT JOIN jute_mr_li vml ON vml.jute_mr_li_id = v.jute_mr_li_id" in sql
        assert "LEFT JOIN jute_mr vjm ON vjm.jute_mr_id = vml.jute_mr_id" in sql


class TestWasteRateExpression:
    """Rate resolution must be identical for the picker and the save."""

    def test_prefers_branch_specific_then_latest_valid_from(self):
        expr = get_jute_waste_rate_expression("v.item_id", "v.branch_id", "v.co_id")
        assert "ORDER BY (r.branch_id IS NOT NULL) DESC, r.valid_from DESC" in expr
        assert "LIMIT 1" in expr

    def test_honours_the_validity_window_and_active_flag(self):
        expr = get_jute_waste_rate_expression("v.item_id", "v.branch_id", "v.co_id")
        assert "r.active = 1" in expr
        assert "r.valid_from <= :issue_date" in expr
        assert "r.valid_to IS NULL OR r.valid_to >= :issue_date" in expr

    def test_company_wide_rate_matches_any_branch(self):
        expr = get_jute_waste_rate_expression("v.item_id", "v.branch_id", "v.co_id")
        assert "r.branch_id IS NULL OR r.branch_id = v.branch_id" in expr

    def test_stock_query_and_write_guard_share_one_expression(self):
        """If these drifted the picker would preview a value the save wouldn't store."""
        expr = " ".join(
            get_jute_waste_rate_expression("v.item_id", "v.branch_id", "v.co_id").split()
        )
        assert expr in " ".join(str(get_jute_waste_stock_outstanding_query()).split())
        assert expr in " ".join(str(get_jute_waste_entry_for_issue_query()).split())

    def test_missing_rate_falls_back_to_zero(self):
        """An unpriced waste item still issues, at zero value."""
        assert "COALESCE(" in str(get_jute_waste_stock_outstanding_query())
        assert ", 0) AS actual_rate" in str(get_jute_waste_entry_for_issue_query())


class TestIssuesByDateShowsWasteLines:
    def test_source_is_derived_from_the_link_column(self):
        sql = str(get_jute_issues_by_date_query())
        assert (
            "CASE WHEN ji.waste_entry_id IS NOT NULL THEN 'WASTE' ELSE 'MR' END AS source"
            in sql
        )

    def test_waste_provenance_is_left_joined(self):
        """Waste lines have no MR, so dept/area stand in — and must not drop rows."""
        sql = str(get_jute_issues_by_date_query())
        assert "LEFT JOIN jute_prod_waste_entry we" in sql
        assert "LEFT JOIN wastage_area_mst wa" in sql
        assert "LEFT JOIN dept_mst wd" in sql

    def test_saved_value_is_not_repriced_from_the_master(self):
        """A rate edited later must not restate history: derive from issue_value."""
        sql = str(get_jute_issues_by_date_query())
        assert "ROUND(ji.issue_value / ji.weight * 100, 2)" in sql
        assert "jute_waste_rate_mst" not in sql

    def test_purchased_waste_slice_carries_is_waste_from_the_item_group(self):
        """A FIFO slice of PURCHASED waste is saved on its MR line, so it reloads
        as source 'MR'. Without this flag the edit page could only infer waste
        from a leftover picker row or a sibling PROD slice — and a mill that
        only BUYS waste and drains the pool in one draw has neither, so the
        chip would read 'MR'. Derived from the group's item_type_id, never from
        the group name, and `ig` is LEFT JOINed so a group-less item still
        loads (as 0)."""
        sql = str(get_jute_issues_by_date_query())
        assert (
            f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste"
            in sql
        )
        assert "LEFT JOIN item_grp_mst ig" in sql


class TestWasteRateMasterQueries:
    def test_list_is_company_scoped_and_includes_company_wide_rows(self):
        sql = str(get_waste_rate_list_query())
        assert "r.co_id = :co_id" in sql
        # a company-wide rate is in force for the branch, so a branch filter keeps it
        assert ":branch_id IS NULL OR r.branch_id = :branch_id OR r.branch_id IS NULL" in sql

    def test_by_id_and_update_are_company_scoped(self):
        """Without co_id another company's rate would be readable/editable by id."""
        assert "waste_rate_id = :waste_rate_id AND co_id = :co_id" in str(
            get_waste_rate_by_id_query()
        )
        assert "WHERE waste_rate_id = :waste_rate_id AND co_id = :co_id" in str(
            update_waste_rate_query()
        )

    def test_insert_binds_every_column(self):
        sql = str(insert_waste_rate_query())
        for col in ("co_id", "branch_id", "item_id", "rate", "valid_from", "valid_to"):
            assert f":{col}" in sql


class TestWasteIssueEndpoints:
    """create_issue / get_stock_outstanding behaviour, with a mocked session."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1,
            "sub": "test_user",
            "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_db(self):
        return self._mock_session

    @staticmethod
    def _payload(**overrides):
        body = {
            "branch_id": 29,
            "issue_date": "2026-08-10",
            "yarn_type_id": 5,
            "item_id": 335637,
            "quantity": 100.0,
            "weight": 100.0,
            "unit_conversion": "KG",
        }
        body.update(overrides)
        return body

    def test_rejects_a_line_with_no_stock_source(self, mock_db):
        response = client.post("/api/juteIssue/create_issue?co_id=2", json=self._payload())
        assert response.status_code == 400
        assert "exactly one" in response.json()["detail"].lower()

    def test_rejects_a_line_claiming_both_sources(self, mock_db):
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(jute_mr_li_id=24143, waste_entry_id=1),
        )
        assert response.status_code == 400
        assert "exactly one" in response.json()["detail"].lower()

    def test_rejects_an_unknown_waste_entry(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload(waste_entry_id=999)
        )
        assert response.status_code == 400
        assert "waste entry" in response.json()["detail"].lower()

    def test_rejects_a_waste_entry_from_another_branch(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=4, inward_date="2026-08-01", bal_weight=500.0, actual_rate=800.0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload(waste_entry_id=1)
        )
        assert response.status_code == 400
        assert "different branch" in response.json()["detail"].lower()

    def test_rejects_waste_booked_after_the_issue_date(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-20", bal_weight=500.0, actual_rate=800.0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload(waste_entry_id=1)
        )
        assert response.status_code == 400
        assert "after the issue date" in response.json()["detail"].lower()

    def test_rejects_an_over_issue(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-01", bal_weight=40.0, actual_rate=800.0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(waste_entry_id=1, quantity=100.0, weight=100.0),
        )
        assert response.status_code == 400
        assert "available waste balance" in response.json()["detail"].lower()

    def test_full_balance_issue_is_allowed(self, mock_db):
        """Exactly the balance must pass — DECIMAL(10,3) vs FLOAT rounding."""
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-01", bal_weight=100.0, actual_rate=800.0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(waste_entry_id=1, quantity=100.0, weight=100.0),
        )
        assert response.status_code == 200

    def test_waste_line_is_priced_from_the_rate_master(self, mock_db):
        """issue_value = weight/100 * rate, with the rate coming from the master."""
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-01", bal_weight=500.0, actual_rate=850.0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(waste_entry_id=1, quantity=120.0, weight=120.0),
        )
        assert response.status_code == 200
        # 120 kg = 1.2 quintal * 850
        assert response.json()["issue_value"] == pytest.approx(1020.0)

    def test_unpriced_waste_issues_at_zero_value(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-01", bal_weight=500.0, actual_rate=0
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(waste_entry_id=1, quantity=50.0, weight=50.0),
        )
        assert response.status_code == 200
        assert response.json()["issue_value"] == 0

    def test_waste_entry_id_is_persisted(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29, inward_date="2026-08-01", bal_weight=500.0, actual_rate=800.0
        )
        client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload(waste_entry_id=1)
        )
        inserts = [
            c for c in mock_db.execute.call_args_list if "INSERT INTO jute_issue" in str(c[0][0])
        ]
        assert inserts, "no INSERT was issued"
        params = inserts[0][0][1]
        assert params["waste_entry_id"] == 1
        assert params["jute_mr_li_id"] is None

    def test_mr_line_still_persists_with_a_null_waste_link(self, mock_db):
        """The existing MR path must be untouched by the new column."""
        # The MR lookup now also returns the balances its write guard needs —
        # see test_jute_mr_stock_guard.py.
        mock_db.execute.return_value.fetchone.return_value = MagicMock(
            branch_id=29,
            unit_conversion="BALE",
            actual_rate=1500.0,
            bal_qty=500.0,
            bal_weight=5000.0,
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(jute_mr_li_id=24143, quantity=10.0, weight=200.0),
        )
        assert response.status_code == 200
        assert response.json()["issue_value"] == pytest.approx(3000.0)
        inserts = [
            c for c in mock_db.execute.call_args_list if "INSERT INTO jute_issue" in str(c[0][0])
        ]
        params = inserts[0][0][1]
        assert params["jute_mr_li_id"] == 24143
        assert params["waste_entry_id"] is None

    def test_stock_endpoint_queries_both_sources(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        response = client.get(
            "/api/juteIssue/get_stock_outstanding?co_id=2&branch_id=29&issue_date=2026-08-10"
        )
        assert response.status_code == 200
        executed = " ".join(str(c[0][0]) for c in mock_db.execute.call_args_list)
        assert "vw_jute_stock_outstanding" in executed
        assert "vw_jute_waste_stock_outstanding" in executed

    def test_stock_endpoint_defaults_a_missing_issue_date_to_today(self, mock_db):
        """The old '' fallback compared a DATE to '' and returned NO stock."""
        from datetime import date

        mock_db.execute.return_value.fetchall.return_value = []
        client.get("/api/juteIssue/get_stock_outstanding?co_id=2&branch_id=29")
        dates = {
            c[0][1]["issue_date"]
            for c in mock_db.execute.call_args_list
            if len(c[0]) > 1 and "issue_date" in c[0][1]
        }
        assert dates == {date.today().isoformat()}


class TestGroupedWasteFifo:
    """A grouped waste pick fans out across its bookings, oldest first.

    The picker offers one balance per waste item (TKT-000147), so the server is
    what turns "Dust, 200 kg" back into the Waste Entry bookings it comes out
    of. What must hold: the draw is FIFO, one row is written per booking
    consumed, the rows sum EXACTLY to the requested weight and quantity, and
    the guards answer 400 against the SUMMED balance the operator was shown.
    """

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        self._mock_session.execute.return_value.fetchone.return_value = MagicMock(id=7)
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1,
            "sub": "test_user",
            "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_db(self):
        return self._mock_session

    @staticmethod
    def _bookings(*pairs, rate=850.0):
        """(waste_entry_id, bal_weight) pairs as the allocation query returns
        them — PROD rows only (source_type 'PROD', no MR line, no MR rate)."""
        return [
            MagicMock(
                waste_entry_id=eid,
                inward_date=f"2026-08-{day:02d}",
                bal_weight=bal,
                actual_rate=rate,
                source_type="PROD",
                jute_mr_li_id=None,
                rate=None,
            )
            for day, (eid, bal) in enumerate(pairs, start=1)
        ]

    @staticmethod
    def _payload(**overrides):
        body = {
            "branch_id": 29,
            "issue_date": "2026-08-31",
            "yarn_type_id": 5,
            "item_id": 335637,
            "waste_item_id": 335637,
            "quantity": 200.0,
            "weight": 200.0,
            "unit_conversion": "KG",
        }
        body.update(overrides)
        return body

    @staticmethod
    def _inserted(mock_db):
        return [
            c[0][1]
            for c in mock_db.execute.call_args_list
            if "INSERT INTO jute_issue" in str(c[0][0])
        ]

    def test_draws_oldest_bookings_first(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 112.0), (103, 90.0)
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        )
        assert response.status_code == 200, response.text

        rows = self._inserted(mock_db)
        assert [(r["waste_entry_id"], r["weight"]) for r in rows] == [
            (101, 56.0),
            (102, 112.0),
            (103, 32.0),
        ]

    def test_rows_sum_to_the_requested_weight_and_quantity(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 112.0), (103, 90.0)
        )
        client.post("/api/juteIssue/create_issue?co_id=2", json=self._payload())

        rows = self._inserted(mock_db)
        assert sum(r["weight"] for r in rows) == pytest.approx(200.0)
        assert sum(r["quantity"] for r in rows) == pytest.approx(200.0)

    def test_every_row_carries_the_item_and_no_mr_link(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 500.0)
        )
        client.post("/api/juteIssue/create_issue?co_id=2", json=self._payload())

        for row in self._inserted(mock_db):
            assert row["item_id"] == 335637
            assert row["jute_mr_li_id"] is None
            assert row["waste_entry_id"] is not None
            assert row["status_id"] == 21

    def test_returns_every_id_it_wrote(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 200.0)
        )
        body = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        ).json()
        assert len(body["jute_issue_ids"]) == 2
        # The single-id key stays populated for callers that expected one row.
        assert body["jute_issue_id"] == body["jute_issue_ids"][0]

    def test_value_is_the_sum_of_the_slices(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 200.0), rate=850.0
        )
        body = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        ).json()
        # 200 kg = 2 quintal * 850, however it was split.
        assert body["issue_value"] == pytest.approx(1700.0)

    def test_rejects_more_than_the_summed_balance(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 40.0)
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        )
        assert response.status_code == 400
        assert "available waste balance" in response.json()["detail"].lower()
        assert not self._inserted(mock_db), "a rejected draw must write nothing"

    def test_exactly_the_summed_balance_is_allowed(self, mock_db):
        """The grouped twin of the per-entry full-balance case."""
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 56.0), (102, 144.0)
        )
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        )
        assert response.status_code == 200
        assert sum(r["weight"] for r in self._inserted(mock_db)) == pytest.approx(200.0)

    def test_rejects_an_item_with_no_open_bookings(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        )
        assert response.status_code == 400
        assert "no waste stock available" in response.json()["detail"].lower()

    def test_rejects_a_grouped_pick_that_also_names_an_mr_line(self, mock_db):
        response = client.post(
            "/api/juteIssue/create_issue?co_id=2",
            json=self._payload(jute_mr_li_id=24143),
        )
        assert response.status_code == 400
        assert "exactly one" in response.json()["detail"].lower()

    def test_allocation_is_scoped_to_the_branch_company_item_and_date(self, mock_db):
        """The allocator must not be able to draw stock the picker never offered."""
        mock_db.execute.return_value.fetchall.return_value = self._bookings((101, 500.0))
        client.post("/api/juteIssue/create_issue?co_id=2", json=self._payload())

        binds = [
            c[0][1]
            for c in mock_db.execute.call_args_list
            if len(c[0]) > 1 and "vw_jute_waste_stock_outstanding" in str(c[0][0])
        ]
        assert binds, "the allocation query was never run"
        assert binds[0] == {
            "co_id": 2,
            "branch_id": 29,
            "item_id": 335637,
            "issue_date": date(2026, 8, 31),
        }

    def test_unpriced_grouped_waste_issues_at_zero_value(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = self._bookings(
            (101, 500.0), rate=0
        )
        body = client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload()
        ).json()
        assert body["issue_value"] == 0


class TestGroupedWasteAllocationQuery:
    """The allocator and the picker must agree on what stock exists."""

    def test_orders_oldest_first_across_both_legs(self):
        """Since purchased waste joined the pool (2026-09-25) a row is keyed by
        waste_entry_id (PROD) OR jute_mr_li_id (MR); the tie-break must work
        for either, so both legs interleave by date, then by their own id."""
        sql = str(get_jute_waste_entries_for_allocation_query())
        assert (
            "ORDER BY v.inward_date ASC, COALESCE(v.waste_entry_id, v.jute_mr_li_id) ASC" in sql
        )

    def test_exposes_the_source_and_both_keys_per_row(self):
        sql = " ".join(str(get_jute_waste_entries_for_allocation_query()).split())
        assert "v.source_type," in sql
        assert "v.jute_mr_li_id," in sql
        assert "v.rate AS mr_rate" in sql
        assert "v.waste_entry_id," in sql

    def test_renarrows_the_mr_leg_exactly_like_the_picker(self):
        tuple_sql = ", ".join(str(s) for s in JUTE_MR_ISSUABLE_STATUSES)
        clause = f"(v.jute_mr_li_id IS NULL OR vjm.status_id IN ({tuple_sql}))"
        assert clause in " ".join(str(get_jute_waste_entries_for_allocation_query()).split())
        assert clause in " ".join(str(get_jute_waste_stock_outstanding_query()).split())

    def test_shares_the_pickers_eligibility_predicate(self):
        """Diverge here and the picker offers a balance the allocator refuses."""
        alloc = str(get_jute_waste_entries_for_allocation_query())
        picker = str(get_jute_waste_stock_outstanding_query())
        for clause in (
            "v.branch_id = :branch_id",
            "v.co_id = :co_id",
            "ROUND(v.bal_weight, 2) > 0",
            "v.inward_date <= :issue_date",
        ):
            assert clause in alloc, clause
            assert clause in picker, clause

    def test_prices_from_the_same_rate_expression_as_the_picker(self):
        expr = " ".join(
            get_jute_waste_rate_expression("v.item_id", "v.branch_id", "v.co_id").split()
        )
        assert expr in " ".join(str(get_jute_waste_entries_for_allocation_query()).split())

    def test_per_entry_guard_still_keys_on_waste_entry_id(self):
        """NULL on the view's MR leg, so a purchased line can never be drawn
        through the legacy direct-entry path."""
        sql = " ".join(str(get_jute_waste_entry_for_issue_query()).split())
        assert "WHERE v.waste_entry_id = :waste_entry_id AND v.co_id = :co_id" in sql


class TestPurchasedWasteFifo:
    """Purchased waste (an MR line of a type-3 item) is part of the grouped
    waste pool (owner ruling 2026-09-25, D3/D4). The FIFO allocator walks PROD
    bookings and MR lines in ONE date order; a PROD slice is written with
    waste_entry_id and priced from the waste rate master, an MR slice with
    jute_mr_li_id and priced from the MR line through the MR stock guard —
    every row carries exactly one key.
    """

    RATE_MASTER = 850.0
    MR_RATE = 900.0

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1,
            "sub": "test_user",
            "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    @staticmethod
    def _prod(entry_id, day, bal):
        return MagicMock(
            source_type="PROD", waste_entry_id=entry_id, jute_mr_li_id=None,
            inward_date=f"2026-08-{day:02d}", bal_weight=bal,
            actual_rate=TestPurchasedWasteFifo.RATE_MASTER, rate=None,
        )

    @staticmethod
    def _mr(li_id, day, bal, view_rate=111.0):
        """view_rate is the informational jml.rate on the view; the slice must
        be priced from the guard row (actual_rate), never from it."""
        return MagicMock(
            source_type="MR", waste_entry_id=None, jute_mr_li_id=li_id,
            inward_date=f"2026-08-{day:02d}", bal_weight=bal,
            actual_rate=TestPurchasedWasteFifo.RATE_MASTER, rate=view_rate,
        )

    def _wire(self, pool, guard_rows):
        """Route each execute(): allocation query -> pool; MR line guard ->
        the row for that jute_mr_li_id; LAST_INSERT_ID -> a fresh id."""
        ids = iter(range(9001, 9100))

        def _execute(sql, params=None):
            text_ = " ".join(str(sql).split())
            result = MagicMock()
            if "vw_jute_waste_stock_outstanding" in text_:
                result.fetchall.return_value = pool
            elif "FROM jute_mr_li jml" in text_:
                result.fetchone.return_value = guard_rows.get(params["jute_mr_li_id"])
            elif "LAST_INSERT_ID" in text_:
                result.fetchone.return_value = MagicMock(id=next(ids))
            else:
                result.fetchone.return_value = None
                result.fetchall.return_value = []
            return result

        self.db.execute.side_effect = _execute

    @staticmethod
    def _guard(bal_weight, bal_qty=1.0, rate=900.0, line_rate=None):
        """One row of get_jute_mr_line_for_issue_query for a type-3 line
        (is_waste 1). bal_qty is deliberately junk (waste has no bale count)
        — the allocator must not care. `rate` is jml.actual_rate, `line_rate`
        the QC-written jml.rate the guard falls back to."""
        return MagicMock(
            branch_id=29, unit_conversion="KG", actual_rate=rate, rate=line_rate,
            is_waste=1, bal_qty=bal_qty, bal_weight=bal_weight,
        )

    @staticmethod
    def _payload(weight=200.0, **overrides):
        body = {
            "branch_id": 29, "issue_date": "2026-08-31", "yarn_type_id": 5,
            "item_id": 700, "waste_item_id": 700,
            "quantity": weight, "weight": weight, "unit_conversion": "KG",
        }
        body.update(overrides)
        return body

    def _inserted(self):
        return [
            c[0][1] for c in self.db.execute.call_args_list
            if "INSERT INTO jute_issue" in str(c[0][0])
        ]

    def _post(self, **overrides):
        return client.post("/api/juteIssue/create_issue?co_id=2", json=self._payload(**overrides))

    def test_mr_only_pool_writes_mr_keyed_rows_priced_at_the_line_rate(self):
        """actual_rate (saved on the MR page) wins over the QC-written jml.rate
        and over the waste rate master."""
        self._wire(
            [self._mr(501, 1, 150.0), self._mr(502, 2, 100.0)],
            {501: self._guard(150.0, line_rate=111.0), 502: self._guard(100.0, line_rate=111.0)},
        )
        response = self._post()
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [(r["jute_mr_li_id"], r["waste_entry_id"], r["weight"]) for r in rows] == [
            (501, None, 150.0),
            (502, None, 50.0),
        ]
        # 200 kg = 2 quintal * 900 (MR line rate), not 850 (waste master) or 111 (view)
        assert response.json()["issue_value"] == pytest.approx(1800.0)
        assert response.json()["warnings"] == []
        assert response.json()["jute_issue_ids"] == [9001, 9002]

    def test_mixed_pool_interleaves_by_date_and_keys_each_row_by_its_source(self):
        self._wire(
            [self._prod(101, 1, 56.0), self._mr(501, 2, 100.0), self._prod(102, 3, 90.0)],
            {501: self._guard(100.0)},
        )
        response = self._post()
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [(r["waste_entry_id"], r["jute_mr_li_id"], r["weight"]) for r in rows] == [
            (101, None, 56.0),
            (None, 501, 100.0),
            (102, None, 44.0),
        ]
        for r in rows:
            assert (r["waste_entry_id"] is None) != (r["jute_mr_li_id"] is None), "one key"
            assert r["item_id"] == 700 and r["status_id"] == 21
        # 0.56 q * 850 + 1.00 q * 900 + 0.44 q * 850
        assert response.json()["issue_value"] == pytest.approx(476.0 + 900.0 + 374.0)
        assert sum(r["weight"] for r in rows) == pytest.approx(200.0)
        assert sum(r["quantity"] for r in rows) == pytest.approx(200.0)

    def test_mr_slice_is_guarded_through_the_mr_line_guard(self):
        """Same helper as a direct MR issue: bound by the line's own id, at the
        company being issued from, with quantity 0 so the bale-count warning
        path never fires for kg-only waste."""
        self._wire([self._mr(501, 1, 300.0)], {501: self._guard(300.0, bal_qty=0.5)})
        response = self._post(weight=120.0)
        assert response.status_code == 200, response.text
        guard_calls = [
            c for c in self.db.execute.call_args_list if "FROM jute_mr_li jml" in str(c[0][0])
        ]
        assert [c[0][1] for c in guard_calls] == [{"jute_mr_li_id": 501, "co_id": 2}]
        assert response.json()["warnings"] == []
        assert response.json()["issue_value"] == pytest.approx(1.2 * 900.0)

    def test_mr_slice_from_another_branch_is_refused(self):
        self._wire([self._mr(501, 1, 300.0)], {501: MagicMock(
            branch_id=4, unit_conversion="KG", actual_rate=900.0, bal_qty=1, bal_weight=300.0,
        )})
        response = self._post(weight=120.0)
        assert response.status_code == 400
        assert "different branch" in response.json()["detail"].lower()
        assert not self._inserted()

    def test_an_mr_slice_that_drains_its_line_takes_the_exact_fractional_balance(self):
        """Owner rule: MR-keyed issues are whole kg — except the draw that
        DRAINS a line, which takes the exact balance so the line closes at
        zero. A 143.5 kg purchased remnant is therefore drawn as 143.5 (not
        capped at 143 with 0.5 kg stranded forever) and the next booking
        supplies the rest — the rows sum to exactly the 200 kg asked for."""
        self._wire(
            [self._mr(501, 1, 143.5), self._prod(101, 2, 100.0)],
            {501: self._guard(143.5)},
        )
        response = self._post(weight=200.0)
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [(r["jute_mr_li_id"], r["waste_entry_id"], r["weight"]) for r in rows] == [
            (501, None, 143.5),
            (None, 101, 56.5),
        ]
        assert sum(r["weight"] for r in rows) == pytest.approx(200.0)
        assert response.json()["warnings"] == []

    @pytest.mark.parametrize("bal", [2.9, 0.9, 0.4])
    def test_asking_exactly_the_shown_balance_of_a_fractional_stub_writes_it_in_full(self, bal):
        """The reviewer's sls case: one open stub (li 6933, br 21, 2.9 kg) and
        the operator asks the balance the picker shows. Whole-kg capping wrote
        2 kg and left 0.9 kg in the picker forever; a sub-kg stub (6 open sls
        rows) was skipped outright and the operator got 'No issuable waste
        stock' for a balance they could see. Now the stub drains exactly."""
        self._wire([self._mr(6933, 1, bal)], {6933: self._guard(bal)})
        response = self._post(weight=bal)
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [(r["jute_mr_li_id"], r["weight"]) for r in rows] == [(6933, bal)]
        assert response.json()["warnings"] == []
        assert response.json()["issue_value"] == pytest.approx(round(bal / 100 * 900.0, 2))

    def test_a_trailing_mr_slice_rounds_half_up_like_a_direct_mr_issue_and_says_so(self):
        """When the LAST slice is a non-draining MR slice there is no PROD
        slice to absorb the fraction, so the whole-kg value stands (56.5 + 144
        = 200.5), exactly what a direct 143.5 kg MR issue writes — and the
        response says so instead of leaving the operator to spot it."""
        self._wire(
            [self._prod(101, 1, 56.5), self._mr(501, 2, 300.0)],
            {501: self._guard(300.0)},
        )
        response = self._post(weight=200.0)
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [(r["waste_entry_id"], r["jute_mr_li_id"], r["weight"]) for r in rows] == [
            (101, None, 56.5),
            (None, 501, 144.0),
        ]
        assert response.json()["warnings"] == [
            "Purchased waste is issued in whole kg: 200.5 kg written for 200 kg asked"
        ]

    def test_a_fractional_ask_that_rounds_down_on_a_purchased_line_is_reported_not_hidden(self):
        self._wire([self._mr(501, 1, 300.0)], {501: self._guard(300.0)})
        response = self._post(weight=143.4)
        assert response.status_code == 200, response.text
        assert [r["weight"] for r in self._inserted()] == [143.0]
        assert response.json()["warnings"] == [
            "Purchased waste is issued in whole kg: 143 kg written for 143.4 kg asked"
        ]

    def test_an_ask_that_rounds_past_a_fractional_stub_drains_it(self):
        """2.5 against a 2.9 kg stub: half-up says 3, the line holds 2.9, so
        the line drains (2.9) rather than being short-written as 2."""
        self._wire([self._mr(6933, 1, 2.9)], {6933: self._guard(2.9)})
        response = self._post(weight=2.5)
        assert response.status_code == 200, response.text
        assert [r["weight"] for r in self._inserted()] == [2.9]
        assert "2.9 kg written for 2.5 kg asked" in response.json()["warnings"][0]

    def test_a_purchased_line_without_actual_rate_is_priced_at_its_qc_rate(self):
        """On sls 314 of the 330 open purchased-waste lines carry no
        actual_rate (the QC insert writes `rate` only) and FIFO drains exactly
        those first, so pricing from actual_rate alone valued the first pooled
        issues at 0 while the receipt side had booked them at total_price."""
        self._wire(
            [self._mr(501, 1, 150.0), self._mr(502, 2, 100.0)],
            {
                501: self._guard(150.0, rate=None, line_rate=13500.0),
                502: self._guard(100.0, rate=0, line_rate=13500.0),
            },
        )
        response = self._post(weight=200.0)
        assert response.status_code == 200, response.text
        rows = self._inserted()
        assert [r["issue_value"] for r in rows] == [
            pytest.approx(1.5 * 13500.0), pytest.approx(0.5 * 13500.0)
        ]
        assert response.json()["issue_value"] == pytest.approx(2.0 * 13500.0)

    def test_over_draw_is_judged_against_the_pooled_balance(self):
        self._wire(
            [self._prod(101, 1, 56.0), self._mr(501, 2, 40.0)],
            {501: self._guard(40.0)},
        )
        response = self._post(weight=200.0)
        assert response.status_code == 400
        assert "available waste balance" in response.json()["detail"].lower()
        assert not self._inserted()

    def test_prod_only_pool_still_never_calls_the_mr_guard(self):
        self._wire([self._prod(101, 1, 500.0)], {})
        response = self._post(weight=120.0)
        assert response.status_code == 200, response.text
        assert not [
            c for c in self.db.execute.call_args_list if "FROM jute_mr_li jml" in str(c[0][0])
        ]
        rows = self._inserted()
        assert rows[0]["waste_entry_id"] == 101 and rows[0]["jute_mr_li_id"] is None
        assert response.json()["issue_value"] == pytest.approx(1.2 * 850.0)
