-- =============================================================================
-- Migration: Jute Mechanical & Maintenance -- portal menu wiring (TENANT DBs)
-- =============================================================================
-- Date: 2026-08-24
-- Spec: docs/jute-mechanical-maintenance-spec.md (sections "Frontend" + "Menu")
--
-- -----------------------------------------------------------------------------
-- README -- HOW TO RUN THIS PAIR OF FILES
-- -----------------------------------------------------------------------------
--   1. THIS FILE  (seed_jute_mech_menus_tenant.sql)
--        -> run against the TENANT databases: dev3 FIRST, then sls.
--           Creates module_mst + menu_mst + role_menu_map rows -- the actual
--           Portal sidebar rows those tenants' users see.
--
--   2. seed_jute_mech_menus_console.sql
--        -> run ONCE against vowconsole3.
--           Creates the portal_menu_mst master-template rows only, which is
--           what a brand new tenant is provisioned from. vowconsole3 has NO
--           role_menu_map of its own, so that file never touches grants.
--
--   3. POST-APPLY: every portal user (QA included) MUST LOG OUT AND BACK IN.
--      The sidebar caches the whole menu tree in localStorage (SidebarContext),
--      so nothing new appears until the cached tree is refetched.
--
-- Run this AFTER create_jute_mech_maintenance_tables.sql and after the
-- backend routers + Next.js routes under
-- src/app/dashboardportal/juteMechanical/ are deployed to the target tenant,
-- so every menu_path resolves by the time somebody clicks it.
--
-- PREREQUISITE: menu_mst must already carry the `report` column
-- (add_report_to_menu_mst.sql -- a no-op on dev3, which has had it since
-- 2026-06-25). Without it every INSERT below fails with
-- "Unknown column 'report' in field list". Check first with:
--     SHOW COLUMNS FROM menu_mst LIKE 'report'
-- -----------------------------------------------------------------------------
--
-- DEFENSIVE BY CONSTRUCTION: nothing below hardcodes a menu_id, a
-- module_mst_id or a role_id. Every id is resolved at run time by name, so
-- this same file promotes to sls (or any future tenant) unmodified. If an
-- anchor fails to resolve on a given target the derived table is empty and the
-- INSERT is a silent no-op -- ALWAYS run the VERIFICATION queries at the end.
--
-- IDEMPOTENT: every INSERT is guarded by NOT EXISTS. menu_name and
-- module_name are both UNIQUE, so the guard is a plain name check.
-- role_menu_map is guarded on (role_id, menu_id). Safe to re-run -- a
-- partially applied prior run just fills in the remainder.
--
-- MENU DEPTH RULE (why the tree is shaped the way it is): only TWO levels
-- reach the Portal sidebar. src/common/portal/menu.py drops any row whose
-- parent itself has a parent (_is_sub_sub_menu). So:
--     level 1  module root      'Jute Mechanical & Maintenance'  (parent NULL)
--     level 2  pages            visible in the sidebar
--     level 3  reports          NOT in the sidebar -- surfaced only through
--                               GET /report_menu_tree, which returns
--                               descendants flagged report = 1
-- The four level-3 rows under 'Maintenance Masters' are ordinary pages, not
-- reports (report = 0), so they will NOT appear in the sidebar either -- that
-- is intentional: the level-2 'Maintenance Masters' page is a hub that links
-- to them. They still get role_menu_map rows because the per-route portal
-- permission check in the frontend middleware needs a grant for the route.
--
-- RUNNER NOTE: the pymysql runner (CLAUDE.md 'Running migrations' /
-- run-migration skill) splits this file on the literal semicolon character
-- and skips any chunk whose trimmed text starts with '--'. Every comment
-- block below is therefore terminated by a lone semicolon on its own line so
-- it forms its own skipped chunk, and no comment prose anywhere in this file
-- contains a mid-line semicolon. There are no DELIMITER blocks.
;

