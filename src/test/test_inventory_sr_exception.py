"""Tests for the SR exception report query (inventory reports)."""

from src.inventory.reportQueries import get_sr_exception_report_query


def test_sr_exception_query_binds_and_rules():
    sql = str(get_sr_exception_report_query())
    # Bind parameters the endpoint supplies — a rename breaks execution.
    for bind in (":co_id", ":branch_id", ":search_like"):
        assert bind in sql
    # Movement rules must match the stock report family.
    assert "pi.sr_status = 3" in sql
    assert "pid.active = 1" in sql
    assert "ih.status_id IN (1, 3, 5, 20)" in sql
    assert "vw_drcr_return_qty" in sql
    # Exception filter: only negative balances (with the dust threshold).
    assert "< -0.01" in sql
