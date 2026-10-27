"""Winding stock query builders — construction smoke test + bind-name contract.

Cheap guard: every builder must construct, and the binds each statement declares
must be exactly the set the router promises to pass. A bind added to the SQL
without being added to the caller's dict is a runtime 500, not an import error,
so it is asserted here.
"""

import src.juteProduction.winding_stock_query as q

BUILDERS = [
    "get_winding_stock_by_date_query",
    "get_winding_stock_active_by_id_query",
    "insert_winding_stock_row_query",
    "update_winding_stock_row_query",
    "soft_delete_winding_stock_row_query",
    "get_winding_stock_duplicate_query",
    "get_winding_stock_wt_default_query",
    "get_winding_stock_prev_entry_date_query",
    "copy_winding_stock_previous_day_query",
    "get_winding_stock_yarn_items_query",
    "get_winding_stock_areas_query",
    "verify_winding_stock_item_query",
    "verify_winding_stock_area_query",
    "get_winding_stock_departments_query",
    "verify_winding_stock_dept_query",
    "get_winding_stock_lock_row_query",
    "insert_winding_stock_lock_query",
    "update_winding_stock_lock_query",
    "clear_winding_stock_lock_query",
    "get_weaving_quality_type_map_query",
    "get_weaving_unresolved_quality_count_query",
    "get_sale_yarn_mt_query",
    "get_jute_issue_weight_query",
    "get_yarn_purchase_qty_query",
    "get_yarn_purchase_document_qty_query",
    "get_yarn_purchase_document_detail_query",
]


def test_all_builders_construct():
    for name in BUILDERS:
        assert getattr(q, name)() is not None, name


def test_by_id_builders_are_co_scoped():
    """A tenant DB holds every company's rows: pk alone is a cross-company read."""
    for name in (
        "get_winding_stock_active_by_id_query",
        "update_winding_stock_row_query",
        "soft_delete_winding_stock_row_query",
        "update_winding_stock_lock_query",
        "clear_winding_stock_lock_query",
    ):
        sql = str(getattr(q, name)())
        assert "co_id = :co_id" in sql, name


def test_soft_delete_never_deletes():
    sql = str(q.soft_delete_winding_stock_row_query())
    assert "active = 0" in sql
    assert "DELETE" not in sql.upper()


def test_sale_yarn_filters_on_item_type_not_invoice_type():
    """INVOICE_TYPE_IDS['JUTE_YARN'] == 4 collides numerically with
    item_grp_mst.item_type_id == 4. Yarn is identified by the LINE's item type,
    which catches yarn on any invoice type; filtering on invoice_type would
    instead pull non-yarn lines that ride a yarn invoice."""
    sql = str(q.get_sale_yarn_mt_query())
    assert "item_type_id" in sql
    assert "invoice_type" not in sql


def test_duplicate_probe_keys_on_location_and_department():
    """The same package counted in the godown and on a floor is two lines.
    dept_id is nullable (legacy FLOOR rows, every GODOWN row) so it must be
    NULL-safe like item_id / wastage_area_id."""
    sql = str(q.get_winding_stock_duplicate_query())
    assert "stock_loc = :stock_loc" in sql
    assert "dept_id <=> :dept_id" in sql


def test_write_builders_carry_location_and_department():
    for name in (
        "insert_winding_stock_row_query",
        "update_winding_stock_row_query",
        "copy_winding_stock_previous_day_query",
    ):
        sql = str(getattr(q, name)())
        assert "stock_loc" in sql and "dept_id" in sql, name


def test_by_date_labels_the_department_without_dropping_rows():
    """A LEFT JOIN: legacy rows and GODOWN rows have dept_id NULL and must
    still be returned, or the report's totals would fall."""
    sql = str(q.get_winding_stock_by_date_query())
    assert "LEFT JOIN dept_mst dm ON dm.dept_id = ws.dept_id" in sql
    assert "ws.stock_loc" in sql and "dm.dept_desc" in sql


def test_departments_are_branch_scoped():
    """dept_mst has no co_id and no active column: branch is the whole scope,
    and it is mandatory — a NULL fallback would offer every branch's depts."""
    sql = str(q.get_winding_stock_departments_query())
    assert "branch_id = :branch_id" in sql
    assert "IS NULL" not in sql
    assert "active" not in sql


def test_purchase_document_builders_share_the_report_window_binds():
    """Both new builders take exactly the binds the difference report already
    passes to the legacy Stores query, so the router can call all three with
    one dict."""
    for name in ("get_yarn_purchase_document_qty_query",
                 "get_yarn_purchase_document_detail_query"):
        binds = set(getattr(q, name)().compile().binds)
        assert binds == {"co_id", "branch_id", "date_from", "date_to"}, name
    assert set(q.get_yarn_purchase_qty_query().compile().binds) == {
        "co_id", "branch_id", "date_from", "date_to"
    }


def test_purchase_document_builders_are_approved_and_co_branch_scoped():
    for name in ("get_yarn_purchase_document_qty_query",
                 "get_yarn_purchase_document_detail_query"):
        sql = str(getattr(q, name)())
        assert "h.co_id = :co_id" in sql, name
        assert "h.branch_id = :branch_id" in sql, name
        assert "h.status_id = 3" in sql, name
