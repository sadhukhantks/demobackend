"""
Sales Report Queries.
Contains SQL queries for sales reports (Tally exports etc.).
"""

from sqlalchemy import text

from src.masters.tallyLinkQueries import LEDGER_DEFAULTS
from src.juteProcurement.formatters import (
    get_jute_mr_number_sql_expression,
    get_financial_year_sql_expression,
)


def _sales_invoice_no_sql_expression() -> str:
    """SQL CONCAT expression that formats the sales invoice number.

    Format mirrors format_indent_no(... document_type="SI") used in
    src/sales/salesInvoice.py: "{co_prefix}/{branch_prefix}/SI/{fy}/{invoice_no}".
    Empty parts are dropped.
    """
    fy_expr = get_financial_year_sql_expression("ih.invoice_date")
    return f"""CONCAT(
        COALESCE(cm.co_prefix, ''),
        CASE WHEN cm.co_prefix IS NOT NULL AND cm.co_prefix != '' THEN '/' ELSE '' END,
        COALESCE(bm.branch_prefix, ''),
        CASE WHEN bm.branch_prefix IS NOT NULL AND bm.branch_prefix != '' THEN '/' ELSE '' END,
        'SI/',
        {fy_expr},
        '/',
        CAST(ih.invoice_no AS CHAR)
    )"""


def _sales_order_no_sql_expression(date_col: str = "so.sales_order_date",
                                   no_col: str = "so.sales_no") -> str:
    """SQL CONCAT expression that formats the sales order number.

    Format mirrors format_indent_no(... document_type="SO"):
    "{co_prefix}/{branch_prefix}/SO/{fy}/{sales_no}".
    """
    fy_expr = get_financial_year_sql_expression(date_col)
    return f"""CONCAT(
        COALESCE(cm.co_prefix, ''),
        CASE WHEN cm.co_prefix IS NOT NULL AND cm.co_prefix != '' THEN '/' ELSE '' END,
        COALESCE(bm.branch_prefix, ''),
        CASE WHEN bm.branch_prefix IS NOT NULL AND bm.branch_prefix != '' THEN '/' ELSE '' END,
        'SO/',
        {fy_expr},
        '/',
        CAST({no_col} AS CHAR)
    )"""


def _invoice_no_for_list_sql_expression() -> str:
    """Same as _sales_invoice_no_sql_expression but referencing the
    `si` alias used by the invoice-list-report query (instead of `ih`)."""
    fy_expr = get_financial_year_sql_expression("si.invoice_date")
    return f"""CONCAT(
        COALESCE(cm.co_prefix, ''),
        CASE WHEN cm.co_prefix IS NOT NULL AND cm.co_prefix != '' THEN '/' ELSE '' END,
        COALESCE(bm.branch_prefix, ''),
        CASE WHEN bm.branch_prefix IS NOT NULL AND bm.branch_prefix != '' THEN '/' ELSE '' END,
        'SI/',
        {fy_expr},
        '/',
        CAST(si.invoice_no AS CHAR)
    )"""


def get_sales_order_list_report_query():
    """Sales Order list report — date-range scoped, all statuses.

    Bind params:
        :co_id      (int, required)
        :branch_id  (int or NULL — optional branch filter)
        :date_from  (str 'YYYY-MM-DD', required)
        :date_to    (str 'YYYY-MM-DD', required)

    Returns: text() sql object with columns:
        sales_order_id, sales_no, sales_no_formatted, sales_order_date,
        branch_name, party_name, quotation_no, invoice_type, status_name,
        status_id, approval_level, gross_amount, net_amount.
    """
    so_no_expr = _sales_order_no_sql_expression()
    sql = f"""
        SELECT
            so.sales_order_id                            AS sales_order_id,
            so.sales_no                                  AS sales_no,
            {so_no_expr}                                 AS sales_no_formatted,
            so.sales_order_date                          AS sales_order_date,
            COALESCE(bm.branch_name, '')                 AS branch_name,
            COALESCE(pm.supp_name, '')                   AS party_name,
            COALESCE(sq.quotation_no, '')                AS quotation_no,
            so.invoice_type                              AS invoice_type,
            COALESCE(sm.status_name, '')                 AS status_name,
            so.status_id                                 AS status_id,
            COALESCE(so.approval_level, 0)               AS approval_level,
            COALESCE(so.gross_amount, 0)                 AS gross_amount,
            COALESCE(so.net_amount, 0)                   AS net_amount
        FROM sales_order AS so
        LEFT JOIN branch_mst AS bm ON bm.branch_id = so.branch_id
        LEFT JOIN co_mst     AS cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst  AS pm ON pm.party_id = so.party_id
        LEFT JOIN sales_quotation AS sq ON sq.sales_quotation_id = so.quotation_id
        LEFT JOIN status_mst AS sm ON sm.status_id = so.status_id
        WHERE so.active = 1
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR so.branch_id = :branch_id)
          AND so.sales_order_date BETWEEN :date_from AND :date_to
        ORDER BY so.sales_order_date DESC, so.sales_order_id DESC
    """
    return text(sql)


