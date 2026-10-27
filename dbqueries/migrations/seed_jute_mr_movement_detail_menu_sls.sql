-- Migration: Seed the "Jute MR Wise Issue & Sales Detail" report menu + grants (sls)
-- Date: 2026-08-23
-- Database: sls (production tenant)
--
-- Data-only: menu_mst + role_menu_map rows. No DDL.
--
-- sls companion to seed_jute_mr_movement_detail_menu.sql. The menu_mst INSERT
-- is identical; only the role list differs.
--
-- Why a separate script: role_menu_map.role_id is FK'd to roles_mst, and the
-- two tenants have DISJOINT role id spaces — dev3 is 1..12, sls is 13..53.
-- Running the dev3 script here would abort with error 1452 on roles 1/3/12.
--
-- Roles: ADMINS ONLY, matching the sibling Issue Detail report's 2026-08-22
-- decision.
--   13 slssuperadmin, 14 MIS ADMIN — both access_type 4 (edit).
-- Deliberately NOT granted: the 20 operational read roles (17, 21-37, 52, 53).
-- On sls the sales side is mostly unattributable — sales_invoice_jute.mr_id
-- resolves for 5,885 invoices but 5,673 of those name an MR in a different
-- branch, and only 100 of 12,251 invoice lines sell an item the referenced MR
-- actually held. Everything that fails the guard lands in the report's
-- "Sale (unattributed)" block, which needs an admin to read correctly.
--
-- Notes:
--   - Parent = menu_id 10 ("Jute Report", menu_path 'jutePurchase/reports').
--   - order_by 190: 180 is Jute MR Wise Issue Detail (sls menu_id 1000).
--   - menu_name is UNIQUE -> INSERT IGNORE makes this re-runnable.
--   - menu_id will NOT match dev3's. The FE resolves by menu_path, so that
--     divergence is expected and fine.
--   - NOT touched here: the parked stub 'jutePurchase/reports/mrWiseSales'
--     ("Jute MR Wise Sales", sls menu_id 952), which this report supersedes.
--     Retiring it is a separate, user-approved change.

-- =============================================================================
-- FORWARD MIGRATION
-- =============================================================================
INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute MR Wise Issue & Sales Detail', 'jutePurchase/reports/mrMovementDetail', 1, 10, NULL, 'assessment', NULL, 190, 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, m.menu_id, 4, 1
  FROM (SELECT 13 AS role_id UNION SELECT 14) r
  CROSS JOIN (
      SELECT menu_id FROM menu_mst
       WHERE menu_path = 'jutePurchase/reports/mrMovementDetail' AND menu_parent_id = 10
  ) m
 WHERE NOT EXISTS (
      SELECT 1 FROM role_menu_map rm
       WHERE rm.role_id = r.role_id AND rm.menu_id = m.menu_id
 );

-- =============================================================================
-- ROLLBACK (uncomment to reverse)
-- =============================================================================
-- DELETE rm FROM role_menu_map rm
--   JOIN menu_mst m ON m.menu_id = rm.menu_id
--  WHERE m.menu_path = 'jutePurchase/reports/mrMovementDetail';
-- DELETE FROM menu_mst
--  WHERE menu_path = 'jutePurchase/reports/mrMovementDetail' AND menu_parent_id = 10;
-- =============================================================================