-- -----------------------------------------------------------------------------
-- COLLISION PRE-CHECK -- run this BEFORE applying, on each target tenant.
-- menu_name is UNIQUE, so if any of these names is already taken by an
-- unrelated menu the guarded INSERT below turns into a silent no-op and that
-- page simply never appears. Expect ZERO rows back.
-- -----------------------------------------------------------------------------
--   SELECT menu_id, menu_name, menu_path FROM menu_mst
--    WHERE menu_name IN ('Jute Mechanical & Maintenance','Breakdown Entry',
--          'Overhaul Register','Preventive Maintenance','Machine History',
--          'Maintenance Masters','Maintenance Reports','Breakdown Reason Master',
--          'Component Master','Machine Attributes','PM Schedule Master',
--          'Breakdown Register','Machine-wise Summary','Reason Pareto',
--          'Date x Machine Grid','Department Summary','Reliability (MTBF/MTTR)',
--          'Reason Trend','Maintenance Due','Spares Consumption',
--          'Top Problem Machines','Machine History Card','Open Breakdown Board')
--
-- NOTE: this module's report hub is named 'Maintenance Reports', not 'Reports'.
-- menu_name is UNIQUE table-wide and Accounting already owns 'Reports', so the
-- plain name would have made the hub INSERT a silent no-op. menu_path is
-- unchanged at juteMechanical/reports -- the frontend anchors by path.
-- -----------------------------------------------------------------------------
;

-- =============================================================================
-- STEP 1 -- module_mst : the new application module
-- =============================================================================
-- module_mst columns: module_mst_id, module_name (UNIQUE), module_type, active.
-- module_type is inherited from the existing 'Jute Production' module so the
-- new module is classified exactly like its sibling. NOTE: the sidebar query
-- (get_portal_user_menus) LEFT JOINs control_co_module on
-- ccm.module_id = mm.module_mst_id -- a brand new module has no
-- control_co_module row, IFNULL(ccm.access_type, 0) = 0, so it is visible by
-- default. If a company should NOT see this module, the tenant admin adds a
-- control_co_module row with access_type = 2 afterwards.
;

INSERT INTO module_mst (module_name, module_type, active)
SELECT 'Jute Mechanical & Maintenance',
       (SELECT jp.module_type FROM module_mst jp WHERE jp.module_name = 'Jute Production' ORDER BY jp.module_mst_id LIMIT 1),
       1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM module_mst WHERE module_name = 'Jute Mechanical & Maintenance');

-- =============================================================================
-- STEP 2 -- menu_mst : the module root (level 1, menu_parent_id NULL)
-- =============================================================================
-- menu_path is RELATIVE on purpose. The sidebar renders
-- href={`/dashboardportal/${menu.menu_path}`}, so a leading '/dashboardportal'
-- here would double up into /dashboardportal/dashboardportal/juteMechanical.
-- Every descendant below CONCATs off this row's own path, so the whole subtree
-- stays consistent with whatever the root ends up holding.
-- order_by 200 places the module after Jute Production (100 block) and its
-- children. menu_type_id NULL matches every other tenant menu_mst row.
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Mechanical & Maintenance', 'juteMechanical', 1, NULL, NULL, 'precision_manufacturing',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Mechanical & Maintenance' ORDER BY module_mst_id LIMIT 1),
       200, 0
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance');

-- =============================================================================
-- STEP 3 -- menu_mst : level-2 pages (these are what the sidebar shows)
-- =============================================================================
-- Each row is parented to the module root by subquery and inherits the root's
-- module_mst_id. menu_path must match the Next.js route folder exactly:
--   src/app/dashboardportal/juteMechanical/<folder>/page.tsx
;

-- 3.1 Breakdown Entry -> juteMechanical/breakdown
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Breakdown Entry', CONCAT(p.menu_path, '/breakdown'), 1, p.menu_id, NULL, 'report_problem',
       p.module_mst_id, 210, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Breakdown Entry');

