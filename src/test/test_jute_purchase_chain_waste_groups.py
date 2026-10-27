"""
Owner ruling 2026-09-25 (D1/D2/D6): the jute purchase chain — Jute PO, Jute
Gate Entry, Jute Quality Check (material inspection), Jute MR — accepts items of
item_grp_mst.item_type_id 3 (jute waste) as well as 2 (raw jute).

Two group shapes must come back from one query:
  * type 2 — root -> SUBgroup -> items (qualities); the pickable row is the
    subgroup, with its recursive path name;
  * type 3 — the root holds its items DIRECTLY; the pickable row is the root.
The items endpoint selects item_mst by item_grp_id, so it resolves both.

The MR has no picker query of its own (it takes actual_item_id as-is) and the
market rate master stays raw-jute only (D6). Structural SQL checks plus the
three setup endpoints through TestClient with a mocked session.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import JUTE_WASTE_ITEM_TYPE_ID, RAW_JUTE_ITEM_TYPE_ID
from src.juteProcurement.marketRateQueries import get_raw_jute_items_query
from src.juteProcurement.query import (
    get_items_by_jute_group_query,
    get_jute_groups_query,
    get_jute_items_query,
    get_jute_mr_line_for_issue_query,
    get_jute_mr_line_items_query,
    get_jute_qualities_by_item_query,
)
from src.main import app
from src.masters.batchPlanMaster import get_jute_items_query as batch_plan_groups_query

client = TestClient(app)

TYPE_FILTER = f"ig.item_type_id IN ({RAW_JUTE_ITEM_TYPE_ID}, {JUTE_WASTE_ITEM_TYPE_ID})"


class TestGroupQuery:
    def test_purchase_chain_groups_accept_raw_jute_and_jute_waste(self):
        sql = str(get_jute_groups_query())
        assert TYPE_FILTER in sql
        assert "ig.item_type_id = 2" not in sql
        assert ":co_id" in sql

    def test_anchor_is_the_groups_own_type_not_a_parent_walk(self):
        """A type-3 root has no parent; it must be pickable in its own right."""
        sql = str(get_jute_groups_query())
        anchor = sql.split("UNION ALL")[0]
        assert "JOIN item_grp_mst parent" not in anchor
        assert "parent_grp_id" in anchor  # carried, not filtered on
        assert "WHERE ig.parent_grp_id" not in anchor

    def test_row_shape_is_item_grp_id_code_name(self):
        """What every FE picker (PO / Gate Entry / QC) reads off `jute_groups`."""
        sql = str(get_jute_groups_query())
        assert "gp.target_id AS item_grp_id" in sql
        assert "gp.item_grp_code_path AS item_grp_code" in sql
        assert "gp.item_grp_name_path AS item_grp_name" in sql

    def test_legacy_alias_is_the_same_query(self):
        assert str(get_jute_items_query()) == str(get_jute_groups_query())

    def test_matches_the_batch_plan_master_shape(self):
        """batchPlanMaster already accepted both types; the purchase chain now
        anchors on the same predicate, so the two pickers offer the same groups."""
        assert TYPE_FILTER in str(batch_plan_groups_query())
        assert TYPE_FILTER in str(get_jute_groups_query())


class TestItemsQuery:
    def test_items_resolve_by_group_id_with_no_shape_assumption(self):
        """A type-3 root's items hang off the root itself: no parent traversal,
        no item_type predicate, so the same query serves both shapes."""
        sql = str(get_items_by_jute_group_query())
        assert "im.item_grp_id = :item_grp_id" in sql
        assert "item_type_id" not in sql
        assert "parent_grp_id" not in sql
        assert "im.item_id" in sql and "im.item_name AS quality_name" in sql

    def test_legacy_alias_is_the_same_query(self):
        assert str(get_jute_qualities_by_item_query()) == str(get_items_by_jute_group_query())


class TestMrHasNoTypeFilter:
    def test_mr_line_items_display_query_joins_group_for_display_only(self):
        sql = str(get_jute_mr_line_items_query())
        assert "LEFT JOIN item_grp_mst ig" in sql
        assert "item_type_id" not in sql

    def test_mr_line_issue_guard_prices_any_line(self):
        """The guard the FIFO allocator reuses for a purchased-waste slice.
        The item group is joined for the is_waste FLAG only — no type
        predicate, so every line of every type is still priced."""
        sql = " ".join(str(get_jute_mr_line_for_issue_query()).split())
        assert "jml.actual_rate" in sql
        assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in sql
        assert "THEN 1 ELSE 0 END AS is_waste" in sql
        assert "item_type_id" not in sql.partition("WHERE")[2]


class TestMarketRateStaysRawJuteOnly:
    def test_market_rate_picker_is_type_2_only(self):
        """D6 — pinned so a global widen cannot drag it along."""
        sql = str(get_raw_jute_items_query())
        assert f"ig.item_type_id = {RAW_JUTE_ITEM_TYPE_ID}" in sql
        assert TYPE_FILTER not in sql


# ---------------------------------------------------------------------------
# Setup endpoints — the rows come back untouched as `jute_groups`.
# ---------------------------------------------------------------------------

RAW_SUBGROUP = {"item_grp_id": 11, "item_grp_code": "JT > TD5", "item_grp_name": "Jute > TD5 Raw"}
WASTE_ROOT = {"item_grp_id": 42, "item_grp_code": "JW", "item_grp_name": "Jute Waste"}


def _row(cols: dict):
    return SimpleNamespace(_mapping=cols, **cols)


def _setup_router():
    """Answer the widened group query with one row of each shape; everything
    else the setup endpoints run gets an empty list."""

    def _execute(sql, params=None):
        result = MagicMock()
        if TYPE_FILTER in str(sql):
            assert params == {"co_id": 2}
            result.fetchall.return_value = [_row(RAW_SUBGROUP), _row(WASTE_ROOT)]
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None
        return result

    return _execute


class TestSetupEndpointsOfferBothShapes:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self.db = MagicMock()
        self.db.execute.side_effect = _setup_router()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1, "sub": "test_user", "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("url", [
        "/api/jutePO/jute_po_create_setup?co_id=2",
        "/api/juteGateEntry/jute_gate_entry_create_setup?co_id=2",
        "/api/juteMaterialInspection/get_inspection_setup?co_id=2",
    ])
    def test_jute_groups_carry_the_raw_subgroup_and_the_waste_root(self, url):
        response = client.get(url)
        assert response.status_code == 200, response.text
        groups = response.json()["jute_groups"]
        assert groups == [RAW_SUBGROUP, WASTE_ROOT]
        executed = [str(c[0][0]) for c in self.db.execute.call_args_list]
        assert any(TYPE_FILTER in s for s in executed), "widened group query never ran"


class TestQualitiesEndpointsBindTheGroupId:
    """All three items endpoints feed the path segment to `:item_grp_id`.

    The material-inspection one used to bind `item_id` (a pre-existing bind
    mismatch that raised on every call — harmless while the FE called the
    gate-entry endpoint, fatal for any new type-3 wiring that targets it)."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self.db = MagicMock()
        self.db.execute.return_value.fetchall.return_value = [
            _row({"item_id": 900, "quality_name": "Hard Waste", "item_grp_id": 42}),
        ]
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1, "sub": "test_user", "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("url", [
        "/api/jutePO/get_qualities_by_item/42?co_id=2",
        "/api/juteGateEntry/get_qualities_by_item/42?co_id=2",
        "/api/juteMaterialInspection/get_qualities_by_item/42?co_id=2",
    ])
    def test_type_3_root_resolves_its_direct_items(self, url):
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "qualities": [{"item_id": 900, "quality_name": "Hard Waste", "item_grp_id": 42}]
        }
        sql, params = self.db.execute.call_args[0]
        assert "im.item_grp_id = :item_grp_id" in str(sql)
        assert params["item_grp_id"] == 42
