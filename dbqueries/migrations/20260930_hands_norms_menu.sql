-- =============================================================================
-- Migration: 'Hands Norms' sidebar row under Jute Production (TKT-000202)
-- =============================================================================
-- Date: 2026-09-30. Menu for the new Hands Norms master page
-- (vowerp3ui juteProduction/masters/handsNorms, backend /api/handsNorms), which
-- maintains jute_prod_hands_norms_mst — the man-machine norms the Hands Report
-- reads its targets from. No schema change: the table already exists
-- (20260825_add_jute_prod_hands_norms_mst.sql).
-- TARGET: tenant DB only (dev3 first, then sls only when the owner names the DB).
-- vowconsole3 portal_menu_mst deliberately untouched, as for every other page
-- under the juteProduction hub.
--
-- One menu_mst row under the 'Jute Production' hub, resolved by menu_path
-- 'juteProduction' (fallback by name — dev3 768, sls 769, never hardcoded):
--   Hands Norms   juteProduction/masters/handsNorms   icon groups   order 207
-- report NULL (a master, not a hub-dropdown report). module_mst_id and the
-- role_menu_map grants are COPIED from the 'Hands Report' row, so exactly the
-- audience that reads the report can open the norms behind it, each role at the
-- access level it has on the report (sls today: role 13 full, 14/54/56/59/61 view).
--
-- Nothing hardcodes a menu_id, module id or role_id. If the hub or the 'Hands
-- Report' row fails to resolve the derived table is empty and the INSERT is a
-- silent no-op, so always run the VERIFICATION queries. Idempotent via NOT EXISTS.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users MUST
-- log out and back in before the row appears.
--
-- ROLLBACK:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_path = 'juteProduction/masters/handsNorms'
--   then
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/masters/handsNorms'
;

-- A. menu_mst — 'Hands Norms' (master) under the Jute Production hub
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Hands Norms', CONCAT(p.menu_path, '/masters/handsNorms'), 1, p.menu_id, NULL, 'groups',
       (SELECT hr.module_mst_id FROM menu_mst hr
         WHERE hr.menu_name = 'Hands Report'
         ORDER BY hr.menu_id LIMIT 1),
       207, NULL
FROM (
    SELECT menu_id, menu_path
    FROM menu_mst
    WHERE menu_path = 'juteProduction' OR menu_name = 'Jute Production'
    ORDER BY CASE WHEN menu_path = 'juteProduction' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/masters/handsNorms');

-- B. role_menu_map — mirror each role's access on 'Hands Report' onto the new row
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst hr ON hr.menu_id = rmm.menu_id
    WHERE hr.menu_name = 'Hands Report'
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/masters/handsNorms'
) m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- VERIFICATION (expect 1 menu row, and as many grants as 'Hands Report' has roles)
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, module_mst_id, order_by
FROM menu_mst WHERE menu_path = 'juteProduction/masters/handsNorms';

SELECT rmm.role_id, rmm.access_type_id
FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
WHERE m.menu_path = 'juteProduction/masters/handsNorms'
ORDER BY rmm.role_id;
