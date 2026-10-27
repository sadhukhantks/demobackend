-- =============================================================================
-- Migration: Spinning Production Trend -- Jute Production report menu
-- =============================================================================
-- Date: 2026-10-07
-- TARGET: tenant DB only -- run once against dev3, once against sls.
--   python tools/run_sql_migration.py dev3 dbqueries/migrations/20261007_add_spinning_production_trend_menu.sql
--
-- Seeds ONE row under the 'Production Reports' hub (menu_path
-- 'juteProduction/report', resolved by PATH, never by id):
--   Spinning Production Trend -> juteProduction/report/spinningProductionTrend
--
-- menu_path equals the Next.js route folder under vowerp3ui/src/app/dashboardportal/.
-- Backend: spinning_trend_router under prefix /api/juteProductionReports (src/main.py).
-- Design note: docs/superpowers/specs/2026-10-07-spinning-production-trend-design.md
--
-- report = 1 puts the row in the hub dropdown (fetchReportMenuTree filters
-- report = 1 children of the hub). order_by continues the hub's run.
--
-- ROLE GRANTS copy what each tenant's roles already hold on the sibling
-- 'Doff Analysis' row (verified read-only 2026-10-07), keyed off DATABASE():
--   dev3 -> roles 1, 3, 12 (access 4)
--   sls  -> role 13 (access 4); roles 14, 33, 53, 54, 56, 59, 61 (access 1)
-- On any other tenant the grant INSERT is a silent no-op.
--
-- vowconsole3.portal_menu_mst is deliberately NOT touched (no juteProduction
-- rows there, no `report` column -- see 20260825_add_mis_report_menus.sql).
--
-- Runner note: the pymysql migration runner splits on ';' and skips chunks that
-- start with '--', so each comment block is terminated by a lone ';'.
;

-- 1. Menu row
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Spinning Production Trend', 'juteProduction/report/spinningProductionTrend', 1, p.menu_id, NULL, 'show_chart', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/spinningProductionTrend');

-- 2. role_menu_map -- explicit per-tenant grants
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT 'dev3' AS db_name, 1  AS role_id, 4 AS access_type_id UNION ALL
    SELECT 'dev3', 3,  4 UNION ALL
    SELECT 'dev3', 12, 4 UNION ALL
    SELECT 'sls',  13, 4 UNION ALL
    SELECT 'sls',  14, 1 UNION ALL
    SELECT 'sls',  33, 1 UNION ALL
    SELECT 'sls',  53, 1 UNION ALL
    SELECT 'sls',  54, 1 UNION ALL
    SELECT 'sls',  56, 1 UNION ALL
    SELECT 'sls',  59, 1 UNION ALL
    SELECT 'sls',  61, 1
) r
CROSS JOIN menu_mst m
WHERE r.db_name = DATABASE()
  AND m.menu_path = 'juteProduction/report/spinningProductionTrend'
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map x
      WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
  );

-- =============================================================================
-- VERIFICATION -- expect 1 menu row; 3 grants on dev3 / 8 on sls
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by, active
--     FROM menu_mst WHERE menu_path = 'juteProduction/report/spinningProductionTrend';
--   SELECT rm.role_id, rm.access_type_id FROM role_menu_map rm
--     JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/spinningProductionTrend';
--
-- ROLLBACK
--   DELETE rm FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/spinningProductionTrend';
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/report/spinningProductionTrend';
;