-- 3.2 Overhaul Register -> juteMechanical/overhaul
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Overhaul Register', CONCAT(p.menu_path, '/overhaul'), 1, p.menu_id, NULL, 'build',
       p.module_mst_id, 220, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Overhaul Register');

-- 3.3 Preventive Maintenance -> juteMechanical/preventiveMaintenance
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Preventive Maintenance', CONCAT(p.menu_path, '/preventiveMaintenance'), 1, p.menu_id, NULL, 'schedule',
       p.module_mst_id, 230, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Preventive Maintenance');

-- 3.4 Machine History -> juteMechanical/machineHistory
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Machine History', CONCAT(p.menu_path, '/machineHistory'), 1, p.menu_id, NULL, 'history',
       p.module_mst_id, 240, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Machine History');

-- 3.5 Maintenance Masters -> juteMechanical/masters  (hub page, see STEP 4)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Maintenance Masters', CONCAT(p.menu_path, '/masters'), 1, p.menu_id, NULL, 'settings',
       p.module_mst_id, 250, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Maintenance Masters');

-- 3.6 Maintenance Reports -> juteMechanical/reports  (report hub, see STEP 5)
-- Named 'Maintenance Reports' because menu_name is UNIQUE table-wide and
-- Accounting already owns 'Reports'. The PATH stays juteMechanical/reports.
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Maintenance Reports', CONCAT(p.menu_path, '/reports'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 260, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Maintenance Reports');

-- =============================================================================
-- STEP 4 -- menu_mst : level-3 MASTER PAGES under 'Maintenance Masters'
-- =============================================================================
-- !! THESE WILL NOT APPEAR IN THE SIDEBAR !!
-- They sit at level 3 and carry report = 0, so the sidebar filter
-- (_is_sub_sub_menu in src/common/portal/menu.py) drops them and
-- /report_menu_tree ignores them too (it only returns report = 1 rows).
-- That is deliberate: the level-2 'Maintenance Masters' page is a HUB that
-- links to these four routes. The rows exist so each route has a menu_id for
-- useMenuId and, via STEP 6, a role_menu_map grant for the per-route portal
-- permission check. If any of these ever needs to be reachable directly from
-- the sidebar it must be promoted to a level-2 sibling instead.
;

-- 4.1 Breakdown Reason Master -> juteMechanical/masters/reasonMaster
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Breakdown Reason Master', CONCAT(p.menu_path, '/reasonMaster'), 1, p.menu_id, NULL, 'rule',
       p.module_mst_id, 251, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Breakdown Reason Master');

-- 4.2 Component Master -> juteMechanical/masters/componentMaster
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Component Master', CONCAT(p.menu_path, '/componentMaster'), 1, p.menu_id, NULL, 'settings_input_component',
       p.module_mst_id, 252, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Component Master');

-- 4.3 Machine Attributes -> juteMechanical/masters/machineAttribute
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Machine Attributes', CONCAT(p.menu_path, '/machineAttribute'), 1, p.menu_id, NULL, 'tune',
       p.module_mst_id, 253, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Machine Attributes');

-- 4.4 PM Schedule Master -> juteMechanical/masters/pmSchedule
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'PM Schedule Master', CONCAT(p.menu_path, '/pmSchedule'), 1, p.menu_id, NULL, 'checklist',
       p.module_mst_id, 254, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'PM Schedule Master');

-- =============================================================================
-- STEP 5 -- menu_mst : level-3 REPORT rows under 'Maintenance Reports'
-- =============================================================================
-- report = 1 is what puts a row in the hub dropdown: the frontend calls
-- fetchReportMenuTree('juteMechanical/reports') -> GET /report_menu_tree,
-- whose recursive CTE walks active descendants of the anchor path and keeps
-- only report = 1 rows the caller's roles grant. menu_icon 'assessment'
-- matches every other report row in the codebase
-- (add_inventory_report_menus.sql, seed_jute_reports_menu.sql).
-- Paths must match src/app/dashboardportal/juteMechanical/reports/<folder>/.
;

