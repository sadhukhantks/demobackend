-- =============================================================================
-- Migration: Yarn Purchase line rate is per KILOGRAM, not per quintal
-- =============================================================================
-- Date: 2026-09-26 (owner ruling). Column rate_per_qtl -> rate_per_kg on
-- jute_yarn_purchase_li. Amount stays kg x rate (previously kg / 100 x rate),
-- so existing rates are divided by 100 to keep every stored amount unchanged.
-- TARGET: every tenant that has the yarn tables (dev3, sls). Idempotent: the
-- UPDATE only touches rows whose amount still reads as a per-quintal figure.
-- Tenants created after this date get rate_per_kg straight from
-- 20260925_yarn_purchase_issue_tables.sql, which now carries the new name.
;

ALTER TABLE jute_yarn_purchase_li RENAME COLUMN rate_per_qtl TO rate_per_kg;

UPDATE jute_yarn_purchase_li
SET rate_per_kg = ROUND(rate_per_kg / 100, 2)
WHERE rate_per_kg IS NOT NULL
  AND amount IS NOT NULL
  AND ABS(amount - ROUND(weight_kg / 100 * rate_per_kg, 2)) < 0.01
  AND ABS(amount - ROUND(weight_kg * rate_per_kg, 2)) >= 0.01;

-- =============================================================================
-- VERIFICATION
--   SHOW COLUMNS FROM jute_yarn_purchase_li LIKE 'rate_per_kg';
--   SELECT yarn_purchase_li_id, weight_kg, rate_per_kg, amount,
--          ROUND(weight_kg * rate_per_kg, 2) AS recomputed
--     FROM jute_yarn_purchase_li WHERE rate_per_kg IS NOT NULL;
-- ROLLBACK
--   UPDATE jute_yarn_purchase_li SET rate_per_kg = ROUND(rate_per_kg * 100, 2)
--    WHERE rate_per_kg IS NOT NULL;
--   ALTER TABLE jute_yarn_purchase_li RENAME COLUMN rate_per_kg TO rate_per_qtl;
-- =============================================================================
