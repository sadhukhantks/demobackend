-- =============================================================================
-- Migration: Jute Gain/Loss Sheet entry page + Jute Gain/Loss report menu
-- =============================================================================
-- Date: 2026-08-25
-- TARGET: tenant DB only (dev3 first). vowconsole3.portal_menu_mst deliberately
-- untouched — the juteProduction hub has no console template row (same as
-- 20260819_winding_stock_menus.sql).
--
-- Nothing hardcodes a menu_id, module id or role_id: every id resolves at run
-- time off existing rows, so the same script promotes to other tenants
-- unmodified. If an anchor fails to resolve the derived table is empty and the
-- INSERT is a silent no-op — always run the VERIFICATION queries.
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users
-- MUST log out and back in before either page appears.
;

-- A1. menu_mst — 'Jute Gain/Loss Sheet' entry page, sibling under Jute Production
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Jute Gain/Loss Sheet', CONCAT(p.menu_path, '/gainLossSheet'), 1, p.menu_id, NULL, 'balance',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Jute Gain/Loss Sheet');

-- A2. role_menu_map — mirror whoever can already reach Winding Stock (the
-- closest sibling entry page: same audience counts month-end stock)
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT rmm.role_id, m.menu_id, rmm.access_type_id, 1
FROM role_menu_map rmm
JOIN menu_mst src ON src.menu_id = rmm.menu_id AND src.menu_name = 'Winding Stock'
JOIN menu_mst m ON m.menu_name = 'Jute Gain/Loss Sheet'
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.menu_id = m.menu_id AND x.role_id = rmm.role_id AND x.access_type_id = rmm.access_type_id
);

-- B1. menu_mst — 'Jute Gain/Loss' report row under the Production Reports hub.
-- report = 1 puts it in the hub dropdown (fetchReportMenuTree filters report=1).
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Jute Gain/Loss', CONCAT(p.menu_path, '/juteGainLoss'), p.menu_id, 'balance', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Jute Gain/Loss');

-- B2. role_menu_map — mirror each role's access on the hub (or any hub child),
-- MAX level held, so exactly today's Production Reports audience opens it.
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Jute Gain/Loss') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
--     FROM menu_mst WHERE menu_name IN ('Jute Gain/Loss Sheet', 'Jute Gain/Loss');
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Jute Gain/Loss Sheet', 'Jute Gain/Loss')
--    ORDER BY m.menu_name, r.role_id;
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name IN ('Jute Gain/Loss Sheet', 'Jute Gain/Loss');
--   DELETE FROM menu_mst WHERE menu_name IN ('Jute Gain/Loss Sheet', 'Jute Gain/Loss');
-- =============================================================================