-- 5.01 Breakdown Register
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Breakdown Register', CONCAT(p.menu_path, '/breakdownRegister'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 261, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Breakdown Register');

-- 5.02 Machine-wise Summary
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Machine-wise Summary', CONCAT(p.menu_path, '/machineSummary'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 262, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Machine-wise Summary');

-- 5.03 Reason Pareto
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Reason Pareto', CONCAT(p.menu_path, '/reasonPareto'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 263, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Reason Pareto');

-- 5.04 Date x Machine Grid
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Date x Machine Grid', CONCAT(p.menu_path, '/dateMachineGrid'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 264, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Date x Machine Grid');

-- 5.05 Department Summary
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Department Summary', CONCAT(p.menu_path, '/departmentSummary'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 265, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Department Summary');

-- 5.06 Reliability (MTBF/MTTR)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Reliability (MTBF/MTTR)', CONCAT(p.menu_path, '/reliability'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 266, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Reliability (MTBF/MTTR)');

-- 5.07 Reason Trend
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Reason Trend', CONCAT(p.menu_path, '/reasonTrend'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 267, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Reason Trend');

-- 5.08 Maintenance Due
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Maintenance Due', CONCAT(p.menu_path, '/maintenanceDue'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 268, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Maintenance Due');

-- 5.09 Spares Consumption
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Spares Consumption', CONCAT(p.menu_path, '/sparesConsumption'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 269, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Spares Consumption');

-- 5.10 Top Problem Machines
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Top Problem Machines', CONCAT(p.menu_path, '/topProblemMachines'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 270, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Top Problem Machines');

-- 5.11 Machine History Card
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Machine History Card', CONCAT(p.menu_path, '/machineHistoryCard'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 271, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Machine History Card');

-- 5.12 Open Breakdown Board
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Open Breakdown Board', CONCAT(p.menu_path, '/openBoard'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 272, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Open Breakdown Board');

-- =============================================================================
-- STEP 6 -- role_menu_map : mirror the existing Jute Production audience
-- =============================================================================
-- WHO: every role that already holds a grant on an existing Jute Production
-- page -- the 'Jute Production' parent row itself or any of its direct
-- children (Spreader Production, Winding Production, Spinning Production, ...).
-- Same anchoring approach as 20260819_winding_stock_menus.sql, joined off a
-- known existing menu_name so no role_id is hardcoded.
--
-- WHAT LEVEL: access_type_id is a CUMULATIVE LEVEL, not a set of independent
-- flags (1 read, 2 print, 3 write, 4 edit), and role_menu_map must carry
-- exactly ONE row per (role_id, menu_id) -- the tenant-admin role-menu UI
-- (expandableMenuDropdown.tsx) binds a single <select> per menu and the
-- menu-tree query joins role_menu_map without DISTINCT, so multiple rows
-- render duplicate React keys. Each role therefore gets ONE row carrying the
-- MAX level it already holds anywhere in the Jute Production tree.
--
-- WHICH MENUS: the whole new subtree in one statement -- the module root, its
-- level-2 children, and every level-3 grandchild (the four master pages and
-- the twelve reports). Level-3 rows need grants of their own: the report tree
-- endpoint enforces role_menu_map per report, and the frontend per-route
-- permission check needs a grant for each master route.
--
-- updated_by = 1 (system/admin seed user, the convention used by every other
-- menu seed in this directory). Idempotent on (role_id, menu_id).
;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, t.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN menu_mst jp ON jp.menu_name = 'Jute Production'
    WHERE existing.menu_id = jp.menu_id OR existing.menu_parent_id = jp.menu_id
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT m.menu_id
    FROM menu_mst m
    JOIN menu_mst root ON root.menu_name = 'Jute Mechanical & Maintenance'
    WHERE m.menu_id = root.menu_id
       OR m.menu_parent_id = root.menu_id
       OR m.menu_parent_id IN (SELECT c.menu_id FROM menu_mst c WHERE c.menu_parent_id = root.menu_id)
) t
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = t.menu_id
);

