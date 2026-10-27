-- =============================================================================
-- Migration: MIS port -- 5 Jute Production report menus
-- =============================================================================
-- Date: 2026-08-25
-- TARGET: tenant DB only -- run once against dev3, once against sls.
--   .venv/Scripts/python.exe tools/run_sql_migration.py dev3 dbqueries/migrations/20260825_add_mis_report_menus.sql
--
-- Seeds 5 rows under the 'Production Reports' hub (menu_path
-- 'juteProduction/report', menu_id 945 on both dev3 and sls -- resolved by PATH
-- here, never by id, so the script promotes to any tenant unmodified):
--   Weaving Low Producer   -> juteProduction/report/weavingLowProducer
--   Spinning Low Producer  -> juteProduction/report/spinningLowProducer
--   Winding Low Producer   -> juteProduction/report/windingLowProducer
--   Doff Analysis          -> juteProduction/report/doffAnalysis
--   Hands Report           -> juteProduction/report/handsReport
--
-- Each menu_path MUST equal the Next.js route folder under
-- vowerp3ui/src/app/dashboardportal/ -- all 5 folders verified present.
-- Backend: 5 routers under prefix /api/juteProductionReports (src/main.py).
--
-- report = 1 is what puts a row in the hub dropdown (fetchReportMenuTree filters
-- report = 1 children of the hub). order_by continues the existing 10..54 run.
--
-- ROLE GRANTS are explicit per tenant, keyed off DATABASE() so one file covers
-- both -- access_type_id copies what each role already holds on the sibling
-- report rows (verified read-only 2026-08-25):
--   dev3 -> role 1 4, role 3 4, role 12 4
--   sls  -> role 13 4, role 14 1
-- On any other tenant the role table matches nothing and the grant INSERT is a
-- silent no-op, leaving the menus ungranted rather than guessing an audience.
--
-- vowconsole3.portal_menu_mst is deliberately NOT touched: it holds no
-- juteProduction rows at all (verified -- zero matches on menu_path LIKE
-- '%juteProduction%') and has no `report` column, so there is no hub template to
-- parent a report row to. Every one of the 9 existing sibling report rows is
-- tenant-only for the same reason -- see the note at the top of
-- 20260819_winding_quality_wise_menus.sql. Templating the whole juteProduction
-- tree into portal_menu_mst is a separate job, not this migration.
--
-- Runner note: the pymysql migration runner splits on ';' and skips chunks that
-- start with '--', so each comment block below is terminated by a lone ';' and
-- no ';' appears inside any string.
;

-- 1. Weaving Low Producer
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Weaving Low Producer', 'juteProduction/report/weavingLowProducer', 1, p.menu_id, NULL, 'trending_down', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/weavingLowProducer');

-- 2. Spinning Low Producer
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Spinning Low Producer', 'juteProduction/report/spinningLowProducer', 1, p.menu_id, NULL, 'trending_down', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/spinningLowProducer');

-- 3. Winding Low Producer
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Winding Low Producer', 'juteProduction/report/windingLowProducer', 1, p.menu_id, NULL, 'trending_down', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/windingLowProducer');

-- 4. Doff Analysis
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Doff Analysis', 'juteProduction/report/doffAnalysis', 1, p.menu_id, NULL, 'analytics', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/doffAnalysis');

-- 5. Hands Report
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Hands Report', 'juteProduction/report/handsReport', 1, p.menu_id, NULL, 'groups', 1, p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/handsReport');

-- 6. role_menu_map -- explicit per-tenant grants for all 5 menus at once.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT 'dev3' AS db_name, 1  AS role_id, 4 AS access_type_id UNION ALL
    SELECT 'dev3', 3,  4 UNION ALL
    SELECT 'dev3', 12, 4 UNION ALL
    SELECT 'sls',  13, 4 UNION ALL
    SELECT 'sls',  14, 1
) r
CROSS JOIN menu_mst m
WHERE r.db_name = DATABASE()
  AND m.menu_path IN (
      'juteProduction/report/weavingLowProducer',
      'juteProduction/report/spinningLowProducer',
      'juteProduction/report/windingLowProducer',
      'juteProduction/report/doffAnalysis',
      'juteProduction/report/handsReport'
  )
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map x
      WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
  );

-- =============================================================================
-- VERIFICATION -- run manually after applying (expect 5 menu rows, and
-- 15 grants on dev3 / 10 on sls)
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by, active
--     FROM menu_mst
--    WHERE menu_path IN ('juteProduction/report/weavingLowProducer',
--                        'juteProduction/report/spinningLowProducer',
--                        'juteProduction/report/windingLowProducer',
--                        'juteProduction/report/doffAnalysis',
--                        'juteProduction/report/handsReport')
--    ORDER BY order_by
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path LIKE 'juteProduction/report/%LowProducer'
--       OR m.menu_path IN ('juteProduction/report/doffAnalysis',
--                          'juteProduction/report/handsReport')
--    ORDER BY m.menu_name, r.role_id
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users
-- MUST log out and back in before the entries appear in the Production Reports
-- dropdown. The pages 404 until the FE build carrying them is deployed.
-- NOTE: Hands Report additionally needs jute_prod_hands_norms_mst created AND seeded on the
-- tenant (20260825_add_jute_prod_hands_norms_mst.sql) -- it is dev3-only as of this date.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_path IN ('juteProduction/report/weavingLowProducer',
--                          'juteProduction/report/spinningLowProducer',
--                          'juteProduction/report/windingLowProducer',
--                          'juteProduction/report/doffAnalysis',
--                          'juteProduction/report/handsReport')
--
--   DELETE FROM menu_mst
--    WHERE menu_path IN ('juteProduction/report/weavingLowProducer',
--                        'juteProduction/report/spinningLowProducer',
--                        'juteProduction/report/windingLowProducer',
--                        'juteProduction/report/doffAnalysis',
--                        'juteProduction/report/handsReport')
-- =============================================================================
