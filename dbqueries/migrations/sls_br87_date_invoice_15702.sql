-- ============================================================================
-- sls — date the undated approved raw-jute invoice 15702 (branch 87)
-- ============================================================================
-- Target DB   : sls ONLY (Limelight Commercial Pvt. Ltd., co_id 106, branch 87)
-- Date        : 2026-08-23   User ruling: "yes add it" (question 5)
--
-- WHY
-- sales_invoice 15702 = LCP/DEC/049/24-25 (invoice_no 49), invoice_type 5 raw jute,
-- status 3 approved, 2 lines 113.10 qtl (MR 23451 lines 37538 = 36.76, 37539 = 76.34),
-- has invoice_date NULL — in the old system too (vowsls invoice_hdr 17744). The
-- line ledger (vw_jute_stock_outstanding, now line-linked) subtracts it, so those
-- two MR lines read 0 in the picker; the branch stock report buckets sales by
-- invoice_date and therefore never counts it, leaving a permanent +113.10 qtl
-- phantom on items 1099/1098. challan_date is 2024-12-16 and the number string
-- says DEC/24-25, so that is the date.
--
-- KNOWN: sales_invoice 15701 (branch 87, type 5, dated 2024-12-19) ALREADY carries
-- invoice_no 49, so dating 15702 makes a duplicate invoice_no 49 visible in the FY
-- 24-25 sales reports (invoice_no has no unique index on sls). Correcting the number
-- is a separate ruling; this script only dates the document.
--
-- GUARDED: only if the date is still NULL. Rerunnable.
-- ROLLBACK (roll back sls_br87_stock_asof_20260329_adjust.sql FIRST if it has run):
--   UPDATE sales_invoice SET invoice_date = NULL
--   WHERE invoice_id = 15702 AND invoice_date = '2024-12-16';
-- ============================================================================

UPDATE sales_invoice
SET invoice_date = '2024-12-16'
WHERE invoice_id = 15702
  AND branch_id = 87
  AND invoice_type = 5
  AND invoice_date IS NULL;

-- Verification (expect 1 row, invoice_date 2024-12-16)
-- SELECT invoice_id, invoice_no, invoice_date, status_id, branch_id FROM sales_invoice WHERE invoice_id = 15702;
