"""
SQL for building the INV-01 payload and reading e-invoice state.

All reads only — the base sales tables are never written by this module.
"""

from sqlalchemy import text


def get_invoice_header_for_einvoice():
    """Invoice header + seller (branch) + buyer/ship-to (party branch) in one row.

    Seller side comes from branch_mst (gst_no, address, state); buyer and
    ship-to come from party_branch_mst joined via billing_to_id/shipping_to_id.
    """
    sql = """
    SELECT
        si.invoice_id,
        si.invoice_no,
        si.invoice_date,
        si.invoice_type,
        si.invoice_amount,
        si.round_off,
        si.tax_amount,
        si.intra_inter_state,
        si.status_id,
        si.branch_id,
        si.party_id,
        si.billing_to_id,
        si.shipping_to_id,
        si.vehicle_no,
        si.transporter_id,
        si.transporter_name,
        si.transporter_doc_no,
        si.transporter_doc_date,
        b.co_id,
        b.branch_name          AS seller_name,
        b.gst_no               AS seller_gstin,
        b.branch_address1      AS seller_address1,
        b.branch_address2      AS seller_address2,
        b.branch_zipcode       AS seller_pincode,
        bs.state               AS seller_state_name,
        bs.state_code          AS seller_state_code,
        b.contact_no           AS seller_phone,
        b.branch_email         AS seller_email,
        p.supp_name            AS buyer_name,
        bill.gst_no            AS buyer_gstin,
        bill.address           AS buyer_address1,
        bill.address_additional AS buyer_address2,
        bill.zip_code          AS buyer_pincode,
        bills.state            AS buyer_state_name,
        bills.state_code       AS buyer_state_code,
        bill.contact_no        AS buyer_phone,
        ship.gst_no            AS ship_gstin,
        ship.address           AS ship_address1,
        ship.address_additional AS ship_address2,
        ship.zip_code          AS ship_pincode,
        ships.state            AS ship_state_name,
        ships.state_code       AS ship_state_code
    FROM sales_invoice si
    LEFT JOIN branch_mst b        ON b.branch_id = si.branch_id
    LEFT JOIN state_mst bs        ON bs.state_id = b.state_id
    LEFT JOIN party_mst p         ON p.party_id = si.party_id
    LEFT JOIN party_branch_mst bill ON bill.party_mst_branch_id = si.billing_to_id
    LEFT JOIN state_mst bills     ON bills.state_id = bill.state_id
    LEFT JOIN party_branch_mst ship ON ship.party_mst_branch_id = si.shipping_to_id
    LEFT JOIN state_mst ships     ON ships.state_id = ship.state_id
    WHERE si.invoice_id = :invoice_id
    """
    return text(sql)


def get_invoice_lines_for_einvoice():
    """Line items + per-line GST breakup + item/uom names for ItemList rows."""
    sql = """
    SELECT
        d.invoice_line_item_id,
        d.item_id,
        d.hsn_code,
        d.quantity,
        d.rate,
        d.discount_amount,
        d.amount_without_tax,
        d.total_amount,
        im.item_name,
        um.uom_name,
        g.tax_percentage,
        g.cgst_amount,
        g.sgst_amount,
        g.igst_amount,
        g.cgst_percentage,
        g.sgst_percentage,
        g.igst_percentage,
        g.tax_amount AS line_tax_amount
    FROM sales_invoice_dtl d
    LEFT JOIN item_mst im ON im.item_id = d.item_id
    LEFT JOIN uom_mst um  ON um.uom_id = d.uom_id
    LEFT JOIN sales_invoice_dtl_gst g ON g.invoice_line_item_id = d.invoice_line_item_id
    WHERE d.invoice_id = :invoice_id
    ORDER BY d.invoice_line_item_id
    """
    return text(sql)


def get_einv_config_for_branch():
    """Active credential config for a branch's GSTIN in the given env.

    Prefers a branch-level row, falls back to a company-level row (branch_id NULL).
    """
    sql = """
    SELECT *
    FROM einv_gstin_config
    WHERE active = 1
      AND env = :env
      AND co_id = :co_id
      AND (branch_id = :branch_id OR branch_id IS NULL)
    ORDER BY branch_id IS NULL
    LIMIT 1
    """
    return text(sql)


def get_irn_registry_for_invoice():
    sql = """
    SELECT *
    FROM einv_irn_registry
    WHERE invoice_id = :invoice_id
      AND env = :env
    LIMIT 1
    """
    return text(sql)


def get_api_logs_for_invoice():
    sql = """
    SELECT einv_api_log_id, einv_irn_id, api_name, http_status, success,
           error_code, error_message, duration_ms, called_by, called_at
    FROM einv_api_log
    WHERE invoice_id = :invoice_id
    ORDER BY einv_api_log_id DESC
    LIMIT 100
    """
    return text(sql)
