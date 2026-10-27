"""Material inspection list: pending-only by default; include_completed lifts the qc_check gate."""
from src.juteProcurement.query import (
    get_material_inspection_table_query,
    get_material_inspection_table_count_query,
)

QC_GATE = "(jm.qc_check IS NULL OR jm.qc_check = 0)"


def test_default_lists_pending_only():
    for fn in (get_material_inspection_table_query, get_material_inspection_table_count_query):
        assert QC_GATE in str(fn(co_id=1))


def test_include_completed_drops_gate_but_keeps_other_filters():
    for fn in (get_material_inspection_table_query, get_material_inspection_table_count_query):
        sql = str(fn(co_id=1, branch_ids=[29], search="x", include_completed=True))
        assert QC_GATE not in sql
        assert ":branch_id_0" in sql and ":search" in sql and ":date_from" in sql
