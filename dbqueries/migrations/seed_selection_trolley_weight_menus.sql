-- =============================================================================
-- Migration: Selection Trolley Weight menus
--            (../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md §2)
-- =============================================================================
-- Date: 2026-09-22
-- TARGET: tenant DB only (dev3 first, other tenants once BE+FE are deployed there).
-- vowconsole3.portal_menu_mst deliberately untouched -- the juteProduction hub
-- has no template row there; every sibling under the hub is tenant-only
-- (see seed_bale_production_menus.sql).
--
-- Adds 2 rows:
--   1. 'Selection Trolley Weight' (txn)    -> juteProduction/selectionTrolleyWeight
--                                             order_by 187 (right after 'Bale Production' 186),
--                                             role_menu_map mirrored from 'Bale Production'
--   2. 'Selection vs Jute Issue'  (report) -> juteProduction/report/selectionVsIssue
--                                             report = 1, order_by = MAX(sibling)+1,
--                                             role_menu_map mirrored from the Production
--                                             Reports hub (as the bale reports seed does)
-- Nothing hardcodes an id. Run AFTER create_jute_prod_selection_trolly_wt.sql.
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

-- 1. Selection Trolley Weight (transaction page, under Jute Production)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Selection Trolley Weight', CONCAT(p.menu_path, '/selectionTrolleyWeight'), 1, p.menu_id, NULL, 'scale',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       187
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Selection Trolley Weight');

-- 2. Selection vs Jute Issue (report, under the Production Reports hub)
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Selection vs Jute Issue', CONCAT(p.menu_path, '/selectionVsIssue'), p.menu_id, 'compare_arrows', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Selection vs Jute Issue');

-- 3. role_menu_map for the entry page -- mirror every role's grant on
--    'Bale Production' (MAX access level held).
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    WHERE existing.menu_name = 'Bale Production'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Selection Trolley Weight') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- 4. role_menu_map for the report -- mirror each role's access on the
--    Production Reports hub (or any hub child), MAX level held.
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Selection vs Jute Issue') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
FROM menu_mst
WHERE menu_name IN ('Selection Trolley Weight', 'Selection vs Jute Issue');

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN
--   (SELECT menu_id FROM menu_mst WHERE menu_name IN ('Selection Trolley Weight', 'Selection vs Jute Issue'));
-- DELETE FROM menu_mst WHERE menu_name IN
--   ('Selection Trolley Weight', 'Selection vs Jute Issue');
-- =============================================================================
;
