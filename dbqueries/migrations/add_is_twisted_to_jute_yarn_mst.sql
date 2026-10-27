-- Migration: Add is_twisted discriminator to jute_yarn_mst (TKT-000127)
-- Date: 2026-08-27
-- Database: tenant_db (run dev3 first, sls needs explicit approval)
-- Author: migration-writer agent
--
-- Why: since 2026-08-21 the mill twists single yarn into 3-ply. The 3-ply output is
-- keyed as ordinary winding doff production but the twisting INPUT (single-yarn
-- consumption) is not captured, so winding-vs-weaving reconciliation drifts by ~2x
-- the twisting tonnage. This additive flag lets the reports net twisted output out.

-- ============================================================
-- FORWARD MIGRATION
-- ============================================================

ALTER TABLE jute_yarn_mst
    ADD COLUMN is_twisted TINYINT(1) NOT NULL DEFAULT 0 AFTER std_mr_pct;

-- Seed: flag 3-ply yarns. Space-insensitive so '3PLY', '3 PLY' and '3-PLY' all match.
-- Matches 0 rows on tenants without a 3-ply yarn (dev3) — that is expected, not an error.
UPDATE jute_yarn_mst y
LEFT JOIN item_mst i ON i.item_id = y.item_id
SET y.is_twisted = 1
WHERE REPLACE(REPLACE(UPPER(COALESCE(i.item_name, '')), ' ', ''), '-', '') LIKE '%3PLY%'
   OR REPLACE(REPLACE(UPPER(COALESCE(i.item_code, '')), ' ', ''), '-', '') LIKE '%3PLY%'
   OR REPLACE(REPLACE(UPPER(COALESCE(y.jute_yarn_name, '')), ' ', ''), '-', '') LIKE '%3PLY%';

-- ============================================================
-- ROLLBACK (uncomment to reverse)
-- ============================================================
-- ALTER TABLE jute_yarn_mst DROP COLUMN is_twisted;
