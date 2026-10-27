-- =============================================================================
-- Migration: Jute Mechanical & Maintenance -- portal menu TEMPLATE (vowconsole3)
-- =============================================================================
-- Date: 2026-08-24
-- Spec: docs/jute-mechanical-maintenance-spec.md (sections "Frontend" + "Menu")
--
-- TARGET: vowconsole3 ONLY -- run this file exactly once, against vowconsole3.
--
-- This is the MASTER TEMPLATE half of the pair. It seeds portal_menu_mst, the
-- control-desk-managed template a brand new tenant is provisioned from. It
-- does NOT put anything in front of a user by itself.
--
-- The other half -- seed_jute_mech_menus_tenant.sql -- creates the rows the
-- existing tenants actually render (module_mst + menu_mst + role_menu_map) and
-- must be run separately against dev3 AND sls. Read that file's README header
-- for the full run order.
--
-- vowconsole3 has NO role_menu_map of its own (grants live per tenant DB), so
-- nothing here touches role grants.
--
-- COLUMN DIFFERENCES vs the tenant menu_mst -- do not copy one file over the
-- other:
--   * the FK column is `module_id`, NOT `module_mst_id`
--   * there is NO `report` column here. The report flag exists only in the
--     tenant menu_mst, which is what GET /report_menu_tree reads. A tenant
--     provisioned from this template therefore needs the twelve report rows
--     re-flagged report = 1 -- easiest is to just run
--     seed_jute_mech_menus_tenant.sql against the new tenant instead.
--   * menu_type_id is NOT NULL here (it is nullable in the tenant table), so
--     it is inherited from an existing top-level template row rather than set
--     to NULL.
--   * module_id is NOT NULL and FKs to con_module_masters, so it too is
--     inherited from an existing top-level template row -- this file
--     deliberately does NOT invent a con_module_masters row.
--
-- DEFENSIVE BY CONSTRUCTION: no menu_id, module_id or menu_type_id is
-- hardcoded. The root row derives menu_type_id and module_id from an existing
-- top-level template row, preferring 'Jute Production' and falling back to
-- whichever top-level row sorts first. Every descendant then inherits from its
-- own parent, so the subtree is internally consistent. If the anchor fails to
-- resolve the derived table is empty and the INSERT is a silent no-op --
-- ALWAYS run the VERIFICATION queries at the end.
--
-- IDEMPOTENT: every INSERT is guarded by NOT EXISTS on menu_name, which is
-- UNIQUE. Safe to re-run.
--
-- MENU DEPTH: identical shape to the tenant file -- level 1 module root,
-- level 2 pages, level 3 master pages and reports. Only two levels reach a
-- portal sidebar.
--
-- RUNNER NOTE: the pymysql runner splits this file on the literal semicolon
-- character and skips any chunk whose trimmed text starts with '--'. Every
-- comment block is terminated by a lone semicolon on its own line, and no
-- comment prose contains a mid-line semicolon. There are no DELIMITER blocks.
;

-- -----------------------------------------------------------------------------
-- COLLISION PRE-CHECK -- run BEFORE applying. menu_name is UNIQUE, so a name
-- already taken by an unrelated template row silently turns its INSERT into a
-- no-op. Expect ZERO rows back.
-- -----------------------------------------------------------------------------
--   SELECT menu_id, menu_name, menu_path FROM portal_menu_mst
--    WHERE menu_name IN ('Jute Mechanical & Maintenance','Breakdown Entry',
--          'Overhaul Register','Preventive Maintenance','Machine History',
--          'Maintenance Masters','Maintenance Reports','Breakdown Reason Master',
--          'Component Master','Machine Attributes','PM Schedule Master',
--          'Breakdown Register','Machine-wise Summary','Reason Pareto',
--          'Date x Machine Grid','Department Summary','Reliability (MTBF/MTTR)',
--          'Reason Trend','Maintenance Due','Spares Consumption',
--          'Top Problem Machines','Machine History Card','Open Breakdown Board')
--
-- The rename is ALREADY APPLIED: this module's report hub is named
-- 'Maintenance Reports', not 'Reports', because menu_name is UNIQUE and
-- vowconsole3 already holds a top-level template row called 'Reports'. Only
-- the NAME changed -- menu_path stays juteMechanical/reports, which is what
-- the frontend fetchReportMenuTree call anchors on.
-- -----------------------------------------------------------------------------
;

