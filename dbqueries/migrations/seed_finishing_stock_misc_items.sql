-- Migration: Finishing Stock — misc loose categories as item_mst items +
--            jute_prod_finishing_quality rows.
-- Date: 2026-09-17
-- Spec: docs/superpowers/specs/2026-09-17-finishing-stock-design.md §2 (Seeds)
-- Applies to: dev3 (QA) as written. Re-runnable — every insert is NOT EXISTS guarded.
--
-- The loose-stock picker shows ONLY jute_prod_finishing_quality rows, so the
-- shop-floor sheet's non-cloth categories (joints, rejected packsheet, spool bag,
-- short bags …) need a real item + quality row or they cannot be counted at all.
--
-- TENANT VARIABLES — change this block only.
--   dev3 : co 1, branch 2, hessian group 625 (HESSIAN(2)), sacking group 626 (SACKING), uom 166 (MT), updated_by 1
--   sls  : SET @co_id = 2, @branch_id = 29, @hess_grp = 2269, @sack_grp = 2270;
--          @uom_id / @updated_by: confirm against sls uom_mst ('MT') and a real user_id
--          BEFORE running — do not run this file half-edited.
--   Block 3 below is SQL-fenced to @co_id = 1 on dev3 and is a no-op elsewhere.
--   APPLIED: dev3 2026-09-17 (values below); sls 2026-09-17 with @co_id=2, @branch_id=29,
--            @hess_grp=2269, @sack_grp=2270, @uom_id=166, @updated_by=19 -> items 335969-335974,
--            finishing_quality_id 20-25; block 3 skipped as designed.

SET @co_id      = 1;
SET @branch_id  = 2;
SET @hess_grp   = 625;
SET @sack_grp   = 626;
SET @uom_id     = 166;
SET @updated_by = 1;

-- -----------------------------------------------------------------------------
-- 1. item_mst rows — guarded on (item_name, item_grp_id)
-- -----------------------------------------------------------------------------

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @hess_grp, 'HESS-JOINT', 'HESSIAN JOINT', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'HESSIAN JOINT' AND item_grp_id = @hess_grp);

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @hess_grp, 'HESS-REJ-PS', 'REJECTED P/SHEET', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'REJECTED P/SHEET' AND item_grp_id = @hess_grp);

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @hess_grp, 'HESS-SPOOL-BAG', 'SPOOL BAG', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'SPOOL BAG' AND item_grp_id = @hess_grp);

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @sack_grp, 'SACK-PACKSHEET', 'PACKSHEET', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'PACKSHEET' AND item_grp_id = @sack_grp);

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @sack_grp, 'SACK-JOINT-BAG', 'JOINT BAG', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'JOINT BAG' AND item_grp_id = @sack_grp);

INSERT INTO item_mst (item_grp_id, item_code, item_name, tangible, saleable, manufacturable, uom_id, active, updated_by)
SELECT @sack_grp, 'SACK-SHORT-BAG', 'SHORT BAG', 1, 1, 1, @uom_id, 1, @updated_by FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM item_mst WHERE item_name = 'SHORT BAG' AND item_grp_id = @sack_grp);

-- -----------------------------------------------------------------------------
-- 2. jute_prod_finishing_quality rows — one per seeded item.
--    quality_type 1 = Hessian, 2 = Sacking. std_bale_weight stays NULL: these
--    categories are never pressed into bales, only counted loose.
--    Guarded on (co_id, item_id, active = 1).
-- -----------------------------------------------------------------------------

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 1, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'HESSIAN JOINT' AND item_grp_id = @hess_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 1, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'REJECTED P/SHEET' AND item_grp_id = @hess_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 1, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'SPOOL BAG' AND item_grp_id = @hess_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 2, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'PACKSHEET' AND item_grp_id = @sack_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 2, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'JOINT BAG' AND item_grp_id = @sack_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, active, updated_by)
SELECT @co_id, @branch_id, 2, i.item_id, 1, @updated_by
FROM (SELECT item_id FROM item_mst WHERE item_name = 'SHORT BAG' AND item_grp_id = @sack_grp LIMIT 1) i
WHERE NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = i.item_id AND q.active = 1);

-- -----------------------------------------------------------------------------
-- 3. dev3-ONLY QA data. item 268939 = 'HESSIAN CLOTH 38-8.12 OZ/40 11X10 PLAIN
--    I/B 2000 YDS', the quality the reference sheet bales at 437.384 kg. Gives
--    browser QA a bale quality whose std weight is neither round nor the
--    existing 400.000, so a defaulted per-bale weight is visibly the quality's.
--    SQL-fenced: inserts only when @co_id = 1 AND item 268939 really is in the
--    dev3 hessian group — a no-op on every other tenant even if left in place.
-- -----------------------------------------------------------------------------

INSERT INTO jute_prod_finishing_quality (co_id, branch_id, quality_type, item_id, std_bale_weight, active, updated_by)
SELECT @co_id, @branch_id, 1, 268939, 437.384, 1, @updated_by FROM DUAL
WHERE @co_id = 1
  AND EXISTS (SELECT 1 FROM item_mst im WHERE im.item_id = 268939 AND im.item_grp_id = @hess_grp AND im.active = 1)
  AND NOT EXISTS (SELECT 1 FROM jute_prod_finishing_quality q WHERE q.co_id = @co_id AND q.item_id = 268939 AND q.active = 1);

-- =============================================================================
-- ROLLBACK (dev3 values ONLY — re-key co_id/item ids for another tenant):
--   DELETE FROM jute_prod_finishing_quality
--    WHERE co_id = 1
--      AND item_id IN (
--        SELECT item_id FROM (SELECT item_id FROM item_mst WHERE item_name IN
--          ('HESSIAN JOINT','REJECTED P/SHEET','SPOOL BAG','PACKSHEET','JOINT BAG','SHORT BAG')
--          AND item_grp_id IN (625, 626)) x);
--   DELETE FROM jute_prod_finishing_quality WHERE co_id = 1 AND item_id = 268939;
--   DELETE FROM item_mst WHERE item_name IN
--     ('HESSIAN JOINT','REJECTED P/SHEET','SPOOL BAG','PACKSHEET','JOINT BAG','SHORT BAG')
--     AND item_grp_id IN (625, 626);
-- =============================================================================
