-- Migration: backfill invoice_rate from PO rate for PO-linked inward lines
-- The SR screen displays PO rate as the default invoice rate for PO-linked
-- lines. Persisting the same default keeps approve_sr (which reads the stored
-- column) consistent with the display for SRs saved before this deploy.
-- PO-less lines stay NULL (no baseline, no auto rate-diff note).
-- Rollback: UPDATE proc_inward_dtl SET invoice_rate = NULL
--   (only if run before any user-entered invoice rates exist)

UPDATE proc_inward_dtl pid
JOIN proc_po_dtl ppd ON ppd.po_dtl_id = pid.po_dtl_id
SET pid.invoice_rate = ppd.rate
WHERE pid.invoice_rate IS NULL
  AND ppd.rate IS NOT NULL;
