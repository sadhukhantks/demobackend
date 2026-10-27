-- =============================================================================
-- Migration: Bale Production menus (docs/bale-production-spec-sheet.md §7)
-- =============================================================================
-- Date: 2026-09-16
-- TARGET: tenant DB only (dev3 first, sls once BE+FE are deployed there).
-- vowconsole3.portal_menu_mst deliberately untouched -- the juteProduction hub
-- has no template row there; every sibling under the hub is tenant-only.
--
-- Adds 4 rows:
--   1. 'Bale Production'   (txn)    -> juteProduction/baleProduction
--   2. 'Bale Series Master' (master) -> juteProduction/masters/baleSeriesMaster
--   3. 'Daily Bale Sheet'  (report) -> juteProduction/report/baleDailySheet
--   4. 'Bale Heavy Light'  (report) -> juteProduction/report/baleHeavyLight
-- Rows 1-2 follow seed_finishing_menu.sql (menu_mst only, role grants left to the
-- tenant admin). Rows 3-4 follow 20260819_winding_quality_wise_menus.sql
-- (report = 1, order_by = MAX(sibling)+1, role_menu_map mirrored from the hub).
-- Nothing hardcodes an id. Run AFTER create_jute_prod_bale_tables.sql.
-- The finishing 'Bale Press' tile (juteProduction/finishing/balePress) now
-- redirects to row 1; it is intentionally left in place.
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

-- 1. Bale Production (transaction page, under Jute Production)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Bale Production', CONCAT(p.menu_path, '/baleProduction'), 1, p.menu_id, NULL, 'inventory_2',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       186
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Bale Production');

-- 2. Bale Series Master (master, under Jute Production; '/masters/' is part of the path)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Bale Series Master', CONCAT(p.menu_path, '/masters/baleSeriesMaster'), 1, p.menu_id, NULL, 'format_list_numbered',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       205
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Bale Series Master');

-- 3. Daily Bale Sheet (report, under the Production Reports hub)
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Daily Bale Sheet', CONCAT(p.menu_path, '/baleDailySheet'), p.menu_id, 'receipt_long', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Daily Bale Sheet');

-- 4. Bale Heavy Light (report, under the Production Reports hub)
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Bale Heavy Light', CONCAT(p.menu_path, '/baleHeavyLight'), p.menu_id, 'scale', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Bale Heavy Light');

-- 5. role_menu_map for the two reports -- mirror each role's access on the
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name IN ('Daily Bale Sheet', 'Bale Heavy Light')) m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- 6. role_menu_map for Bale Production + Bale Series Master -- mirror each role's
--    access on 'Finishing Production' (bale press used to live under it). Added
--    2026-09-21: without it nobody sees the two pages until the tenant admin grants.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst fp ON fp.menu_id = rmm.menu_id AND fp.menu_name = 'Finishing Production'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name IN ('Bale Production', 'Bale Series Master')) m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
FROM menu_mst
WHERE menu_name IN ('Bale Production', 'Bale Series Master', 'Daily Bale Sheet', 'Bale Heavy Light');

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN
--   (SELECT menu_id FROM menu_mst WHERE menu_name IN
--    ('Bale Production', 'Bale Series Master', 'Daily Bale Sheet', 'Bale Heavy Light'));
-- DELETE FROM menu_mst WHERE menu_name IN
--   ('Bale Production', 'Bale Series Master', 'Daily Bale Sheet', 'Bale Heavy Light');
-- =============================================================================
;
