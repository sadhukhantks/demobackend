"""Tests for the batch plan master jute-group dropdown query."""

from src.masters.batchPlanMaster import get_jute_items_query


def test_jute_items_query_accepts_jute_and_wastage():
    """
    Raw jute (2) and jute wastage (3) groups are selected by their OWN
    item_type_id — parent-group traversal was retired when the redundant
    JUTE root groups were removed (2026-08-21 cleanup).
    """
    sql = str(get_jute_items_query())

    assert "ig.item_type_id IN (2, 3)" in sql
    # No parent traversal in the anchor any more.
    assert "JOIN item_grp_mst parent" not in sql
    assert ":co_id" in sql
