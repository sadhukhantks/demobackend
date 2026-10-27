-- ============================================================================
-- sls branch 87 — attribute every migrated raw-jute sales line to the MR line it
-- was sold from (sales_invoice_dtl.jute_mr_li_id backfill)
-- ============================================================================
-- Target DB   : sls ONLY (production tenant — Limelight Commercial Pvt. Ltd.,
--               co_id 106, branch 87 FACTORY). Reads two sibling schemas on the
--               same server: vowsls (old system) and vowsls_to_sls_transfer
--               (migration ledger). Do NOT run on dev3 / other tenants.
-- Requires    : add_sales_dtl_jute_mr_li_link_and_view_sold.sql applied first.
-- Date        : 2026-08-23
--
-- SOURCE OF TRUTH
-- The old system kept the link on every invoice line
-- (vowsls.invoice_line_items.mr_line_id = scm_mr_line_item.jute_line_item_no),
-- and the migration preserved MR line ids 1:1 (sls.jute_mr_li.jute_mr_li_id =
-- old jute_line_item_no). The ledger table vowsls_to_sls_transfer._map_invoice_lines
-- maps every sls sales line back to its old invoice line. Verified 2026-08-23
-- for branch 87 before writing this:
--     1,075 approved raw-jute sales lines on branch 87
--     1,075 map to an old invoice line            (100%)
--     1,075 old lines carry mr_line_id > 0          (100%)
--     1,075 of those MR lines exist in sls.jute_mr_li AND sit on branch 87 (100%)
--     1,075 have sales_weight identical old vs new  (100%)
--     85,111.23 qtl linked in total
--
-- ROW COUNT: the UPDATE links every raw-jute line on branch 87 regardless of
-- invoice status — 1,091 rows = the 1,075 approved lines above + 16 lines of 8
-- DRAFT (status 1) invoices from 2022 (1,080.34 qtl; draft in the old system
-- too). Attribution is a fact of the line, status is the view's concern: the
-- view and the branch report both ignore non-approved invoices, so those 16 move
-- nothing today. If one of those 2022 drafts were ever approved, both ledgers
-- would subtract it (consistently) against MR lines the zeroing script has
-- already closed — cancel those drafts rather than approving them.
--
-- EFFECT
-- vw_jute_stock_outstanding (line ledger: issue picker, mrInStock, MR Wise,
-- Godown Wise) now subtracts these sales per line, so the ~1,100 legacy lines
-- that were sold in the old system stop showing as issuable stock. The branch
-- balance reports already subtracted these same invoices per branch
-- (`_sold_cte`), so they do NOT move. No accounting, no voucher, no sales
-- document field other than this one attribution column is touched.
--
-- GUARDS (re-runnable, cannot mis-attribute)
--   * only rows whose jute_mr_li_id is still NULL
--   * only invoice_type 5 on branch 87
--   * only where the old line's mr_line_id exists in sls.jute_mr_li and that
--     line's MR is on branch 87
--
-- ROLLBACK
--   UPDATE sales_invoice_dtl sid
--   JOIN sales_invoice sinv ON sinv.invoice_id = sid.invoice_id
--   SET sid.jute_mr_li_id = NULL
--   WHERE sinv.branch_id = 87 AND sinv.invoice_type = 5;
-- (the ledger map is the source, so the link is fully re-derivable)
-- ============================================================================

-- The join plan full-scans vowsls.invoice_line_items (the OLD system, still
-- live for this company); READ COMMITTED keeps the sub-second UPDATE from
-- holding next-key locks on that table.
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;

UPDATE sales_invoice_dtl sid
JOIN sales_invoice sinv
    ON sinv.invoice_id = sid.invoice_id
JOIN vowsls_to_sls_transfer._map_invoice_lines m
    ON m.new_invoice_line_item_id = sid.invoice_line_item_id
JOIN vowsls.invoice_line_items o
    ON o.invoice_line_item_id = m.old_invoice_line_item_id
JOIN jute_mr_li li
    ON li.jute_mr_li_id = o.mr_line_id
JOIN jute_mr jm
    ON jm.jute_mr_id = li.jute_mr_id
SET sid.jute_mr_li_id = o.mr_line_id
WHERE sinv.branch_id   = 87
  AND sinv.invoice_type = 5
  AND sid.jute_mr_li_id IS NULL
  AND o.mr_line_id > 0
  AND jm.branch_id = 87;

-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
-- expect: n = 1075, linked = 1075, wt = 85111.23   (runner rowcount for the UPDATE: 1091)
-- SELECT COUNT(*) n, SUM(sid.jute_mr_li_id IS NOT NULL) linked,
--        ROUND(SUM(CASE WHEN sid.jute_mr_li_id IS NOT NULL THEN sid.sales_weight END),2) wt
-- FROM sales_invoice sinv JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
-- WHERE sinv.branch_id = 87 AND sinv.invoice_type = 5 AND sinv.status_id = 3
--   AND COALESCE(sinv.active,1) = 1;
