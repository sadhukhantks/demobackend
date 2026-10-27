-- =============================================================================
-- Migration: ITM-004 Power readings -- portal menu TEMPLATE rows (vowconsole3)
-- =============================================================================
-- Date: 2026-10-18
-- TARGET: vowconsole3 ONLY -- run this file exactly once, against vowconsole3.
-- Pair:   20261018_add_power_menus.sql (the tenant rows + grants, dev3 first)
-- Spec:   Empire Jute Mill AI GM and team/itmis/prompts/ITM-004-power-readings.md
--
-- This is the MASTER TEMPLATE half: it seeds portal_menu_mst, the control-desk
-- template a brand new tenant is provisioned from. It puts nothing in front of
-- an existing tenant's users by itself -- that is the tenant file's job.
-- vowconsole3 has NO role_menu_map of its own, so nothing here touches grants.
--
-- Follows seed_jute_mech_menus_console.sql exactly. COLUMN DIFFERENCES vs the
-- tenant menu_mst: the FK column is `module_id` (not module_mst_id), there is
-- NO `report` column, and menu_type_id / module_id are NOT NULL so both are
-- inherited from the parent template row.
--
--   level 2  'Power Readings'    juteMechanical/powerReading         order 245
--            under 'Jute Mechanical & Maintenance'
--   level 3  'Power & DG Report' juteMechanical/reports/powerReport  order 273
--            under 'Maintenance Reports'
--
-- DEFENSIVE BY CONSTRUCTION: no id is hardcoded. Parents resolve by menu_name
-- (UNIQUE). If an anchor fails to resolve the INSERT is a silent no-op --
-- run the VERIFICATION query at the end.
--
-- IDEMPOTENT: every INSERT is guarded by NOT EXISTS on menu_name. Safe to re-run.
--
-- RUNNER NOTE: the pymysql runner (run-migration skill) splits this file on
-- the literal semicolon character and skips any chunk whose trimmed text
-- starts with '--'. Every comment block is therefore terminated by a lone
-- semicolon on its own line, and no comment prose contains a mid-line
-- semicolon. There are no DELIMITER blocks.
;

-- -----------------------------------------------------------------------------
-- COLLISION PRE-CHECK -- run BEFORE applying. Expect ZERO rows back
-- (checked read-only on vowconsole3 on 2026-10-18: none).
-- -----------------------------------------------------------------------------
--   SELECT menu_id, menu_name, menu_path FROM portal_menu_mst
--    WHERE menu_name IN ('Power Readings', 'Power & DG Report')
;

-- =============================================================================
-- STEP 1 -- portal_menu_mst : level-2 entry page under 'Jute Mechanical & Maintenance'
-- =============================================================================
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Power Readings', CONCAT(p.menu_path, '/powerReading'), 1, p.menu_id, p.menu_type_id, 'bolt',
       p.module_id, 245
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Power Readings');

-- =============================================================================
-- STEP 2 -- portal_menu_mst : level-3 report row under 'Maintenance Reports'
-- =============================================================================
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Power & DG Report', CONCAT(p.menu_path, '/powerReport'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 273
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Power & DG Report');

-- =============================================================================
-- VERIFICATION -- run manually against vowconsole3 after applying
-- =============================================================================
--   -- EXPECT two rows: powerReading (245) under 'Jute Mechanical & Maintenance',
--   -- reports/powerReport (273) under 'Maintenance Reports', both active = 1
--   SELECT m.menu_id, m.menu_name, m.menu_path, p.menu_name AS parent_name,
--          m.order_by, m.module_id, m.menu_type_id, m.active
--     FROM portal_menu_mst m
--     LEFT JOIN portal_menu_mst p ON p.menu_id = m.menu_parent_id
--    WHERE m.menu_path LIKE 'juteMechanical/%power%'
--    ORDER BY m.order_by
;

-- =============================================================================
-- ROLLBACK -- run against vowconsole3
-- =============================================================================
--   DELETE FROM portal_menu_mst
--    WHERE menu_path IN ('juteMechanical/powerReading', 'juteMechanical/reports/powerReport')
-- =============================================================================
