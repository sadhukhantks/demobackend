"""Tests for the Outstanding PO Position report
(src/juteProcurement/po_position.py, GET /api/juteReports/po-outstanding-position)
and its SQL builder get_jute_po_outstanding_position_query() in
src/juteProcurement/reportQueries.py.

Pure-builder tests cover the weight-share arithmetic, the balance filter, the
quantity fallback, value pro-rating, ageing/overdue, and the quality/supplier
roll-ups. Endpoint tests cover parameter validation and a happy path through
TestClient with the DB and auth dependencies overridden by a MagicMock — no
database. The SQL builder gets bind-name and predicate assertions.
"""

import re
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import (
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    JUTE_PO_CLOSED_STATUS,
    JUTE_PO_INACTIVE_STATUSES,
)
from src.juteProcurement.po_settlement import SettleRule
from src.juteProcurement.reportQueries import get_jute_po_outstanding_position_query
from src.juteProcurement.po_position import (
    BAD_BRANCH_MSG,
    build_po_position,
    router as po_position_router,
)
from src.main import app

PREFIX = "/api/juteReports"
URL = f"{PREFIX}/po-outstanding-position"

if URL not in {getattr(r, "path", None) for r in app.routes}:
    app.include_router(po_position_router, prefix=PREFIX, tags=["jute-procurement-reports"])

client = TestClient(app)

AS_ON = date(2026, 9, 24)


def _row(**over):
    """A PO-item row as the query returns it (dev3-style kg weights)."""
    base = {
        "jute_po_id": 13260,
        "po_label": "EJM/F/JPO/26-27/00047",
        "po_date": date(2026, 9, 1),
        "delivery_days": 10,
        "po_status": "APPROVED",
        "party_name": "SUKUMAR SAHA",
        "mukam": "JALANGI",
        "vehicle_quantity": 5,
        "received_lorries": 2,
        "po_weight": 50000.0,
        "po_received_weight": 20000.0,
        "item_id": 269848,
        "item_code": "3",
        "item_name": "D TD 6",
        "po_uom": "BALE",
        "po_rate": 13400.0,
        "ordered_qty": 250.0,
        "ordered_value": 5025000.0,
        "weight_share": 0.75,
        "received_qty": 100.0,
        "received_wt": 15000.0,
        "mr_count": 2,
        "last_receipt_date": date(2026, 9, 20),
        "returned_lorries": 0,
        "returned_weight": 0.0,
    }
    base.update(over)
    return base


# =============================================================================
# build_po_position — one row
# =============================================================================


