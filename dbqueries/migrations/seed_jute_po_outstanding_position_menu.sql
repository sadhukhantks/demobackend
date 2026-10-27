-- =============================================================================
-- Migration: 'Outstanding PO Position' report menu (Jute Purchase reports hub)
-- =============================================================================
-- Date: 2026-09-25
-- TARGET: tenant DB only (dev3 and sls done, other tenants once BE+FE are
-- deployed there). vowconsole3.portal_menu_mst deliberately untouched, as for
-- the other jute report menus.
--
-- Adds 1 row:
--   'Outstanding PO Position' (report) -> jutePurchase/reports/poOutstandingPosition
--       report = 1, order_by = MAX(sibling)+1, under the 'Jute Report' hub
--       (menu_path 'jutePurchase/reports'), role_menu_map mirrored from that hub
--       (MAX level held per role on the hub or any of its children).
-- Nothing hardcodes an id.
--
-- Runner note: the pymysql runner splits on semicolons and skips chunks starting
-- with a comment marker, so each comment block is terminated by a lone semicolon.
;

-- 1. Report row under the Jute Report hub
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Outstanding PO Position', CONCAT(p.menu_path, '/poOutstandingPosition'), p.menu_id, 'pending_actions', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition');

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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
FROM menu_mst
WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition';

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN
--   (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition');
-- DELETE FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition';
-- =============================================================================
;
