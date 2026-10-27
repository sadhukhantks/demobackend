"""Tests for Jute Market Rate (src/juteProcurement/marketRate.py)."""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.marketRate import creates_cycle
from src.main import app
from src.models.jute import JuteMarketRate, JuteMarketRateSourceMst

client = TestClient(app)


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    row.configure_mock(**mapping)
    return row


ITEMS = [
    _row({"item_id": 5, "item_name": "SN TD-5", "item_grp_id": 1, "item_grp_name": "SEMI NORTHERN"}),
    _row({"item_id": 6, "item_name": "SN TD-6", "item_grp_id": 1, "item_grp_name": "SEMI NORTHERN"}),
]


class TestCreatesCycle:
    def test_direct_self(self):
        assert creates_cycle({}, 5, 5)

    def test_chain_back_to_item(self):
        # 6 <- 5 already (5 derives from 6); making 6 derive from 5 loops
        assert creates_cycle({5: (6, 200.0)}, 6, 5)

    def test_longer_chain(self):
        assert creates_cycle({7: (6, -100.0), 6: (5, -200.0)}, 5, 7)

    def test_no_cycle(self):
        assert not creates_cycle({6: (5, -200.0)}, 7, 6)

    def test_ignores_items_own_old_row(self):
        # 6's current row points at 5; re-pointing 6 at 5 with a new diff is fine
        assert not creates_cycle({6: (5, -200.0)}, 6, 5)


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def overrides(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    def _source(self, co_id=1, active=1):
        src = JuteMarketRateSourceMst(source_id=3, co_id=co_id, source_name="JBA", active=active)
        self.db.get.return_value = src
        return src

    def test_rates_requires_params(self):
        r = client.get("/api/juteMarketRate/rates?co_id=1&source_id=3")
        assert r.status_code == 400
        assert "rate_date" in r.json()["detail"]

    def test_save_rates_rejects_future_date(self):
        self._source()
        r = client.post("/api/juteMarketRate/rates/save", json={
            "co_id": 1, "source_id": 3, "rate_date": str(date.today() + timedelta(days=1)),
            "rates": [{"item_id": 5, "rate": 7000}],
        })
        assert r.status_code == 400

    def test_save_rates_rejects_other_company_source(self):
        self._source(co_id=2)
        r = client.post("/api/juteMarketRate/rates/save", json={
            "co_id": 1, "source_id": 3, "rate_date": str(date.today()), "rates": [],
        })
        assert r.status_code == 404

    def test_save_rates_rejects_non_jute_item(self):
        self._source()
        self.db.execute.return_value.fetchall.return_value = ITEMS
        r = client.post("/api/juteMarketRate/rates/save", json={
            "co_id": 1, "source_id": 3, "rate_date": str(date.today()),
            "rates": [{"item_id": 99, "rate": 7000}],
        })
        assert r.status_code == 400
        assert "99" in r.json()["detail"]

    def test_save_rates_upserts_and_soft_deletes_blanked(self):
        self._source()
        self.db.execute.return_value.fetchall.return_value = ITEMS
        old6 = JuteMarketRate(item_id=6, rate=6800.0, active=1)
        self.db.query.return_value.filter.return_value = [old6]
        r = client.post("/api/juteMarketRate/rates/save", json={
            "co_id": 1, "source_id": 3, "rate_date": str(date.today()),
            "rates": [{"item_id": 5, "rate": 7000}, {"item_id": 6, "rate": None}],
        })
        assert r.status_code == 200, r.text
        assert r.json() == {"data": {"saved": 1}}
        added = self.db.add.call_args[0][0]
        assert (added.item_id, added.rate, added.source_id) == (5, 7000.0, 3)
        assert old6.active == 0
        self.db.commit.assert_called_once()

    def test_rate_diff_same_item_rejected(self):
        r = client.post("/api/juteMarketRate/rate_diff/save", json={
            "co_id": 1, "item_id": 5, "base_item_id": 5, "diff_amount": 0,
            "effective_from": "2026-09-01",
        })
        assert r.status_code == 400

    def test_rate_diff_cycle_rejected(self):
        # first execute -> items, second -> diffs in force (5 derives from 6)
        items_res, diff_res = MagicMock(), MagicMock()
        items_res.fetchall.return_value = ITEMS
        diff_res.fetchall.return_value = [_row({"item_id": 5, "base_item_id": 6, "diff_amount": 200.0})]
        self.db.execute.side_effect = [items_res, diff_res]
        r = client.post("/api/juteMarketRate/rate_diff/save", json={
            "co_id": 1, "item_id": 6, "base_item_id": 5, "diff_amount": -200,
            "effective_from": "2026-09-01",
        })
        assert r.status_code == 400
        assert "circular" in r.json()["detail"]

    def test_source_duplicate_name_rejected(self):
        self.db.query.return_value.filter.return_value.first.return_value = JuteMarketRateSourceMst(
            source_id=4, co_id=1, source_name="JBA", active=1
        )
        r = client.post("/api/juteMarketRate/sources/save", json={"co_id": 1, "source_name": " JBA "})
        assert r.status_code == 400
        assert "already exists" in r.json()["detail"]
