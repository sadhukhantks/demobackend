-- Migration: Seed the two Jute PO report menu rows + role grants (sls, TKT-000115)
--   1. Jute PO Wise MR Receipt Detail  -> jutePurchase/reports/poMrReceiptDetail
--   2. Jute Pending PO                 -> jutePurchase/reports/poPending
-- Date: 2026-08-31
-- Database: sls (production tenant)
--
-- Data-only: menu_mst + role_menu_map rows. No DDL.
--
-- sls companion to seed_jute_po_reports_menus.sql. The menu_mst INSERTs are
-- identical; only the role list differs — the two tenants have DISJOINT
-- roles_mst id spaces (dev3 1..12, sls 13..53) and role_menu_map.role_id is
-- FK'd, so the dev3 script would abort with 1452 here.
--
-- Roles: ADMINS ONLY to start, mirroring the 2026-08-22 decision for the
-- MR detail reports: 13 slssuperadmin, 14 MIS ADMIN — access_type 4.
-- The reporter of TKT-000115 (slsuser, branch 29) may hold an operational
-- role instead: CONFIRM the audience before running, and if it must be wide
-- use the commented block below (the ~20 operational read roles that hold
-- access_type 1 on the sibling jute reports).
--
-- Notes:
--   - Parent = menu_id 10; order_by 210/220 (sls children occupy 10..200,
--     verified 2026-08-31). menu_name UNIQUE -> INSERT IGNORE re-runnable.
--   - menu_ids will NOT match dev3's; the FE resolves by menu_path.

-- =============================================================================
-- FORWARD MIGRATION
-- =============================================================================
INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute PO Wise MR Receipt Detail', 'jutePurchase/reports/poMrReceiptDetail', 1, 10, NULL, 'assessment', NULL, 210, 1;

INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Pending PO', 'jutePurchase/reports/poPending', 1, 10, NULL, 'assessment', NULL, 220, 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, m.menu_id, 4, 1
  FROM (SELECT 13 AS role_id UNION SELECT 14) r
  CROSS JOIN (
      SELECT menu_id FROM menu_mst
       WHERE menu_path IN ('jutePurchase/reports/poMrReceiptDetail',
                           'jutePurchase/reports/poPending')
         AND menu_parent_id = 10
  ) m
 WHERE NOT EXISTS (
      SELECT 1 FROM role_menu_map rm
       WHERE rm.role_id = r.role_id AND rm.menu_id = m.menu_id
 );

-- OPTIONAL WIDE GRANT (run only if the user decides these reports go to the
-- operational roles too — read access, mirroring the sibling jute reports):
-- INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
-- SELECT rm0.role_id, m.menu_id, 1, 1
--   FROM (SELECT DISTINCT rm.role_id
--           FROM role_menu_map rm
--           JOIN menu_mst pm ON pm.menu_id = rm.menu_id
--          WHERE pm.menu_path = 'jutePurchase/reports/mrList') rm0
--   CROSS JOIN (
--       SELECT menu_id FROM menu_mst
--        WHERE menu_path IN ('jutePurchase/reports/poMrReceiptDetail',
--                            'jutePurchase/reports/poPending')
--          AND menu_parent_id = 10
--   ) m
--  WHERE NOT EXISTS (
--       SELECT 1 FROM role_menu_map rm
--        WHERE rm.role_id = rm0.role_id AND rm.menu_id = m.menu_id
--  );

-- =============================================================================
-- ROLLBACK (uncomment to reverse)
-- =============================================================================
-- DELETE rm FROM role_menu_map rm
--   JOIN menu_mst m ON m.menu_id = rm.menu_id
--  WHERE m.menu_path IN ('jutePurchase/reports/poMrReceiptDetail',
--                        'jutePurchase/reports/poPending');
-- DELETE FROM menu_mst
--  WHERE menu_path IN ('jutePurchase/reports/poMrReceiptDetail',
--                      'jutePurchase/reports/poPending')
--    AND menu_parent_id = 10;
-- =============================================================================
