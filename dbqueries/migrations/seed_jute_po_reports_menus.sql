-- Migration: Seed the two Jute PO report menu rows + role grants (TKT-000115)
--   1. Jute PO Wise MR Receipt Detail  -> jutePurchase/reports/poMrReceiptDetail
--   2. Jute Pending PO                 -> jutePurchase/reports/poPending
-- Date: 2026-08-31
-- Database: tenant DB (dev3 first; sls via seed_jute_po_reports_menus_sls.sql)
--
-- Data-only: menu_mst + role_menu_map rows. No DDL.
--
-- Notes (same conventions as seed_jute_mr_issue_detail_menu.sql):
--   - Parent = menu_id 10 ("Jute Report", menu_path 'jutePurchase/reports').
--   - report=1 puts the rows in the reports dropdown (get_report_menu_tree
--     filters report=1, active=1, and requires a role_menu_map grant).
--   - menu_path has NO '/dashboardportal' prefix and NO leading slash.
--   - order_by 210/220: existing children of 10 occupy 10..200 on dev3 and sls
--     (verified 2026-08-31).
--   - menu_name is UNIQUE -> INSERT IGNORE makes this re-runnable.
--   - Roles {1,3,12} = the dev3 audience of parent menu 10.

-- =============================================================================
-- FORWARD MIGRATION
-- =============================================================================
INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute PO Wise MR Receipt Detail', 'jutePurchase/reports/poMrReceiptDetail', 1, 10, NULL, 'assessment', NULL, 210, 1;

INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Pending PO', 'jutePurchase/reports/poPending', 1, 10, NULL, 'assessment', NULL, 220, 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, m.menu_id, 4, 1
  FROM (SELECT 1 AS role_id UNION SELECT 3 UNION SELECT 12) r
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
