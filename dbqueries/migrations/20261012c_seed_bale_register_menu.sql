-- =============================================================================
-- Migration: 'Bale Register' report menu (spec §7c)
-- Date: 2026-10-12
-- Spec: docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §7c
-- TARGET: tenant DB only (dev3 first, sls where the feature is live).
--
-- Adds ONE report row under the 'Production Reports' hub:
--   'Bale Register' -> juteProduction/report/baleRegister
-- report = 1, order_by = MAX(sibling) + 1, role_menu_map mirrored from the hub
-- (every role that can see the hub or any hub child, MAX level held) — the
-- same shape as seed_bale_production_menus.sql rows 3–5. Nothing hardcodes an id.
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

-- 1. The report row
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Bale Register', CONCAT(p.menu_path, '/baleRegister'), p.menu_id, 'menu_book', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Bale Register');

-- 2. role_menu_map mirrored from the Production Reports hub
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Bale Register') m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
FROM menu_mst
WHERE menu_name = 'Bale Register';

-- Users must log out/in once: the sidebar caches the menu tree in localStorage.
-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Bale Register');
-- DELETE FROM menu_mst WHERE menu_name = 'Bale Register';
-- =============================================================================
;
