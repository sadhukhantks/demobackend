-- =============================================================================
-- Migration: 'Bale Stock & Inspection' portal menu
-- Date: 2026-10-12
-- Spec: docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §2 (D10)
-- TARGET: tenant DB only (dev3 first, sls once BE+FE are deployed there).
-- vowconsole3.portal_menu_mst deliberately untouched — the juteProduction hub
-- has no template row there; every sibling under the hub is tenant-only.
--
-- Adds ONE row under 'Jute Production':
--   'Bale Stock & Inspection' -> juteProduction/baleStock   (order_by 168, right
--   after 'Finishing Stock' 167)
-- plus role_menu_map mirrored from 'Finishing Stock' (same access level per
-- role), the way seed_bale_production_menus.sql mirrors 'Finishing Production'.
-- Nothing hardcodes an id. Run AFTER 20261012_jute_prod_bale_inspection_despatch.sql.
-- APPLIED: dev3 2026-10-12 (menu 1051); sls 2026-10-12 (menu 1080, 6 role grants mirrored).
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

-- 1. The menu row (transaction page, under Jute Production)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Bale Stock & Inspection', CONCAT(p.menu_path, '/baleStock'), 1, p.menu_id, NULL, 'fact_check',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       168
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Bale Stock & Inspection');

-- 2. role_menu_map — mirror each role's access on 'Finishing Stock'
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst fs ON fs.menu_id = rmm.menu_id AND fs.menu_name = 'Finishing Stock'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Bale Stock & Inspection') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, order_by
FROM menu_mst
WHERE menu_name = 'Bale Stock & Inspection';

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN
--   (SELECT menu_id FROM menu_mst WHERE menu_name = 'Bale Stock & Inspection');
-- DELETE FROM menu_mst WHERE menu_name = 'Bale Stock & Inspection';
-- =============================================================================
;
