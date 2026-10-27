-- =============================================================================
-- Migration: 'Jute Issue Yarn Quality Wise' report menu (Jute Purchase reports hub)
-- =============================================================================
-- Date: 2026-10-05
-- Ticket: TKT-000214 (sls) -- "total jute issue in kg or MT for a particular date
-- and the month-to-date figure, against the yarn qualities".
--
-- STATUS: applied on dev3 (menu_id 1045, 5 role grants) and sls (menu_id 1073,
-- 30 role grants) on 2026-10-05 with the owner's OK. Other tenants: not run.
-- Data-only: one menu_mst row + its role_menu_map rows. No DDL.
--
-- TARGET: tenant DB only, and only once the BE + FE that serve
-- jutePurchase/reports/issueYarnWise are deployed for that tenant.
-- vowconsole3.portal_menu_mst deliberately untouched, as for the other jute
-- report menus.
--
-- Until this is run the page still opens by its URL for anyone who can open the
-- Jute Report hub (the route guard falls back to the hub's grant). It is only
-- missing from the hub's Report dropdown.
--
-- Adds 1 row:
--   'Jute Issue Yarn Quality Wise' (report) -> jutePurchase/reports/issueYarnWise
--       report = 1, order_by = MAX(sibling)+1, under the 'Jute Report' hub
--       (menu_path 'jutePurchase/reports'), role_menu_map mirrored from that hub
--       (MAX level held per role on the hub or any of its children) -- i.e. every
--       role that can already open a jute report sees this one. The report shows
--       weights only (no rates / values). To restrict it to chosen roles instead,
--       replace the `r` subquery in step 2 with an explicit role list (pattern:
--       seed_jute_mr_issue_detail_menu_sls.sql).
-- Nothing hardcodes an id. Re-runnable (both inserts are guarded by NOT EXISTS;
-- menu_name is UNIQUE).
--
-- Runner note: the pymysql runner splits on semicolons and skips chunks starting
-- with a comment marker, so each comment block is terminated by a lone semicolon.
;

-- 1. Report row under the Jute Report hub
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Jute Issue Yarn Quality Wise', CONCAT(p.menu_path, '/issueYarnWise'), p.menu_id, 'assessment', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issueYarnWise');

-- 2. role_menu_map -- mirror each role's access on the Jute Report hub (or any
--    hub child), MAX level held.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports' LIMIT 1) hub
      ON existing.menu_id = hub.menu_id OR existing.menu_parent_id = hub.menu_id
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issueYarnWise') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
FROM menu_mst
WHERE menu_path = 'jutePurchase/reports/issueYarnWise';

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN
--   (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issueYarnWise');
-- DELETE FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issueYarnWise';
-- =============================================================================
;
