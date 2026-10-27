-- Migration: 20260823_remove_fabricated_gst_inward_18640.sql
-- Target DB: sls   Date: 2026-08-23   Scope: ONE row — proc_gst 49441 (inward 18640 / dtl 42292)
--
-- WHY
-- SR LCPL/SR/26-27/000287 (inward 18640, br 87) was saved 2026-08-10 for Rs 750
-- with NO tax: supplier SRI RAJARAJESWARI ELECTRICAL&HARDWARE is an unregistered
-- dealer (branch 38356 gst_no was the placeholder '37', later blanked by the user
-- on 2026-08-20 and the branch deactivated 2026-08-19).
-- The 2026-08-11 backfill (20260811_backfill_sr_gst_missing_supplier_branch.sql)
-- aliased placeholder '37' -> state 29 and fabricated 18% GST (Rs 135) on the line
-- from item_mst.tax_percentage. Bill Pass reads proc_gst, so it shows Rs 885 while
-- the SR screen (FE hides GST when the supplier branch has no state) shows Rs 750.
-- Remove the fabricated row. Pre-image goes to the migration ledger DB.

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.proc_gst_removed_20260823 (
    gst_invoice_type INT NOT NULL,
    proc_inward_dtl INT NULL,
    tax_pct DOUBLE NULL, stax_percentage DOUBLE NULL, s_tax_amount DOUBLE NULL,
    i_tax_amount DOUBLE NULL, i_tax_percentage DOUBLE NULL,
    c_tax_amount DOUBLE NULL, c_tax_percentage DOUBLE NULL, tax_amount DOUBLE NULL,
    active INT NULL, updated_by INT NULL, updated_date_time DATETIME NULL,
    proc_inward_additional_id INT NULL,
    reason VARCHAR(255) NULL,
    removed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (gst_invoice_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO vowsls_to_sls_transfer.proc_gst_removed_20260823
    (gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
     i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
     active, updated_by, updated_date_time, proc_inward_additional_id, reason)
SELECT gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
       i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
       active, updated_by, updated_date_time, proc_inward_additional_id,
       'fabricated by 20260811 backfill - SR 287 is no-tax (unregistered supplier)'
FROM proc_gst
WHERE gst_invoice_type = 49441 AND proc_inward_dtl = 42292;

DELETE FROM proc_gst
WHERE gst_invoice_type = 49441 AND proc_inward_dtl = 42292
  AND EXISTS (SELECT 1 FROM vowsls_to_sls_transfer.proc_gst_removed_20260823 b WHERE b.gst_invoice_type = 49441);

-- ROLLBACK
--   INSERT INTO proc_gst (gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
--       i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
--       active, updated_by, updated_date_time, proc_inward_additional_id)
--   SELECT gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
--       i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
--       active, updated_by, updated_date_time, proc_inward_additional_id
--   FROM vowsls_to_sls_transfer.proc_gst_removed_20260823 WHERE gst_invoice_type = 49441