class TestDetailRow:
    def test_weight_share_arithmetic(self):
        d = build_po_position([_row()], AS_ON)["detail"][0]
        assert d["ordered_wt"] == 37500.0          # 50,000 × 0.75
        assert d["received_wt"] == 15000.0
        assert d["balance_wt"] == 22500.0
        assert d["balance_pct"] == 60.0
        # value pro-rated on the unreceived weight fraction, no rate unit assumed
        assert d["balance_value"] == 3015000.0     # 5,025,000 × 0.6
        assert d["ordered_value"] == 5025000.0

    def test_quantities_and_lorries(self):
        d = build_po_position([_row()], AS_ON)["detail"][0]
        assert (d["ordered_qty"], d["received_qty"], d["balance_qty"]) == (250.0, 100.0, 150.0)
        assert (d["vehicle_quantity"], d["received_lorries"], d["pending_lorries"]) == (5, 2, 3)
        assert d["po_pending_weight"] == 30000.0

    def test_ageing_and_overdue(self):
        d = build_po_position([_row()], AS_ON)["detail"][0]
        assert d["po_date"] == "2026-09-01"
        assert d["age_days"] == 23
        assert d["due_date"] == "2026-09-11"
        assert d["overdue"] is True
        assert d["last_receipt_date"] == "2026-09-20"

    def test_not_overdue_when_due_today_or_later(self):
        d = build_po_position([_row(po_date=date(2026, 9, 20), delivery_days=4)], AS_ON)["detail"][0]
        assert d["due_date"] == "2026-09-24" and d["overdue"] is False
        d = build_po_position([_row(delivery_days=None)], AS_ON)["detail"][0]
        assert d["due_date"] is None and d["overdue"] is False

    def test_rate_and_rate_range_pass_through(self):
        """TKT-208: the PO Detail grid shows each quality's rate; po_rate_min
        lets it show a range when one quality sits on lines at different rates."""
        d = build_po_position([_row(po_rate_min=13400.0)], AS_ON)["detail"][0]
        assert (d["po_rate"], d["po_rate_min"]) == (13400.0, 13400.0)
        d = build_po_position([_row(po_rate=6375.0, po_rate_min=5875.456)], AS_ON)["detail"][0]
        assert (d["po_rate"], d["po_rate_min"]) == (6375.0, 5875.46)
        # a row without the column (older query shape) stays null, never 0
        d = build_po_position([_row()], AS_ON)["detail"][0]
        assert d["po_rate_min"] is None

    def test_fully_received_item_is_dropped(self):
        out = build_po_position([_row(received_wt=37500.0)], AS_ON)
        assert out["detail"] == [] and out["by_quality"] == [] and out["by_supplier"] == []
        assert out["totals"]["po_count"] == 0

    def test_over_receipt_clamps_to_zero_and_drops(self):
        assert build_po_position([_row(received_wt=40000.0)], AS_ON)["detail"] == []

    def test_quantity_fallback_when_weight_share_unknown(self):
        """Legacy lines with no quantity/percentage on a multi-line PO: judge
        on drums/bales instead of hiding the item."""
        d = build_po_position(
            [_row(weight_share=None, ordered_qty=90.0, received_qty=30.0, received_wt=0.0)], AS_ON
        )["detail"][0]
        assert d["ordered_wt"] is None and d["balance_wt"] is None
        assert d["balance_qty"] == 60.0
        assert d["balance_pct"] == pytest.approx(66.67)
        assert d["balance_value"] == pytest.approx(5025000.0 * 60 / 90, abs=0.01)

    def test_quantity_fallback_fully_received_is_dropped(self):
        rows = [_row(weight_share=None, ordered_qty=90.0, received_qty=90.0)]
        assert build_po_position(rows, AS_ON)["detail"] == []

    def test_nothing_ordered_at_all_is_dropped(self):
        rows = [_row(weight_share=None, po_weight=None, ordered_qty=0.0, received_qty=0.0)]
        assert build_po_position(rows, AS_ON)["detail"] == []

    def test_missing_value_gives_null_balance_value(self):
        d = build_po_position([_row(ordered_value=None)], AS_ON)["detail"][0]
        assert d["ordered_value"] is None and d["balance_value"] is None
        assert d["balance_wt"] == 22500.0

    def test_po_label_falls_back_to_id(self):
        d = build_po_position([_row(po_label=None)], AS_ON)["detail"][0]
        assert d["po_label"] == "PO-13260"

    def test_no_unit_conversion(self):
        """A legacy quintal-weight PO comes back in quintals, untouched."""
        d = build_po_position([_row(po_weight=97.2, weight_share=1.0, received_wt=0.0)], AS_ON)["detail"][0]
        assert d["ordered_wt"] == 97.2 and d["balance_wt"] == 97.2