def get_invoice_list_report_query():
    """Sales Invoice list report — date-range scoped, all statuses.

    Bind params:
        :co_id      (int, required)
        :branch_id  (int or NULL — optional branch filter)
        :date_from  (str 'YYYY-MM-DD', required)
        :date_to    (str 'YYYY-MM-DD', required)

    Returns: text() sql object with columns:
        invoice_id, invoice_no, invoice_no_formatted, invoice_date,
        branch_name, party_name, invoice_type, status_name, status_id,
        approval_level, challan_no, challan_date, due_date, invoice_amount,
        tax_amount, tax_payable, round_off.
    """
    inv_no_expr = _invoice_no_for_list_sql_expression()
    sql = f"""
        SELECT
            si.invoice_id                                AS invoice_id,
            si.invoice_no                                AS invoice_no,
            {inv_no_expr}                                AS invoice_no_formatted,
            si.invoice_date                              AS invoice_date,
            COALESCE(bm.branch_name, '')                 AS branch_name,
            COALESCE(pm.supp_name, '')                   AS party_name,
            si.invoice_type                              AS invoice_type,
            COALESCE(sm.status_name, '')                 AS status_name,
            si.status_id                                 AS status_id,
            COALESCE(si.approval_level, 0)               AS approval_level,
            COALESCE(si.challan_no, '')                  AS challan_no,
            si.challan_date                              AS challan_date,
            si.due_date                                  AS due_date,
            COALESCE(si.invoice_amount, 0)               AS invoice_amount,
            COALESCE(si.tax_amount, 0)                   AS tax_amount,
            COALESCE(si.tax_payable, 0)                  AS tax_payable,
            COALESCE(si.round_off, 0)                    AS round_off
        FROM sales_invoice AS si
        LEFT JOIN branch_mst AS bm ON bm.branch_id = si.branch_id
        LEFT JOIN co_mst     AS cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst  AS pm ON pm.party_id = si.party_id
        LEFT JOIN status_mst AS sm ON sm.status_id = si.status_id
        WHERE bm.co_id = :co_id
          AND (si.active = 1 OR si.active IS NULL)
          AND (:branch_id IS NULL OR si.branch_id = :branch_id)
          AND si.invoice_date BETWEEN :date_from AND :date_to
        ORDER BY si.invoice_date DESC, si.invoice_id DESC
    """
    return text(sql)


# Shared scope of the raw-jute Tally export and its preview: approved invoices
# of one type, company via branch_mst, optional branch, invoice-date range.
_JUTE_TALLY_SCOPE_WHERE = """
        WHERE bm.co_id = :co_id
          AND ih.invoice_type = :invoice_type_id
          AND ih.status_id = :approved_status_id
          AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
          AND ih.invoice_date BETWEEN :date_from AND :date_to
"""


