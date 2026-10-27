-- Migration: 20260823_remove_fabricated_gst_backfill_siblings.sql
-- Target DB: sls   Date: 2026-08-23   Scope: the 10 remaining proc_gst rows posted by the
-- 2026-08-11 backfill burst (gst_invoice_type 49431..49440), sibling of
-- 20260823_remove_fabricated_gst_inward_18640.sql (which removed 49441).
--
-- WHY
-- Every row was posted onto an SR whose supplier invoice total equals the SR's
-- tax-exclusive value (proc_inward.invoice_amount = proc_inward.sr_value), i.e. the
-- supplier billed NO tax; all suppliers are unregistered (branch gst_no = bare
-- placeholder '37' / blank, invoice_no 'NIL' / '0' for cash). The backfill inferred
-- 18% from item_mst.tax_percentage, which is only a seed for direct inwards.
-- The invoice_amount = sr_value equality is enforced below as the guard, so a row
-- that does not meet it is left alone and reported.
--
-- Pre-images go to vowsls_to_sls_transfer.proc_gst_removed_20260823 (created by the
-- sibling script; CREATE IF NOT EXISTS repeated here so this file runs standalone).

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
SELECT g.gst_invoice_type, g.proc_inward_dtl, g.tax_pct, g.stax_percentage, g.s_tax_amount,
       g.i_tax_amount, g.i_tax_percentage, g.c_tax_amount, g.c_tax_percentage, g.tax_amount,
       g.active, g.updated_by, g.updated_date_time, g.proc_inward_additional_id,
       CONCAT('fabricated by 20260811 backfill - invoice_amount = sr_value = ', pi.sr_value, ' (no tax billed)')
FROM proc_gst g
JOIN proc_inward_dtl pid ON pid.inward_dtl_id = g.proc_inward_dtl
JOIN proc_inward pi ON pi.inward_id = pid.inward_id
WHERE g.gst_invoice_type BETWEEN 49431 AND 49440
  AND g.updated_date_time = '2026-08-11 07:12:42'
  AND pi.invoice_amount IS NOT NULL
  AND pi.invoice_amount = pi.sr_value;

DELETE g FROM proc_gst g
JOIN vowsls_to_sls_transfer.proc_gst_removed_20260823 b ON b.gst_invoice_type = g.gst_invoice_type
WHERE g.gst_invoice_type BETWEEN 49431 AND 49440;

-- ROLLBACK (all rows removed by this file)
--   INSERT INTO proc_gst (gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
--       i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
--       active, updated_by, updated_date_time, proc_inward_additional_id)
--   SELECT gst_invoice_type, proc_inward_dtl, tax_pct, stax_percentage, s_tax_amount,
--       i_tax_amount, i_tax_percentage, c_tax_amount, c_tax_percentage, tax_amount,
--       active, updated_by, updated_date_time, proc_inward_additional_id
--   FROM vowsls_to_sls_transfer.proc_gst_removed_20260823 WHERE gst_invoice_type BETWEEN 49431 AND 49440