class TestSettlementFlags:
    """days_since_receipt / stale / settle_suggest (po-settlement-spec.md §6)."""

    def test_days_since_receipt(self):
        d = build_po_position([_row()], AS_ON)["detail"][0]
        assert d["days_since_receipt"] == 4          # 20 Sep -> 24 Sep
        d = build_po_position([_row(last_receipt_date=None)], AS_ON)["detail"][0]
        assert d["days_since_receipt"] is None

    def test_stale_needs_overdue_and_no_recent_receipt(self):
        # overdue, receipt 4 days ago, default 30 days -> not stale
        assert build_po_position([_row()], AS_ON)["detail"][0]["stale"] is False
        # same PO under a 3-day rule -> stale
        rule = SettleRule(stale_days=3)
        assert build_po_position([_row()], AS_ON, rule)["detail"][0]["stale"] is True
        # overdue and never received -> stale
        assert build_po_position([_row(last_receipt_date=None)], AS_ON)["detail"][0]["stale"] is True
        # not overdue -> never stale, however old the receipt
        d = build_po_position([_row(delivery_days=60, last_receipt_date=None)], AS_ON)["detail"][0]
        assert d["overdue"] is False and d["stale"] is False

    def test_settle_suggest_from_header_receipts(self):
        # 2 of 5 lorries, 20,000 of 50,000 kg -> not settled
        assert build_po_position([_row()], AS_ON)["detail"][0]["settle_suggest"] is False
        # all lorries in -> suggest (lorry rule)
        assert build_po_position([_row(received_lorries=5)], AS_ON)["detail"][0]["settle_suggest"] is False  # lorry rule off by default
        assert build_po_position([_row(received_lorries=5)], AS_ON, SettleRule(lorry_rule_enabled=True))["detail"][0]["settle_suggest"] is True
        # ... unless the company turned the lorry rule off
        rule = SettleRule(lorry_rule_enabled=False)
        assert build_po_position([_row(received_lorries=5)], AS_ON, rule)["detail"][0]["settle_suggest"] is False
        # weight within half a lorry (50,000 / 5 = 10,000 -> 5,000) -> suggest
        assert build_po_position([_row(po_received_weight=45000.0)], AS_ON)["detail"][0]["settle_suggest"] is True
        assert build_po_position([_row(po_received_weight=44999.0)], AS_ON)["detail"][0]["settle_suggest"] is False
        # a whole-lorry rule
        rule = SettleRule(tolerance_lorry_fraction=1.0)
        assert build_po_position([_row(po_received_weight=40000.0)], AS_ON, rule)["detail"][0]["settle_suggest"] is True

    def test_returned_lorries_are_shown_not_received(self):
        d = build_po_position([_row(received_lorries=4, returned_lorries=1, returned_weight=9000.0)], AS_ON)["detail"][0]
        assert (d["returned_lorries"], d["returned_weight"]) == (1, 9000.0)
        assert (d["received_lorries"], d["pending_lorries"]) == (4, 1)
        assert d["settle_suggest"] is False

    def test_lorry_and_weight_totals_are_per_po(self):
        rows = [
            _row(received_lorries=2, returned_lorries=1, returned_weight=9000.0),
            _row(item_id=1, item_name="X", weight_share=0.25, received_wt=0.0,
                 received_lorries=2, returned_lorries=1, returned_weight=9000.0),      # same PO, no double count
            _row(jute_po_id=2, vehicle_quantity=3, received_lorries=0, po_weight=30000.0, po_received_weight=0.0),
        ]
        out = build_po_position(rows, AS_ON)
        t = out["totals"]
        assert (t["order_lorries"], t["received_lorries"], t["pending_lorries"], t["returned_lorries"]) == (8, 2, 6, 1)
        assert (t["order_weight"], t["received_weight"], t["pending_weight"], t["returned_weight"]) == (80000.0, 20000.0, 60000.0, 9000.0)
        s = out["by_supplier"][0]  # both POs are Saha's
        assert (s["order_lorries"], s["returned_lorries"], s["pending_weight"]) == (8, 1, 60000.0)

    def test_totals_count_pos_not_rows(self):
        rows = [
            _row(received_lorries=5),                                           # 13260 suggest
            _row(item_id=1, item_name="X", weight_share=0.25, received_wt=0.0, received_lorries=5),  # same PO
            _row(jute_po_id=2, last_receipt_date=None),                         # stale, not suggest
            _row(jute_po_id=3, delivery_days=60, last_receipt_date=None),       # neither
        ]
        t = build_po_position(rows, AS_ON, SettleRule(lorry_rule_enabled=True))["totals"]
        assert t["po_count"] == 3
        assert t["settle_suggest_pos"] == 1 and t["stale_pos"] == 1
        assert t["overdue_pos"] == 2