-- =============================================================================
-- STEP 1 -- portal_menu_mst : the module root (level 1, menu_parent_id 0)
-- =============================================================================
-- menu_path is RELATIVE, matching the tenant rows -- the sidebar renders
-- href={`/dashboardportal/${menu.menu_path}`}, so a leading '/dashboardportal'
-- would double up.
--
-- PARENT CONVENTION: in vowconsole3 top-level template rows use
-- menu_parent_id = 0, never NULL (zero rows in portal_menu_mst carry a NULL
-- parent). This differs from the tenant menu_mst, where the root is NULL. The
-- anchor subquery accepts either so it still resolves if that ever changes.
;

INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Jute Mechanical & Maintenance', 'juteMechanical', 1, 0, a.menu_type_id, 'precision_manufacturing',
       a.module_id, 200
FROM (
    SELECT menu_type_id, module_id
    FROM portal_menu_mst
    WHERE (menu_parent_id = 0 OR menu_parent_id IS NULL) AND active = 1
    ORDER BY CASE WHEN menu_name = 'Jute Production' THEN 0 ELSE 1 END, menu_id
    LIMIT 1
) a
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance');

-- =============================================================================
-- STEP 2 -- portal_menu_mst : level-2 pages
-- =============================================================================
-- Parent id, menu_type_id and module_id all inherited from the root row.
-- menu_path CONCATs off the parent's own path.
;

-- 2.1 Breakdown Entry -> juteMechanical/breakdown
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Breakdown Entry', CONCAT(p.menu_path, '/breakdown'), 1, p.menu_id, p.menu_type_id, 'report_problem',
       p.module_id, 210
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Breakdown Entry');

-- 2.2 Overhaul Register -> juteMechanical/overhaul
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Overhaul Register', CONCAT(p.menu_path, '/overhaul'), 1, p.menu_id, p.menu_type_id, 'build',
       p.module_id, 220
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Overhaul Register');

-- 2.3 Preventive Maintenance -> juteMechanical/preventiveMaintenance
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Preventive Maintenance', CONCAT(p.menu_path, '/preventiveMaintenance'), 1, p.menu_id, p.menu_type_id, 'schedule',
       p.module_id, 230
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Preventive Maintenance');

-- 2.4 Machine History -> juteMechanical/machineHistory
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Machine History', CONCAT(p.menu_path, '/machineHistory'), 1, p.menu_id, p.menu_type_id, 'history',
       p.module_id, 240
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Machine History');

-- 2.5 Maintenance Masters -> juteMechanical/masters
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Maintenance Masters', CONCAT(p.menu_path, '/masters'), 1, p.menu_id, p.menu_type_id, 'settings',
       p.module_id, 250
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Maintenance Masters');

-- 2.6 Maintenance Reports -> juteMechanical/reports
-- Named 'Maintenance Reports' because menu_name is UNIQUE and 'Reports' is
-- already taken. The PATH stays juteMechanical/reports.
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Maintenance Reports', CONCAT(p.menu_path, '/reports'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 260
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports');

-- =============================================================================
-- STEP 3 -- portal_menu_mst : level-3 MASTER PAGES under 'Maintenance Masters'
-- =============================================================================
-- Level-3 rows never reach a portal sidebar. These four exist so a newly
-- provisioned tenant inherits the route rows -- the level-2 'Maintenance
-- Masters' page is a hub that links to them.
;

-- 3.1 Breakdown Reason Master -> juteMechanical/masters/reasonMaster
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Breakdown Reason Master', CONCAT(p.menu_path, '/reasonMaster'), 1, p.menu_id, p.menu_type_id, 'rule',
       p.module_id, 251
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Breakdown Reason Master');

-- 3.2 Component Master -> juteMechanical/masters/componentMaster
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Component Master', CONCAT(p.menu_path, '/componentMaster'), 1, p.menu_id, p.menu_type_id, 'settings_input_component',
       p.module_id, 252
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Component Master');

-- 3.3 Machine Attributes -> juteMechanical/masters/machineAttribute
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Machine Attributes', CONCAT(p.menu_path, '/machineAttribute'), 1, p.menu_id, p.menu_type_id, 'tune',
       p.module_id, 253
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Machine Attributes');

