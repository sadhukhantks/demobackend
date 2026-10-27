-- =============================================================================
-- Migration: Bale despatch WITHOUT an invoice (owner option 2, 2026-10-12)
-- Date: 2026-10-12
-- Spec: docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §7b
-- Applies to: tenant DBs where 20261012_jute_prod_bale_inspection_despatch.sql
--             is applied (dev3, sls). Run AFTER that script.
-- APPLIED: dev3 2026-10-12; sls 2026-10-12 (owner-requested).
--
-- Bales can leave the mill before the sales invoice exists (delivery challan /
-- delivery order, transfer to another unit, sample). The despatch row therefore
-- no longer requires an invoice: invoice_id becomes NULLable and two columns
-- record what the bale left on instead. A later "Link to invoice" fills
-- invoice_id in on the same row (the despatch date stays the day it left).
--   despatch_reason — constants.BALE_DESPATCH_REASONS code (CHALLAN,
--                     DELIVERY_ORDER, TRANSFER, SAMPLE, OTHER); required by the
--                     API when invoice_id is NULL, optional otherwise
--   despatch_ref    — challan / delivery-order / gate-pass number, free text
-- Backward compatible: existing rows keep their invoice_id; nothing is rewritten.
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

ALTER TABLE jute_prod_bale_despatch
    MODIFY COLUMN invoice_id BIGINT NULL COMMENT 'sales_invoice.invoice_id, NULL = despatched without invoice (see despatch_reason)',
    ADD COLUMN despatch_reason VARCHAR(20)  NULL AFTER despatch_date,
    ADD COLUMN despatch_ref    VARCHAR(100) NULL AFTER despatch_reason;

-- VERIFICATION
;
SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_bale_despatch'
  AND COLUMN_NAME IN ('invoice_id', 'despatch_reason', 'despatch_ref')
ORDER BY ORDINAL_POSITION;

-- =============================================================================
-- ROLLBACK (only valid while no row has invoice_id NULL):
--   ALTER TABLE jute_prod_bale_despatch
--       DROP COLUMN despatch_ref,
--       DROP COLUMN despatch_reason,
--       MODIFY COLUMN invoice_id BIGINT NOT NULL;
-- =============================================================================
;