# =============================================================================
# build_po_position — roll-ups
# =============================================================================


ROWS = [
    _row(),  # D TD 6, 22,500 balance, Saha
    _row(item_id=269847, item_code="2", item_name="D TD 5", po_rate=13500.0, ordered_qty=83.3333,
         ordered_value=1687500.0, weight_share=0.25, received_qty=0.0, received_wt=0.0),  # 12,500 balance
    _row(jute_po_id=13300, po_label="EJM/F/JPO/26-27/00050", po_date=date(2026, 9, 15),
         party_name="KISHAN KOTHARI", po_weight=20000.0, po_received_weight=0.0, received_lorries=0,
         ordered_qty=100.0, ordered_value=2680000.0, weight_share=1.0, po_rate=13400.0,
         received_qty=0.0, received_wt=0.0, delivery_days=30),  # D TD 6, 20,000 balance, not overdue
    _row(jute_po_id=13301, party_name="KISHAN KOTHARI", received_wt=37500.0),  # fully received → dropped
]


class TestRollups:
    @pytest.fixture(scope="class")
    def out(self):
        return build_po_position(ROWS, AS_ON)

    def test_detail_keeps_query_order_and_drops_received(self, out):
        assert [d["jute_po_id"] for d in out["detail"]] == [13260, 13260, 13300]

    def test_by_quality(self, out):
        assert [q["item_name"] for q in out["by_quality"]] == ["D TD 5", "D TD 6"]
        dtd6 = out["by_quality"][1]
        assert dtd6["po_count"] == 2
        assert dtd6["ordered_wt"] == 57500.0 and dtd6["received_wt"] == 15000.0
        assert dtd6["balance_wt"] == 42500.0
        assert dtd6["balance_pct"] == pytest.approx(73.91)
        assert dtd6["balance_value"] == 3015000.0 + 2680000.0
        assert dtd6["avg_rate"] == 13400.0        # both lines at 13,400
        assert dtd6["overdue_pos"] == 1           # only 13260 is past due

    def test_by_quality_weighted_rate(self, out):
        dtd5 = out["by_quality"][0]
        assert dtd5["avg_rate"] == 13500.0 and dtd5["balance_wt"] == 12500.0

    def test_by_supplier_ordered_by_balance_desc(self, out):
        assert [s["party_name"] for s in out["by_supplier"]] == ["SUKUMAR SAHA", "KISHAN KOTHARI"]
        saha = out["by_supplier"][0]
        assert saha["po_count"] == 1 and saha["balance_wt"] == 35000.0
        assert saha["oldest_po_date"] == "2026-09-01" and saha["overdue_pos"] == 1
        kothari = out["by_supplier"][1]
        assert kothari["po_count"] == 1 and kothari["balance_wt"] == 20000.0  # 13301 dropped

    def test_totals(self, out):
        t = out["totals"]
        assert t["po_count"] == 2 and t["overdue_pos"] == 1
        assert t["ordered_wt"] == 70000.0 and t["received_wt"] == 15000.0
        assert t["balance_wt"] == 55000.0
        assert t["balance_pct"] == pytest.approx(78.57)
        assert t["balance_value"] == pytest.approx(3015000.0 + 1687500.0 + 2680000.0)
        assert t["avg_rate"] == pytest.approx((22500 * 13400 + 12500 * 13500 + 20000 * 13400) / 55000, abs=0.01)

    def test_empty_input(self):
        out = build_po_position([], AS_ON)
        assert out["detail"] == [] and out["by_quality"] == [] and out["by_supplier"] == []
        assert out["totals"]["po_count"] == 0 and out["totals"]["balance_wt"] == 0.0
        assert out["totals"]["balance_pct"] is None and out["totals"]["avg_rate"] is None


# =============================================================================
# Endpoint
# =============================================================================


