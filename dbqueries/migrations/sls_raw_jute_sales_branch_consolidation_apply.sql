-- ============================================================================
-- Raw-jute sales: consolidate the selling branch onto the receiving branch
-- STEP 2 of 3 — APPLY (mutates sales_invoice.branch_id)
-- ============================================================================
-- Target DB : sls   (production tenant)
-- Tag       : JUTESALESBR-20260822
-- Requires  : _plan.sql already run (8,265 rows, status 'proposed')
-- Undo      : _rollback.sql
--
-- Moves ONLY the invoices the plan proposed, and only while they still sit on the
-- branch the plan recorded (`si.branch_id = m.old_branch_id`). That guard makes
-- this re-runnable and stops it touching an invoice someone re-filed in the
-- meantime — such a row simply stays 'proposed' and shows up in the check below.
--
-- Expected: 8,265 rows moved, 8,265 marked applied, 0 left proposed.
-- ============================================================================

UPDATE sales_invoice si
JOIN vowsls_to_sls_transfer.jute_sales_branch_remap m
  ON m.invoice_id = si.invoice_id
 AND m.tag = 'JUTESALESBR-20260822'
 AND m.status = 'proposed'
 AND si.branch_id = m.old_branch_id
SET si.branch_id = m.new_branch_id;

UPDATE vowsls_to_sls_transfer.jute_sales_branch_remap m
JOIN sales_invoice si
  ON si.invoice_id = m.invoice_id
 AND si.branch_id = m.new_branch_id
SET m.status = 'applied',
    m.applied_at = NOW()
WHERE m.tag = 'JUTESALESBR-20260822'
  AND m.status = 'proposed';
