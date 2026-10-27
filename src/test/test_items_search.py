"""
Regression tests for GET /api/itemMaster/item_search (src/masters/items.py).

TKT-000057: the purchaseable/saleable filter used to be applied in Python
AFTER LIMIT/OFFSET, which shrank pages and made `total` wrong. It must now be
bound into both the count and list SQL, and no Python post-filter may run.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.main import app

client = TestClient(app)
URL = "/api/itemMaster/item_search"


def _row(d):
    return SimpleNamespace(_mapping=d, **d)


class TestItemSearchFilter:
    @pytest.fixture(autouse=True)
    def _overrides(self):
        self.session = MagicMock()
        self.calls = []

        def execute(query, params=None):
            sql = str(query)
            self.calls.append((sql, params or {}))
            res = MagicMock()
            if "COUNT(1) AS total" in sql:
                res.fetchone.return_value = SimpleNamespace(total=7)
            elif "FROM item_mst AS im" in sql:
                # one saleable and one non-saleable row come back from SQL
                res.fetchall.return_value = [
                    _row({"item_id": 1, "item_grp_id": 5, "saleable": 1, "purchaseable": 0}),
                    _row({"item_id": 2, "item_grp_id": 5, "saleable": 0, "purchaseable": 1}),
                ]
            else:
                res.fetchall.return_value = []
            return res

        self.session.execute.side_effect = execute
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        yield
        app.dependency_overrides.clear()

    def _params(self, marker):
        return [p for sql, p in self.calls if marker in sql]

    def test_saleable_filter_bound_into_sql_not_python(self):
        resp = client.get(f"{URL}?co_id=1&filter=saleable&limit=50")
        assert resp.status_code == 200
        body = resp.json()
        # no Python post-filter: whatever SQL returns is returned as-is
        assert [i["item_id"] for i in body["data"]] == [1, 2]
        assert body["total"] == 7
        for p in self._params("FROM item_mst AS im"):
            assert p["saleable_only"] == 1
            assert p["purchaseable_only"] == 0
        # both count and list queries ran with the flag
        assert len(self._params("saleable_only = 0 OR im.saleable = 1")) == 2

    def test_no_filter_binds_zero_flags(self):
        resp = client.get(f"{URL}?co_id=1")
        assert resp.status_code == 200
        for p in self._params("FROM item_mst AS im"):
            assert p["saleable_only"] == 0
            assert p["purchaseable_only"] == 0

    def test_missing_co_id_400(self):
        assert client.get(URL).status_code == 400