-- =============================================================================
-- VERIFICATION -- run manually against each target tenant after applying
-- =============================================================================
--
--   -- 1. the module row
--   SELECT module_mst_id, module_name, module_type, active
--     FROM module_mst WHERE module_name = 'Jute Mechanical & Maintenance'
--
--   -- 2. the whole tree, in display order. EXPECT 23 ROWS:
--   --    1 root + 6 level-2 pages + 4 level-3 masters + 12 level-3 reports.
--   --    Every menu_path must start 'juteMechanical' with NO leading slash.
--   SELECT m.menu_id, m.menu_name, m.menu_path, m.menu_parent_id,
--          p.menu_name AS parent_name, m.report, m.order_by, m.module_mst_id, m.active
--     FROM menu_mst m
--     LEFT JOIN menu_mst p ON p.menu_id = m.menu_parent_id
--    WHERE m.menu_path = 'juteMechanical' OR m.menu_path LIKE 'juteMechanical/%'
--    ORDER BY m.order_by
--
--   -- 3. depth sanity: exactly 16 rows must be level 3 (4 masters + 12 reports)
--   SELECT COUNT(*) FROM menu_mst m
--     JOIN menu_mst p ON p.menu_id = m.menu_parent_id
--    WHERE p.menu_parent_id IS NOT NULL AND m.menu_path LIKE 'juteMechanical/%'
--
--   -- 4. report flags: exactly 12 rows, all under 'Maintenance Reports'
--   SELECT menu_name, menu_path, order_by FROM menu_mst
--    WHERE menu_path LIKE 'juteMechanical/reports/%' AND report = 1
--    ORDER BY order_by
--
--   -- 5. grants: one row per (role, menu), 23 menus per granted role
--   SELECT r.role_id, COUNT(*) AS menus_granted, MIN(r.access_type_id) AS min_level,
--          MAX(r.access_type_id) AS max_level
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path = 'juteMechanical' OR m.menu_path LIKE 'juteMechanical/%'
--    GROUP BY r.role_id ORDER BY r.role_id
--
--   -- 6. duplicate guard: must return ZERO rows
--   SELECT r.role_id, r.menu_id, COUNT(*) AS c
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path LIKE 'juteMechanical%'
--    GROUP BY r.role_id, r.menu_id HAVING c > 1
--
-- Then log in to the tenant as a user holding one of the granted roles
-- (re-login first -- see the README POST-APPLY note at the top) and confirm:
--   * 'Jute Mechanical & Maintenance' appears as a top-level sidebar module
--   * it expands to exactly SIX entries -- Breakdown Entry, Overhaul Register,
--     Preventive Maintenance, Machine History, Maintenance Masters,
--     Maintenance Reports
--   * the four master pages are NOT in the sidebar and are reached from the
--     Maintenance Masters hub page
--   * the Maintenance Reports hub lists all twelve reports via the report-menu
--     tree
--   * a user WITHOUT a granted role sees none of it
-- =============================================================================

-- =============================================================================
-- ROLLBACK -- run against each tenant this was applied to
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm
--     JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_path = 'juteMechanical' OR m.menu_path LIKE 'juteMechanical/%'
--
--   -- children before parents, or the self-referencing menu_parent_id
--   -- leaves orphans behind
--   DELETE FROM menu_mst WHERE menu_path LIKE 'juteMechanical/reports/%'
--   DELETE FROM menu_mst WHERE menu_path LIKE 'juteMechanical/masters/%'
--   DELETE FROM menu_mst WHERE menu_path LIKE 'juteMechanical/%'
--   DELETE FROM menu_mst WHERE menu_path = 'juteMechanical'
--
--   -- only safe once no menu_mst row references it any more
--   DELETE FROM module_mst WHERE module_name = 'Jute Mechanical & Maintenance'
-- =============================================================================