-- 3.4 PM Schedule Master -> juteMechanical/masters/pmSchedule
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'PM Schedule Master', CONCAT(p.menu_path, '/pmSchedule'), 1, p.menu_id, p.menu_type_id, 'checklist',
       p.module_id, 254
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Masters' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'PM Schedule Master');

-- =============================================================================
-- STEP 4 -- portal_menu_mst : level-3 REPORT rows under 'Maintenance Reports'
-- =============================================================================
-- REMINDER: portal_menu_mst has no `report` column. These rows carry the names,
-- paths and ordering only. The report = 1 flag that makes them show up in the
-- reports hub lives in the tenant menu_mst and is set by
-- seed_jute_mech_menus_tenant.sql.
;

-- 4.01 Breakdown Register
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Breakdown Register', CONCAT(p.menu_path, '/breakdownRegister'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 261
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Breakdown Register');

-- 4.02 Machine-wise Summary
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Machine-wise Summary', CONCAT(p.menu_path, '/machineSummary'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 262
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Machine-wise Summary');

-- 4.03 Reason Pareto
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Reason Pareto', CONCAT(p.menu_path, '/reasonPareto'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 263
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Reason Pareto');

-- 4.04 Date x Machine Grid
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Date x Machine Grid', CONCAT(p.menu_path, '/dateMachineGrid'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 264
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Date x Machine Grid');

-- 4.05 Department Summary
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Department Summary', CONCAT(p.menu_path, '/departmentSummary'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 265
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Department Summary');

-- 4.06 Reliability (MTBF/MTTR)
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Reliability (MTBF/MTTR)', CONCAT(p.menu_path, '/reliability'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 266
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Reliability (MTBF/MTTR)');

-- 4.07 Reason Trend
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Reason Trend', CONCAT(p.menu_path, '/reasonTrend'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 267
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Reason Trend');

-- 4.08 Maintenance Due
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Maintenance Due', CONCAT(p.menu_path, '/maintenanceDue'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 268
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Maintenance Due');

-- 4.09 Spares Consumption
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Spares Consumption', CONCAT(p.menu_path, '/sparesConsumption'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 269
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Spares Consumption');

-- 4.10 Top Problem Machines
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Top Problem Machines', CONCAT(p.menu_path, '/topProblemMachines'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 270
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Top Problem Machines');

-- 4.11 Machine History Card
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Machine History Card', CONCAT(p.menu_path, '/machineHistoryCard'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 271
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Machine History Card');

-- 4.12 Open Breakdown Board
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Open Breakdown Board', CONCAT(p.menu_path, '/openBoard'), 1, p.menu_id, p.menu_type_id, 'assessment',
       p.module_id, 272
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Open Breakdown Board');

-- =============================================================================
-- VERIFICATION -- run manually against vowconsole3 after applying
-- =============================================================================
--
--   -- the whole template tree, in display order. EXPECT 23 ROWS:
--   -- 1 root + 6 level-2 pages + 4 level-3 masters + 12 level-3 reports.
--   -- Every menu_path must start 'juteMechanical' with NO leading slash, and
--   -- module_id / menu_type_id must be non-NULL on every row.
--   SELECT m.menu_id, m.menu_name, m.menu_path, m.menu_parent_id,
--          p.menu_name AS parent_name, m.menu_type_id, m.module_id,
--          m.order_by, m.active
--     FROM portal_menu_mst m
--     LEFT JOIN portal_menu_mst p ON p.menu_id = m.menu_parent_id
--    WHERE m.menu_path = 'juteMechanical' OR m.menu_path LIKE 'juteMechanical/%'
--    ORDER BY m.order_by
--
--   -- the root must be top level and must have inherited a real module_id
--   SELECT menu_id, menu_name, menu_parent_id, menu_type_id, module_id
--     FROM portal_menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance'
-- =============================================================================

-- =============================================================================
-- ROLLBACK -- vowconsole3
-- =============================================================================
--   -- children before parents, or the self-referencing menu_parent_id
--   -- leaves orphans behind
--   DELETE FROM portal_menu_mst WHERE menu_path LIKE 'juteMechanical/reports/%'
--   DELETE FROM portal_menu_mst WHERE menu_path LIKE 'juteMechanical/masters/%'
--   DELETE FROM portal_menu_mst WHERE menu_path LIKE 'juteMechanical/%'
--   DELETE FROM portal_menu_mst WHERE menu_path = 'juteMechanical'
-- =============================================================================
