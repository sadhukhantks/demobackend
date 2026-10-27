-- =============================================================================
-- Migration: Wastage Report menu (Production Reports hub)
-- =============================================================================
-- Date: 2026-08-25
-- TARGET: tenant DB only (dev3 first) — the juteProduction hub has no
-- vowconsole3 template row (same as 20260819_winding_stock_menus.sql).
-- POST-APPLY: users must re-login (sidebar menus cached in localStorage).
;

-- 1. menu_mst — report row under the Production Reports hub (report = 1 puts
-- it in the hub dropdown).
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Wastage Report', CONCAT(p.menu_path, '/wastageReport'), p.menu_id, 'recycling', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Wastage Report');

-- 2. role_menu_map — mirror each role's access on the hub (or any hub child),
-- MAX level held.
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Wastage Report') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, report FROM menu_mst WHERE menu_name = 'Wastage Report';
--   SELECT r.role_id, r.access_type_id FROM role_menu_map r
--     JOIN menu_mst m ON m.menu_id = r.menu_id WHERE m.menu_name = 'Wastage Report';
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name = 'Wastage Report';
--   DELETE FROM menu_mst WHERE menu_name = 'Wastage Report';
-- =============================================================================
