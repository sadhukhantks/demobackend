-- Migration: Seed the "Jute MR Wise Issue Detail" report menu row + role grants
-- Date: 2026-08-22
-- Database: tenant DB (dev3 first; other tenants only after confirming role ids exist)
-- Author: migration-writer agent
--
-- Data-only: menu_mst + role_menu_map rows. No DDL.
--
-- Notes:
--   - Parent = menu_id 10 ("Jute Report", menu_path 'jutePurchase/reports').
--   - report=1 puts the row in the reports dropdown (get_report_menu_tree filters
--     report=1, active=1, AND requires a role_menu_map grant for one of the
--     caller's roles — a row with no grant is invisible).
--   - menu_path has NO '/dashboardportal' prefix and NO leading slash (the hub
--     prepends it on router.push), matching every sibling under parent 10.
--   - order_by 180: existing children of 10 occupy 10..170 on both dev3 and sls.
--   - menu_name is UNIQUE -> INSERT IGNORE makes this re-runnable.
--   - Role grants are scoped to THIS menu_path only (not the whole
--     'jutePurchase/reports/%' prefix), so a re-run cannot back-fill grants onto
--     sibling reports.
--   - Roles {1,3,12} = the dev3 audience of parent menu 10. Tenants with a
--     different roles_mst id space (e.g. sls: 13..53) need their own role list;
--     role_menu_map.role_id is FK'd to roles_mst, so a wrong id errors 1452.

-- =============================================================================
-- FORWARD MIGRATION
-- =============================================================================
INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute MR Wise Issue Detail', 'jutePurchase/reports/mrIssueDetail', 1, 10, NULL, 'assessment', NULL, 180, 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, m.menu_id, 4, 1
  FROM (SELECT 1 AS role_id UNION SELECT 3 UNION SELECT 12) r
  CROSS JOIN (
      SELECT menu_id FROM menu_mst
       WHERE menu_path = 'jutePurchase/reports/mrIssueDetail' AND menu_parent_id = 10
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
--  WHERE m.menu_path = 'jutePurchase/reports/mrIssueDetail';
-- DELETE FROM menu_mst
--  WHERE menu_path = 'jutePurchase/reports/mrIssueDetail' AND menu_parent_id = 10;
-- =============================================================================
