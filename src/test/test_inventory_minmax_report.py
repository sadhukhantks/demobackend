"""Stores Min-Max report — shortfall column, shortest-of-stock ordering, and the
General-Production-Maintenance / Overhauling-Capital stock split (TKT-000114)."""

import inspect
from types import SimpleNamespace

from src.inventory.reportQueries import get_inventory_minmax_report_query
from src.inventory.reports import get_inventory_minmax_report


def _run_handler(**row):
    """Call the handler over one canned SQL row and return its data dict."""
    base = {
        "item_id": 1, "item_code": "A-1", "item_name": "Bolt",
        "item_grp_name": "Hardware", "uom_name": "NOS",
        "minqty": None, "maxqty": None, "min_order_qty": None, "lead_time": None,
        "current_qty": 0, "stock_gpm_qty": 0, "stock_oc_qty": 0,
        "below_min_qty": 0, "pending_indent_qty": 0, "pending_po_qty": 0,
    }
    base.update(row)
    db = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
        fetchall=lambda: [SimpleNamespace(_mapping=base)]
    ))
    resp = get_inventory_minmax_report(
        request=None, db=db, token_data={"user_id": 1},
        co_id=1, branch_id=1, date_to="2026-08-31",
    )
    return resp["data"][0]


def test_minmax_query_binds_and_movement_rules():
    sql = str(get_inventory_minmax_report_query())
    for bind in (":co_id", ":branch_id", ":date_to", ":item_grp_id",
                 ":search_like", ":limit", ":offset"):
        assert bind in sql, bind
    # Stock definition stays in step with the rest of the stock report family.
    assert "pi.sr_status = 3" in sql
    assert "ih.status_id IN (1, 3, 5, 20)" in sql
    assert "vw_drcr_return_qty" in sql
    # Only items that HAVE a min-max record are reported (driven off the master).
    assert "FROM item_minmax_mst AS mm" in sql
    assert "mm.active = 1" in sql


def test_below_min_shortfall_is_selected_and_leads_the_order():
    """The shortfall must be an ORDERABLE SQL expression, not a Python
    post-pass: sorting the fetched page would only reorder whatever rows the
    LIMIT already picked, so a below-min item on page 2 would never surface."""
    sql = str(get_inventory_minmax_report_query())
    assert "AS below_min_qty" in sql
    assert "ORDER BY below_min_qty DESC" in sql
    # The shortfall leads the ORDER BY — group/name only break ties behind it.
    order = sql[sql.index("ORDER BY below_min_qty DESC"):]
    assert order.index("below_min_qty") < order.index("item_grp_name")


def test_no_min_and_float_dust_are_not_shortfalls():
    """minqty NULL means 'no level set', not 'short by the whole stock'. And the
    stock CTE sums DOUBLEs, so a fully-issued item reads -2.4e-07 — without the
    ROUND that phantom shortfall sorts above every genuinely short item."""
    sql = str(get_inventory_minmax_report_query())
    assert "CASE WHEN mm.minqty IS NULL THEN 0" in sql
    assert "ROUND(GREATEST(" in sql
    assert "mm.minqty - (COALESCE(st.net, 0) - COALESCE(oc.net, 0))" in sql


def test_stock_split_buckets_are_selected_and_sum_to_total():
    """B is the matched Overhauling/Capital slice and A is the remainder of the
    SAME total, so A + B = current_qty by construction — no third query, and
    unclassifiable stock (DR/CR returns, drifted expense-type spellings) falls
    into A rather than vanishing."""
    sql = str(get_inventory_minmax_report_query())
    assert "COALESCE(oc.net, 0) AS stock_oc_qty" in sql
    assert "COALESCE(st.net, 0) - COALESCE(oc.net, 0) AS stock_gpm_qty" in sql
    assert "COALESCE(st.net, 0) AS current_qty" in sql  # total kept as-is
    assert "LEFT JOIN stock_oc AS oc ON oc.item_id = mm.item_id" in sql


def test_oc_bucket_matches_expense_type_by_name_prefix():
    """Tenant expense-type masters carry spelling drift ("Mantainence"), so the
    bucket matches names, not ids; an unmatched name degrades into bucket A."""
    sql = str(get_inventory_minmax_report_query())
    assert "stock_oc AS (" in sql
    assert "LOWER(et.expense_type_name) LIKE 'overhaul%'" in sql
    assert "LOWER(et.expense_type_name) LIKE 'capital%'" in sql
    # Receipts leg keeps the same stock definition as the `stock` CTE...
    oc = sql[sql.index("stock_oc AS ("):sql.index("pending_indent AS (")]
    assert "INNER JOIN proc_po_dtl AS ppd ON ppd.po_dtl_id = pid.po_dtl_id" in oc
    assert "INNER JOIN proc_po AS pp ON pp.po_id = ppd.po_id" in oc
    assert "pid.active = 1 AND pi.sr_status = 3" in oc
    # ...and issues are classified by the RECEIPT line's PO expense type, so
    # the bucket can never go negative and double-count into the other one.
    assert "ON pid.inward_dtl_id = il.inward_dtl_id" in oc
    assert "il.expense_type_id" not in oc


def test_handler_returns_both_buckets_summing_to_current():
    row = _run_handler(current_qty=100, stock_gpm_qty=60, stock_oc_qty=40)
    assert row["stock_gpm_qty"] == 60
    assert row["stock_oc_qty"] == 40
    assert row["stock_gpm_qty"] + row["stock_oc_qty"] == row["current_qty"]


def test_status_and_order_qty_use_the_gpm_bucket_not_the_total():
    """Reporter's rule: 100 in stock with 40 earmarked Overhauling/Capital and a
    min of 80 is SHORT — the earmarked 40 can't cover production."""
    row = _run_handler(
        current_qty=100, stock_gpm_qty=60, stock_oc_qty=40,
        minqty=80, maxqty=150, below_min_qty=20,
    )
    assert row["status"] == "Below Min"
    assert row["below_min_qty"] == 20
    assert row["qty_to_be_ordered"] == 90  # up to max from the GPM bucket
    # Judged on the total it would have read "OK" and ordered nothing.


def test_handler_is_not_a_coroutine():
    """Repo sync-handler policy: sync DB calls on the event loop freeze every
    other request for the duration of the query."""
    assert not inspect.iscoroutinefunction(get_inventory_minmax_report)
