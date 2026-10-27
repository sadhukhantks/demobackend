# src/test/test_procurement_sr_no_po_gst.py
"""
Regression tests: a direct (no-PO) inward never captures the supplier/billing/
shipping branch, so the SR footer GST silently computed to zero. The SR header
query must now resolve those states with sensible fallbacks:

  - billing / shipping  -> the inward's own receiving branch (pi.branch_id)
  - supplier            -> the supplier's sole active party branch (auto), or
                           left NULL when ambiguous (0 / >1 branches) so the SR
                           page can offer a picker.
"""

from sqlalchemy import text

from src.procurement.query import (
    get_inward_dtl_for_sr_query,
    get_inward_for_sr_query,
    get_supplier_branches_for_gst_query,
    update_inward_sr,
)


class TestInwardForSrStateFallbacks:
    def test_returns_text_object(self):
        assert isinstance(get_inward_for_sr_query(), type(text("")))

    def test_bill_ship_fall_back_to_receiving_branch(self):
        sql = str(get_inward_for_sr_query())
        # Both billing and shipping resolution end at the inward's own branch.
        assert "COALESCE(pi.bill_branch_id, linked_po.billing_branch_id, pi.branch_id)" in sql
        assert "COALESCE(pi.ship_branch_id, linked_po.shipping_branch_id, pi.branch_id)" in sql

    def test_supplier_branch_auto_resolves_single_active_branch(self):
        sql = str(get_inward_for_sr_query())
        # The auto-single subquery: exactly-one active party branch or NULL.
        assert "party_branch_mst pb_auto" in sql
        assert "HAVING COUNT(*) = 1" in sql

    def test_exposes_resolved_supplier_branch_id(self):
        sql = str(get_inward_for_sr_query())
        assert "AS supplier_branch_id" in sql


class TestInwardDtlTaxPrecedence:
    """SR line tax_percentage precedence: saved proc_gst first, then explicit
    zero for a saved SR with no proc_gst row, then the PO's po_gst (PO-linked
    lines only), then item_mst. A direct (no-PO) inward whose SR was saved at
    5% must read back 5%, not the item master rate."""

    def test_saved_proc_gst_wins_over_item_master(self):
        sql = str(get_inward_dtl_for_sr_query())
        # proc_gst (aliased psg) is joined on the inward detail and checked first.
        assert "proc_gst AS psg ON psg.proc_inward_dtl = pid.inward_dtl_id" in sql
        assert "WHEN psg.tax_pct IS NOT NULL THEN psg.tax_pct" in sql

    def test_falls_back_to_po_then_item_master(self):
        sql = str(get_inward_dtl_for_sr_query())
        # Order matters: psg check must precede the po_dtl_id / item_mst branches.
        psg = sql.index("psg.tax_pct IS NOT NULL")
        po = sql.index("pid.po_dtl_id IS NOT NULL THEN COALESCE(pg.tax_pct, 0)")
        im = sql.index("ELSE COALESCE(im.tax_percentage, 0)")
        assert psg < po < im

    def test_saved_sr_without_proc_gst_reads_back_zero(self):
        """TKT-000126: an SR saved with Tax % zeroed writes no proc_gst row.
        Reloading must show 0, not re-seed 18% from item_mst / po_gst — the
        saved-SR branch sits between the psg check and the seed fallbacks."""
        sql = str(get_inward_dtl_for_sr_query())
        assert "LEFT JOIN proc_inward AS pi_hdr ON pi_hdr.inward_id = pid.inward_id" in sql
        saved_zero = "WHEN pi_hdr.sr_status IS NOT NULL AND pi_hdr.sr_status <> 0 THEN 0"
        assert saved_zero in sql
        psg = sql.index("psg.tax_pct IS NOT NULL")
        zero = sql.index(saved_zero)
        po = sql.index("pid.po_dtl_id IS NOT NULL THEN COALESCE(pg.tax_pct, 0)")
        assert psg < zero < po


class TestSupplierBranchesQuery:
    def test_binds_supplier_id_and_only_active(self):
        sql = str(get_supplier_branches_for_gst_query())
        assert ":supplier_id" in sql
        assert "pb.active = 1" in sql
        # Carries state + gst so the picker can label each option.
        assert "state_name" in sql
        assert "gst_no" in sql


class TestUpdateInwardSrPersistsSupplierBranch:
    def test_persists_supplier_branch_without_nulling_existing(self):
        sql = str(update_inward_sr())
        # COALESCE keeps the stored value when the request omits it.
        assert "supplier_branch_id = COALESCE(:supplier_branch_id, supplier_branch_id)" in sql
