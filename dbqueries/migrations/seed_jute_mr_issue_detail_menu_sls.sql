-- Migration: Seed the "Jute MR Wise Issue Detail" report menu row + role grants (sls)
-- Date: 2026-08-22
-- Database: sls (production tenant)
-- Author: migration-writer agent
--
-- Data-only: menu_mst + role_menu_map rows. No DDL.
--
-- sls companion to seed_jute_mr_issue_detail_menu.sql. The menu_mst INSERT is
-- identical; only the role list differs.
--
-- Why a separate script: role_menu_map.role_id is FK'd to roles_mst, and the
-- two tenants have DISJOINT role id spaces — dev3 is 1..12, sls is 13..53.
-- Running the dev3 script here would abort with error 1452 on roles 1/3/12.
--
-- Roles: ADMINS ONLY, by explicit decision (2026-08-22).
--   13 slssuperadmin, 14 MIS ADMIN — both access_type 4 (edit).
-- Deliberately NOT granted: the 20 operational read roles (17, 21-37, 52, 53)
-- that hold access_type 1 on the sibling jute reports. This report stays
-- invisible to them until someone widens it later.
--
-- Notes:
--   - Parent = menu_id 10 ("Jute Report", menu_path 'jutePurchase/reports').
--   - report=1 puts the row in the reports dropdown (get_report_menu_tree filters
--     report=1, active=1, AND requires a role_menu_map grant for one of the
--     caller's roles — a row with no grant is invisible).
--   - menu_path has NO '/dashboardportal' prefix and NO leading slash (the hub
--     prepends it on router.push), matching every sibling under parent 10.
--   - order_by 180: sls children of 10 occupy 10..170 (verified 2026-08-22).
--   - menu_name is UNIQUE -> INSERT IGNORE makes this re-runnable.
--   - Grants are scoped to THIS menu_path only (not the
--     'jutePurchase/reports/%' prefix), so a re-run cannot back-fill grants onto
--     the 17 sibling reports.
--   - menu_id will NOT match dev3's (985). The FE resolves this report by
--     menu_path, so the divergence is expected and fine.

-- =============================================================================
-- FORWARD MIGRATION
-- =============================================================================
INSERT IGNORE INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute MR Wise Issue Detail', 'jutePurchase/reports/mrIssueDetail', 1, 10, NULL, 'assessment', NULL, 180, 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, m.menu_id, 4, 1
  FROM (SELECT 13 AS role_id UNION SELECT 14) r
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