def get_sales_jute_summary_query():
    """Per-sales-invoice summary mirroring the Tally download scope.

    One row per `sales_invoice` matching the same filters as the Tally
    header/line queries. Useful for a "Load" preview before downloading the
    xlsx. `invoice_amount` is the Tally party amount — Σ line
    amount_without_tax − Σ line claim + round_off — NOT
    sales_invoice.invoice_amount, which is net of claim on legacy rows and
    gross on juteTransfer rows.

    Bind params:
        :co_id               (int, required)
        :branch_id           (int or NULL — optional branch filter)
        :date_from           (str 'YYYY-MM-DD', required)
        :date_to             (str 'YYYY-MM-DD', required)
        :invoice_type_id     (int — INVOICE_TYPE_IDS["RAW_JUTE"])
        :approved_status_id  (int — SALES_STATUS_IDS["APPROVED"])

    Returns: text() sql object with columns:
        invoice_id, invoice_no, invoice_date, party_name, branch_name,
        vehicle_no, challan_no, mr_number, total_qty_kg, invoice_amount,
        claim_amount
    """
    mr_num_expr = get_jute_mr_number_sql_expression()
    invoice_no_expr = _sales_invoice_no_sql_expression()

    sql = f"""
        WITH
        agg_qty AS (
            SELECT sid.invoice_id,
                   SUM(sid.quantity) AS tqty,
                   SUM(COALESCE(sid.amount_without_tax, 0)) AS tawt
            FROM sales_invoice_dtl sid
            GROUP BY sid.invoice_id
        ),
        agg_claim AS (
            SELECT sid.invoice_id,
                   SUM(COALESCE(sijd.claim_amount_dtl, 0)) AS tclm
            FROM sales_invoice_dtl sid
            LEFT JOIN sales_invoice_jute_dtl sijd
                ON sijd.invoice_line_item_id = sid.invoice_line_item_id
            GROUP BY sid.invoice_id
        ),
        agg_mr AS (
            SELECT sij.invoice_id,
                   MIN(sij.mr_id) AS mr_id
            FROM sales_invoice_jute sij
            WHERE sij.mr_id IS NOT NULL
            GROUP BY sij.invoice_id
        )
        SELECT
            ih.invoice_id                                          AS invoice_id,
            {invoice_no_expr}                                      AS invoice_no,
            ih.invoice_date                                        AS invoice_date,
            COALESCE(pm.supp_name, '')                             AS party_name,
            COALESCE(bm.branch_name, '')                           AS branch_name,
            COALESCE(ih.vehicle_no, '')                            AS vehicle_no,
            COALESCE(ih.challan_no, '')                            AS challan_no,
            CASE WHEN amr.mr_id IS NOT NULL
                 THEN {mr_num_expr}
                 ELSE ''
            END                                                    AS mr_number,
            COALESCE(aq.tqty, 0)                                   AS total_qty_kg,
            (ROUND(COALESCE(aq.tawt, 0), 2) - ROUND(COALESCE(ac.tclm, 0), 2)
             + COALESCE(ih.round_off, 0))                          AS invoice_amount,
            COALESCE(ac.tclm, 0)                                   AS claim_amount
        FROM sales_invoice ih
        LEFT JOIN branch_mst bm ON bm.branch_id = ih.branch_id
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst pm ON pm.party_id = ih.party_id
        LEFT JOIN agg_qty aq ON aq.invoice_id = ih.invoice_id
        LEFT JOIN agg_claim ac ON ac.invoice_id = ih.invoice_id
        LEFT JOIN agg_mr amr ON amr.invoice_id = ih.invoice_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = amr.mr_id
        {_JUTE_TALLY_SCOPE_WHERE}
        ORDER BY ih.invoice_date, ih.invoice_id
    """
    return text(sql)


def _tally_name_join(alias: str, link_type: str, ref_col: str) -> str:
    """LEFT JOIN to the active tally_link_mst name for one ERP record."""
    return f"""
        LEFT JOIN tally_link_mst {alias}
            ON {alias}.co_id = bm.co_id
           AND {alias}.link_type = '{link_type}'
           AND {alias}.ref_id = {ref_col}
           AND {alias}.active = 1"""


