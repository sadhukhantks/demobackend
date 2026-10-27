-- =============================================================================
-- Migration: Daily Production Report (DPR) -- tenant menu wiring (SQL only)
-- =============================================================================
-- Date: 2026-08-14
-- Adds the Portal report page
--   'Daily Production Report' -> /dashboardportal/juteProduction/report/dailyProductionReport
-- to the Jute Production > Production Reports hub. The page is served by the
-- reports-hub dropdown (menu_mst.report = 1 rows parented to the hub). Route
-- permission walks path prefixes, so a hub grant already opens the page by
-- URL -- the role_menu_map rows below make the grant explicit per role.
--
-- TARGET: the TENANT DB only (dev3, then sls when FE+BE are deployed there).
-- Run via the run-migration skill (pymysql). Only tenants that already have
-- the 'Production Reports' hub (menu_path 'juteProduction/report') get a row:
-- today that is dev3 and sls. Tenants whose menu_mst lacks the `report`
-- column (e.g. vownjm) would error on B1 -- do not blanket-run.
--
-- vowconsole3.portal_menu_mst is deliberately NOT touched: it has no
-- juteProduction hub template at all (all 5 sibling reports under hub 945 are
-- tenant-only rows too), and the only 'Production Reports' row there is the
-- legacy vowerp2 hub 410 ('reports_production', module 1) -- anchoring on it
-- would insert a mis-parented template row. Add a template only once a
-- 'juteProduction/report' hub row exists in portal_menu_mst.
--
-- DEFENSIVE-BY-CONSTRUCTION: nothing hardcodes a menu_id, module id, or
-- role_id -- every id resolves at run time from the existing
-- 'Production Reports' hub row (anchored by menu_path, falling back to name),
-- so the same script promotes to other tenants (e.g. sls) unmodified. If the
-- hub anchor does not resolve on a target DB the derived table is empty and
-- the INSERT is a silent no-op -- always run the VERIFICATION queries.
--
-- Idempotent via NOT EXISTS guards: menu_mst insert guarded on menu_name
-- (also UNIQUE on dev3/sls, extra safety) and role_menu_map guarded on
-- (role_id, menu_id) -- that table has NO unique key, the guard is the only
-- protection, so never bypass it.
--
-- RUNNER NOTE: the pymysql runner splits this file on the literal semicolon
-- and skips chunks whose trimmed text starts with '--'. Every comment block is
-- therefore terminated by a lone semicolon so the following real statement
-- starts a clean chunk. Comment prose never contains a mid-line semicolon.
--
-- POST-APPLY: Portal sidebars cache their menu tree in localStorage
-- (SidebarContext) -- users MUST log out/re-login before the report appears
-- in the hub dropdown.
--
-- Run this AFTER the backend endpoint
-- (/api/juteProductionReports/daily_production_report) and the frontend page
-- are deployed, so the route resolves by the time anyone clicks it.
;

-- =============================================================================
-- TENANT DB (dev3 / sls): menu_mst + role_menu_map
-- =============================================================================
-- report = 1 puts the row in the reports-hub dropdown
-- (fetchReportMenuTree('juteProduction/report') filters report = 1 children of
-- the hub). menu_path derives via CONCAT off the hub's own path so this also
-- works on tenants that store absolute '/dashboardportal/...' paths.
;

-- B1. menu_mst -- Daily Production Report (report page under the hub)
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Daily Production Report', CONCAT(p.menu_path, '/dailyProductionReport'), p.menu_id, 'assessment', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Daily Production Report');

-- B2. role_menu_map -- mirror each role's existing access level on the hub
-- row (or any current report child of the hub, taking the MAX level held) so
-- exactly the audience that can open Production Reports today can open the
-- DPR. One row per (role_id, menu_id) -- access_type is a cumulative level.
-- updated_by = 1 (system/admin seed user).
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN (
        SELECT menu_id
        FROM menu_mst
        WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
        ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
        LIMIT 1
    ) hub ON existing.menu_id = hub.menu_id OR existing.menu_parent_id = hub.menu_id
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Daily Production Report') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION -- run manually against each target DB after applying
-- =============================================================================
--
--   -- dev3
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
--     FROM menu_mst WHERE menu_name = 'Daily Production Report'
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name = 'Daily Production Report' ORDER BY r.role_id
--
-- Then re-login to dev3 and confirm the report appears in the
-- Jute Production > Production Reports dropdown and the page loads at
-- /dashboardportal/juteProduction/report/dailyProductionReport.
-- =============================================================================

-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- dev3:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name = 'Daily Production Report'
--   DELETE FROM menu_mst WHERE menu_name = 'Daily Production Report'
-- =============================================================================
