"""
Regression tests for GET /api/itemMaster/item_branch_position (src/masters/items.py).

TKT-000193: the create-indent "Add Items" dialog shows, per item at the chosen
branch, current stock, indent qty not yet ordered, PO qty not yet received and
qty received at the gate but not yet SR-approved. The endpoint takes the ids of
one dialog page and returns one row per id with plain floats.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.main import app

client = TestClient(app)
URL = "/api/itemMaster/item_branch_position"


def _row(d):
    return SimpleNamespace(_mapping=d, **d)


class TestItemBranchPosition:
    @pytest.fixture(autouse=True)
    def _overrides(self):
        self.session = MagicMock()
        self.calls = []

        def execute(query, params=None):
            sql = str(query)
            self.calls.append((sql, params or {}))
            res = MagicMock()
            if "AS pending_indent_qty" in sql:
                res.fetchall.return_value = [
                    _row({
                        "item_id": 7,
                        "cur_stock": Decimal("12.500"),
                        "pending_indent_qty": Decimal("3"),
                        "pending_po_qty": None,
                        "in_process_qty": Decimal("0.250"),
                    }),
                    _row({
                        "item_id": 9,
                        "cur_stock": None,
                        "pending_indent_qty": None,
                        "pending_po_qty": None,
                        "in_process_qty": None,
                    }),
                ]
            else:
                res.fetchall.return_value = []
            return res

        self.session.execute.side_effect = execute
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        yield
        app.dependency_overrides.clear()

    def test_returns_one_row_per_item_as_floats(self):
        resp = client.get(f"{URL}?branch_id=2&item_ids=9,7,7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["branch_id"] == 2
        assert body["data"] == [
            {
                "item_id": 7,
                "cur_stock": 12.5,
                "pending_indent_qty": 3.0,
                "pending_po_qty": 0.0,
                "in_process_qty": 0.25,
            },
            {
                "item_id": 9,
                "cur_stock": 0.0,
                "pending_indent_qty": 0.0,
                "pending_po_qty": 0.0,
                "in_process_qty": 0.0,
            },
        ]

    def test_binds_branch_and_deduplicated_sorted_item_ids(self):
        client.get(f"{URL}?branch_id=29&item_ids=5,3,5,%201")
        position_calls = [p for sql, p in self.calls if "AS pending_indent_qty" in sql]
        assert len(position_calls) == 1
        assert position_calls[0] == {"branch_id": 29, "item_ids": [1, 3, 5]}

    def test_position_sql_covers_all_four_buckets(self):
        client.get(f"{URL}?branch_id=2&item_ids=7")
        sql = next(s for s, _ in self.calls if "AS pending_indent_qty" in s)
        # stock: approved SR balance only
        assert "pi.sr_status = 3" in sql
        # pending PO: receipts net of cancelled inward lines, returns added back
        assert "pid.status_id <> 6" in sql and "vw_drcr_return_qty" in sql
        # in receipt: gate entry done, SR not approved
        assert "pi.sr_status <> 3" in sql
        # indents / POs that are Rejected or Cancelled never count
        assert sql.count("NOT IN (4, 6)") >= 2

    def test_missing_branch_id_400(self):
        resp = client.get(f"{URL}?item_ids=1")
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_invalid_branch_id_400(self):
        assert client.get(f"{URL}?branch_id=abc&item_ids=1").status_code == 400

    def test_missing_item_ids_400(self):
        resp = client.get(f"{URL}?branch_id=2")
        assert resp.status_code == 400
        assert "item_ids" in resp.json()["detail"]
        assert client.get(f"{URL}?branch_id=2&item_ids=,").status_code == 400

    def test_invalid_item_ids_400(self):
        assert client.get(f"{URL}?branch_id=2&item_ids=1,x").status_code == 400

    def test_more_than_50_item_ids_400(self):
        ids = ",".join(str(i) for i in range(1, 52))
        resp = client.get(f"{URL}?branch_id=2&item_ids={ids}")
        assert resp.status_code == 400
        assert "50" in resp.json()["detail"]