def get_sales_jute_tally_header_query():
    """Raw-jute Tally export — one row per invoice (voucher header data).

    Money is NOT aggregated here: the builder sums it from the line query so
    sales_invoice.invoice_amount (unreliable — see the summary query) is never
    used. sales_invoice_jute is one row per invoice, so its LEFT JOIN does not
    fan out. Tally names fall back to the ERP name when tally_link_mst has no
    active row (GODOWN falls back to blank, ledgers to LEDGER_DEFAULTS).

    Bind params: same as get_sales_jute_summary_query.
    """
    invoice_no_expr = _sales_invoice_no_sql_expression()
    inv_fy_expr = get_financial_year_sql_expression("ih.invoice_date")
    # lot_no = the RECEIVING company's MR no, formatted like the purchase sheet's
    # "Item Allocations - Batch/Lot No." (each MR keyed on its own branch +
    # company), so the buyer's purchase voucher and this sales voucher carry the
    # same lot. sales_invoice_jute.mr_id is the SELLER's own MR; the buyer's MR
    # is found through the juteTransfer chain (see the `rm` join below) and the
    # seller's MR is the fallback (outside buyer, or not yet received).
    seller_mr_num_expr = get_jute_mr_number_sql_expression(
        co_prefix_column="mcm.co_prefix", branch_prefix_column="mbm.branch_prefix"
    )
    recv_mr_num_expr = get_jute_mr_number_sql_expression(
        mr_no_column="rm.branch_mr_no", mr_date_column="rm.jute_mr_date",
        co_prefix_column="rcm.co_prefix", branch_prefix_column="rbm.branch_prefix",
    )
    # Chain members = the seller MR's root (src_jute_mr_id, or itself) plus every
    # hop off that root. The buyer's MR is the member that (1) carries this
    # invoice's formatted no in jute_mr.invoice_no (juteTransfer writes it),
    # else (2) matches on the "/SI/<fy>/<no>" tail (company prefix renamed since),
    # else (3) names the seller as its source company. Lowest id breaks ties.
    inv_tail_match = (
        f"SUBSTRING_INDEX(h.invoice_no, '/SI/', -1) = CONCAT({inv_fy_expr}, '/', ih.invoice_no)"
    )
    # Destination (Tally "Destination" column) is fixed per receiving mill:
    # the chain ROOT MR's branch (`rt` / `rtbm`, the mill's own FACTORY) looks up
    # a DESTINATION row in the mill company's tally_link_mst; a DESTINATION_PARTY
    # row in the selling company (keyed on the buyer) overrides it; the ship-to
    # branch address is the fallback for everything unmapped.
    # ponytail: jute_mr has no index on src_jute_mr_id / invoice_no, so this is
    # one jute_mr scan per invoice -- fine for a month, add the indexes if a
    # whole-FY range gets slow.
    receiving_mr_subquery = f"""(
                SELECT h.jute_mr_id FROM jute_mr h
                WHERE jm.jute_mr_id IS NOT NULL
                  AND h.jute_mr_id <> jm.jute_mr_id
                  AND (h.jute_mr_id = COALESCE(jm.src_jute_mr_id, jm.jute_mr_id)
                       OR h.src_jute_mr_id = COALESCE(jm.src_jute_mr_id, jm.jute_mr_id))
                  AND (h.invoice_no = {invoice_no_expr}
                       OR {inv_tail_match}
                       OR h.src_com_id = bm.co_id)
                ORDER BY (h.invoice_no = {invoice_no_expr}) DESC,
                         ({inv_tail_match}) DESC,
                         h.jute_mr_id
                LIMIT 1
            )"""
    sql = f"""
        SELECT
            ih.invoice_id,
            ih.invoice_date,
            {invoice_no_expr}                              AS voucher_no,
            ih.vehicle_no,
            ih.challan_no,
            ih.challan_date,
            ih.buyer_order_no,
            ih.buyer_order_date,
            ih.payment_terms,
            COALESCE(ih.round_off, 0)                      AS round_off,
            COALESCE(tlp.tally_name, pm.supp_name, '')     AS party_name,
            COALESCE(tlc.tally_name, cpm.supp_name, '')    AS consignee_name,
            COALESCE(tlg.tally_name, '')                   AS godown_name,
            COALESCE(tlsl.tally_name, '{LEDGER_DEFAULTS["SALES_LEDGER"]}') AS sales_ledger,
            COALESCE(tlcl.tally_name, '{LEDGER_DEFAULTS["SALES_CLAIM"]}')  AS claim_ledger,
            COALESCE(tlro.tally_name, '{LEDGER_DEFAULTS["ROUND_OFF"]}')    AS round_off_ledger,
            bpb.address                                    AS bill_address,
            bpb.zip_code                                   AS bill_zip,
            COALESCE(bst.state, bsc.state, '')             AS bill_state,
            spb.address                                    AS ship_address,
            spb.zip_code                                   AS ship_zip,
            COALESCE(sst.state, ssc.state, '')             AS ship_state,
            bm.branch_address1                             AS dispatch_address,
            COALESCE(tldp.tally_name, tld.tally_name, spb.address, '') AS destination,
            sij.unit_conversion,
            CASE WHEN rm.jute_mr_id IS NOT NULL THEN {recv_mr_num_expr}
                 WHEN jm.jute_mr_id IS NOT NULL THEN {seller_mr_num_expr}
                 ELSE ''
            END                                            AS lot_no
        FROM sales_invoice ih
        JOIN branch_mst bm ON bm.branch_id = ih.branch_id
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst pm ON pm.party_id = ih.party_id
        LEFT JOIN party_branch_mst bpb ON bpb.party_mst_branch_id = ih.billing_to_id
        LEFT JOIN state_mst bst ON bst.state_id = bpb.state_id
        LEFT JOIN state_mst bsc ON bsc.state_code = ih.billing_state_code
        LEFT JOIN party_branch_mst spb ON spb.party_mst_branch_id = ih.shipping_to_id
        LEFT JOIN state_mst sst ON sst.state_id = spb.state_id
        LEFT JOIN state_mst ssc ON ssc.state_code = ih.shipping_state_code
        LEFT JOIN party_mst cpm ON cpm.party_id = spb.party_id
        LEFT JOIN sales_invoice_jute sij ON sij.invoice_id = ih.invoice_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = sij.mr_id
        LEFT JOIN branch_mst mbm ON mbm.branch_id = jm.branch_id
        LEFT JOIN co_mst mcm ON mcm.co_id = mbm.co_id
        LEFT JOIN jute_mr rm ON rm.jute_mr_id = {receiving_mr_subquery}
        LEFT JOIN branch_mst rbm ON rbm.branch_id = rm.branch_id
        LEFT JOIN co_mst rcm ON rcm.co_id = rbm.co_id
        LEFT JOIN jute_mr rt ON rt.jute_mr_id = COALESCE(jm.src_jute_mr_id, jm.jute_mr_id)
        LEFT JOIN branch_mst rtbm ON rtbm.branch_id = rt.branch_id
        LEFT JOIN tally_link_mst tld
            ON tld.co_id = rtbm.co_id
           AND tld.link_type = 'DESTINATION'
           AND tld.ref_id = rt.branch_id
           AND tld.active = 1
        {_tally_name_join("tldp", "DESTINATION_PARTY", "ih.party_id")}
        {_tally_name_join("tlp", "PARTY", "ih.party_id")}
        {_tally_name_join("tlc", "PARTY", "spb.party_id")}
        {_tally_name_join("tlg", "GODOWN", "0")}
        {_tally_name_join("tlsl", "SALES_LEDGER", "0")}
        {_tally_name_join("tlcl", "SALES_CLAIM", "0")}
        {_tally_name_join("tlro", "ROUND_OFF", "0")}
        {_JUTE_TALLY_SCOPE_WHERE}
        ORDER BY ih.invoice_date, ih.invoice_no, ih.invoice_id
    """
    return text(sql)


