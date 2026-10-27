"""
TKT-000193 — "previous/current stock is not showing while creating indent".

/validate_item_for_indent used to take branch_stock from
vw_item_balance_qty_by_branch_new, which is driven from item_minmax_mst: items
without a min/max row (sls br 29: 2681 of 3128 stocked items) had no row and
reported 0, and Logic 3 items returned before the lookup with stock None. Stock
is now read directly from the approved-SR balance for every item and logic.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.main import app

client = TestClient(app)
URL = "/api/procurementIndent/validate_item_for_indent"


def _expense_row(name):
    row = MagicMock()
    row._mapping = {"expense_type_name": name}
    return row


class TestBranchStockSource:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _install(self, session):
        def _override():
            yield session
        app.dependency_overrides[get_tenant_db] = _override

    def test_logic3_reports_stock_even_though_validation_is_skipped(self):
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = _expense_row("General")
        session.execute.return_value.scalar.return_value = Decimal("42.500")
        self._install(session)

        resp = client.get(f"{URL}?branch_id=29&item_id=10&indent_type=Open&expense_type_id=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["validation_logic"] == 3
        assert data["branch_stock"] == 42.5
        assert data["errors"] == [] and data["warnings"] == []

    def test_logic1_item_without_minmax_row_still_reports_stock(self):
        session = MagicMock()
        # expense lookup, then NO row from the min/max view, then no Open-type FY indent
        session.execute.return_value.fetchone.side_effect = [_expense_row("General"), None, None]
        session.execute.return_value.scalar.return_value = 17
        self._install(session)

        resp = client.get(f"{URL}?branch_id=29&item_id=10&indent_type=Regular&expense_type_id=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["validation_logic"] == 1
        assert data["branch_stock"] == 17.0
        # no min/max configured -> free entry, exactly as before
        assert data["has_minmax"] is False
        assert data["errors"] == []

    def test_stock_query_is_scoped_to_branch_and_item(self):
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = _expense_row("General")
        session.execute.return_value.scalar.return_value = 0
        self._install(session)

        client.get(f"{URL}?branch_id=29&item_id=10&indent_type=Open&expense_type_id=1")
        stock_calls = [
            (str(call.args[0]), call.args[1] if len(call.args) > 1 else call.kwargs.get("params"))
            for call in session.execute.call_args_list
            if "AS branch_stock" in str(call.args[0])
        ]
        assert len(stock_calls) == 1
        sql, params = stock_calls[0]
        assert params == {"branch_id": 29, "item_id": 10}
        assert "pi.sr_status = 3" in sql and "pid.active" in sql

    def test_null_stock_reads_as_zero(self):
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = _expense_row("General")
        session.execute.return_value.scalar.return_value = None
        self._install(session)

        resp = client.get(f"{URL}?branch_id=29&item_id=10&indent_type=Open&expense_type_id=1")
        assert resp.status_code == 200
        assert resp.json()["branch_stock"] == 0.0
