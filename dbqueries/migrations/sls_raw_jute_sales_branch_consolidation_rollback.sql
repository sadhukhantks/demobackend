-- ============================================================================
-- Raw-jute sales: consolidate the selling branch onto the receiving branch
-- STEP 3 of 3 — ROLLBACK (restores sales_invoice.branch_id from the ledger)
-- ============================================================================
-- Target DB : sls   (production tenant)
-- Tag       : JUTESALESBR-20260822
--
-- Puts every applied invoice back on the branch it was invoiced from, using the
-- old_branch_id captured at plan time. Guarded on `si.branch_id = m.new_branch_id`
-- so it never overwrites an invoice that has since been moved somewhere else.
--
-- The ledger rows are kept and marked 'rolled_back' — they are the audit trail, so
-- do not DELETE them. Re-applying afterwards means re-running _plan.sql (which will
-- re-propose the rows, since they are back on the old branch) then _apply.sql.
--
-- Expected: 8,265 rows restored, 8,265 marked rolled_back.
-- ============================================================================

UPDATE sales_invoice si
JOIN vowsls_to_sls_transfer.jute_sales_branch_remap m
  ON m.invoice_id = si.invoice_id
 AND m.tag = 'JUTESALESBR-20260822'
 AND m.status = 'applied'
 AND si.branch_id = m.new_branch_id
SET si.branch_id = m.old_branch_id;

UPDATE vowsls_to_sls_transfer.jute_sales_branch_remap m
JOIN sales_invoice si
  ON si.invoice_id = m.invoice_id
 AND si.branch_id = m.old_branch_id
SET m.status = 'rolled_back',
    m.rolled_back_at = NOW()
WHERE m.tag = 'JUTESALESBR-20260822'
  AND m.status = 'applied';