def get_sales_jute_tally_line_query():
    """Raw-jute Tally export — one row per invoice line.

    Raw quantity / rate / uom are returned as stored; the builder converts
    legacy quintal lines to kg. sales_invoice_jute_dtl is at most one row per
    line (claim + bale count).

    Bind params: same as get_sales_jute_summary_query.
    """
    sql = f"""
        SELECT
            sid.invoice_id,
            sid.invoice_line_item_id,
            COALESCE(tli.tally_name, im.item_name, '')     AS item_name,
            sid.quantity,
            sid.rate,
            sid.amount_without_tax,
            sid.uom_id,
            um.uom_name,
            sijd.claim_amount_dtl                          AS claim_amount,
            sijd.qty_untit_conversion                      AS bales
        FROM sales_invoice ih
        JOIN branch_mst bm ON bm.branch_id = ih.branch_id
        JOIN sales_invoice_dtl sid ON sid.invoice_id = ih.invoice_id
        LEFT JOIN sales_invoice_jute_dtl sijd
            ON sijd.invoice_line_item_id = sid.invoice_line_item_id
        LEFT JOIN item_mst im ON im.item_id = sid.item_id
        LEFT JOIN uom_mst um ON um.uom_id = sid.uom_id
        {_tally_name_join("tli", "ITEM", "sid.item_id")}
        {_JUTE_TALLY_SCOPE_WHERE}
        ORDER BY sid.invoice_id, sid.invoice_line_item_id
    """
    return text(sql)