class TestEndpoint:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self.session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        yield
        app.dependency_overrides.clear()

    def _branch_ok(self):
        branch = MagicMock()
        branch._mapping = {"branch_id": 2, "branch_name": "Factory"}
        return branch

    def test_missing_co_id_returns_400(self):
        r = client.get(URL, params={"branch_id": 2})
        assert r.status_code == 400 and "co_id" in r.json()["detail"]

    def test_missing_branch_id_returns_400(self):
        r = client.get(URL, params={"co_id": 1})
        assert r.status_code == 400 and "branch_id" in r.json()["detail"]

    def test_bad_as_on_returns_400(self):
        r = client.get(URL, params={"co_id": 1, "branch_id": 2, "as_on": "24-09-2026"})
        assert r.status_code == 400 and "as_on" in r.json()["detail"]

    def test_unknown_branch_returns_400(self):
        self.session.execute.return_value.fetchone.return_value = None
        r = client.get(URL, params={"co_id": 1, "branch_id": 999})
        assert r.status_code == 400 and r.json()["detail"] == BAD_BRANCH_MSG

    def test_happy_path_binds_as_on(self):
        row = MagicMock()
        row._mapping = _row()
        self.session.execute.return_value.fetchone.return_value = self._branch_ok()
        self.session.execute.return_value.fetchall.return_value = [row]
        r = client.get(URL, params={"co_id": 1, "branch_id": 2, "as_on": "2026-09-24"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["as_on"] == "2026-09-24" and data["branch_name"] == "Factory"
        assert data["totals"]["balance_wt"] == 22500.0
        assert len(data["detail"]) == 1 and data["detail"][0]["po_label"] == "EJM/F/JPO/26-27/00047"
        binds = self.session.execute.call_args_list[-1][0][1]
        assert binds == {"co_id": 1, "branch_id": 2, "as_on": date(2026, 9, 24)}

    def test_as_on_defaults_to_today(self):
        self.session.execute.return_value.fetchone.return_value = self._branch_ok()
        self.session.execute.return_value.fetchall.return_value = []
        r = client.get(URL, params={"co_id": 1, "branch_id": 2})
        assert r.status_code == 200
        assert r.json()["data"]["as_on"] == date.today().isoformat()

    def test_response_carries_default_rule_when_company_has_none(self):
        self.session.execute.return_value.fetchone.return_value = self._branch_ok()
        self.session.execute.return_value.fetchall.return_value = []
        r = client.get(URL, params={"co_id": 1, "branch_id": 2})
        assert r.json()["data"]["rule"] == {
            "lorry_rule_enabled": False, "tolerance_lorry_fraction": 0.5, "stale_days": 30, "is_default": True,
        }
        t = r.json()["data"]["totals"]
        assert t["stale_pos"] == 0 and t["settle_suggest_pos"] == 0

    def test_company_rule_drives_flags(self):
        rule_row = MagicMock()
        rule_row._mapping = {"lorry_rule_enabled": 0, "tolerance_lorry_fraction": 1.0, "stale_days": 2}
        self.session.execute.return_value.fetchone.side_effect = [self._branch_ok(), rule_row]
        row = MagicMock()
        row._mapping = _row(received_lorries=5, po_received_weight=40000.0)  # one lorry (10,000) short
        self.session.execute.return_value.fetchall.return_value = [row]
        r = client.get(URL, params={"co_id": 1, "branch_id": 2, "as_on": "2026-09-24"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["rule"] == {"lorry_rule_enabled": False, "tolerance_lorry_fraction": 1.0, "stale_days": 2, "is_default": False}
        d = data["detail"][0]
        assert d["settle_suggest"] is True     # whole-lorry tolerance, lorry rule off
        assert d["stale"] is True              # receipt 4 days ago >= 2
        assert data["totals"]["settle_suggest_pos"] == 1 and data["totals"]["stale_pos"] == 1
        # the rule is read BEFORE the report query, whose binds stay the last call
        assert self.session.execute.call_args_list[-1][0][1] == {"co_id": 1, "branch_id": 2, "as_on": date(2026, 9, 24)}
        assert "jute_po_settle_rule" in str(self.session.execute.call_args_list[-2][0][0])

    def test_report_works_without_settlement_table(self):
        """A tenant without the migration: the rule lookup fails, defaults apply."""
        branch = self._branch_ok()
        calls = {"n": 0}

        def _execute(stmt, params=None):
            calls["n"] += 1
            if "jute_po_settle_rule" in str(stmt):
                raise Exception(1146, "Table 'sls.jute_po_settle_rule' doesn't exist")
            res = MagicMock()
            res.fetchone.return_value = branch
            res.fetchall.return_value = []
            return res

        self.session.execute.side_effect = _execute
        r = client.get(URL, params={"co_id": 1, "branch_id": 2})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["rule"]["is_default"] is True


# =============================================================================
# SQL builder
# =============================================================================


def _bound_names(query):
    return set(re.findall(r"(?<!:):([a-z_][a-z0-9_]*)", str(query)))


class TestQuery:
    def test_bind_params(self):
        assert _bound_names(get_jute_po_outstanding_position_query()) == {"co_id", "branch_id", "as_on"}

    def test_excludes_closed_and_inactive_pos(self):
        sql = str(get_jute_po_outstanding_position_query())
        excluded = ", ".join(str(s) for s in JUTE_PO_INACTIVE_STATUSES + (JUTE_PO_CLOSED_STATUS,))
        assert f"jp.status_id NOT IN ({excluded})" in sql

    def test_as_on_cuts_pos_receipts_and_gate(self):
        sql = str(get_jute_po_outstanding_position_query())
        assert "jp.po_date <= :as_on" in sql
        assert "jm.jute_gate_entry_date <= :as_on" in sql
        assert "COALESCE(jm.jute_gate_entry_date" in sql and "<= :as_on" in sql

    def test_gate_lorries_use_the_settlement_receipt_predicate(self):
        """Header lorries/weight exclude rejected/cancelled/draft/returned MRs,
        exactly like recompute_settlement — otherwise settle_suggest disagrees
        with the engine (dev3 PO 13163: one rejected lorry counted as received)."""
        sql = str(get_jute_po_outstanding_position_query())
        gate = sql[sql.index("gate AS ("):sql.index("SELECT\n", sql.index("gate AS ("))]
        excluded = ", ".join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)
        assert "jm.jute_gate_entry_no IS NOT NULL" in gate
        assert f"jm.status_id NOT IN ({excluded})" in gate
        # returned lorries (48) are counted separately, never as received
        assert "jm.is_returned = 1 AND jm.status_id NOT IN (4, 6, 21) THEN 1" in gate and "AS returned_lorries" in gate and "AS returned_weight" in gate

    def test_matches_receipts_by_item_not_po_li_id(self):
        sql = str(get_jute_po_outstanding_position_query())
        assert "COALESCE(mrli.actual_item_id, mrli.challan_item_id)" in sql
        assert "jute_po_li_id" not in sql

    def test_selects_min_and_max_line_rate_per_item(self):
        """TKT-208: rate range per PO item (same quality on several lines)."""
        sql = str(get_jute_po_outstanding_position_query())
        items = sql[sql.index("po_items AS ("):sql.index("received AS (")]
        assert "MAX(pl.rate) AS po_rate," in items
        assert "MIN(pl.rate) AS po_rate_min," in items
        assert "pi.po_rate_min," in sql

    def test_scopes_company_through_branch_mst(self):
        sql = str(get_jute_po_outstanding_position_query())
        assert "INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id" in sql
        assert "bm.co_id = :co_id" in sql

    def test_no_unit_conversion_on_weights(self):
        """Weight share is a fraction; nothing scales kg <-> quintal."""
        sql = str(get_jute_po_outstanding_position_query())
        assert "/ 100\n" not in sql.replace("percentage, 0) / 100", "")
        assert "* 100" not in sql
