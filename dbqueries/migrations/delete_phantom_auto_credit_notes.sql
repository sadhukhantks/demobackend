-- Migration: delete phantom auto-created credit notes
-- Root cause: approve_sr compared accepted_rate against po_rate coerced to 0 for
-- PO-less inward lines, creating a bogus credit note on every SR approval.
-- Scope: auto_create=1, adjustment_type=2 (credit), status Draft(21)/Open(1),
-- and no line whose inward dtl has a po_dtl_id.
-- Rollback:
--   INSERT INTO drcr_note SELECT * FROM vowsls_to_sls_transfer.drcr_note_bak_20260808
--   INSERT INTO drcr_note_dtl SELECT * FROM vowsls_to_sls_transfer.drcr_note_dtl_bak_20260808
--   INSERT INTO drcr_note_dtl_gst SELECT * FROM vowsls_to_sls_transfer.drcr_note_dtl_gst_bak_20260808
--   DROP TABLE vowsls_to_sls_transfer.drcr_note_bak_20260808, vowsls_to_sls_transfer.drcr_note_dtl_bak_20260808, vowsls_to_sls_transfer.drcr_note_dtl_gst_bak_20260808

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.drcr_note_bak_20260808 AS
SELECT dn.* FROM drcr_note dn
WHERE dn.auto_create = 1 AND dn.adjustment_type = 2 AND dn.status_id IN (21, 1)
  AND NOT EXISTS (
    SELECT 1 FROM drcr_note_dtl dd
    JOIN proc_inward_dtl pid ON pid.inward_dtl_id = dd.inward_dtl_id
    WHERE dd.debit_credit_note_id = dn.debit_credit_note_id
      AND pid.po_dtl_id IS NOT NULL
  );

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.drcr_note_dtl_bak_20260808 AS
SELECT dd.* FROM drcr_note_dtl dd
JOIN vowsls_to_sls_transfer.drcr_note_bak_20260808 b ON b.debit_credit_note_id = dd.debit_credit_note_id;

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.drcr_note_dtl_gst_bak_20260808 AS
SELECT g.* FROM drcr_note_dtl_gst g
JOIN vowsls_to_sls_transfer.drcr_note_dtl_bak_20260808 db ON db.drcr_note_dtl_id = g.drcr_note_dtl_id;

DELETE g FROM drcr_note_dtl_gst g
JOIN vowsls_to_sls_transfer.drcr_note_dtl_bak_20260808 db ON db.drcr_note_dtl_id = g.drcr_note_dtl_id;

DELETE dd FROM drcr_note_dtl dd
JOIN vowsls_to_sls_transfer.drcr_note_bak_20260808 b ON b.debit_credit_note_id = dd.debit_credit_note_id;

DELETE dn FROM drcr_note dn
JOIN vowsls_to_sls_transfer.drcr_note_bak_20260808 b ON b.debit_credit_note_id = dn.debit_credit_note_id;
