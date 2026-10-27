-- =============================================================================
-- Migration: 'Loom Groups' + 'Loom Modifications' sidebar rows under Jute Production
-- =============================================================================
-- Date: 2026-09-28   Phase 1 of
-- docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md (§6, D7)
-- TARGET: tenant DB only (dev3, then sls only when the owner names the DB).
-- vowconsole3 portal_menu_mst deliberately untouched, exactly as for every other
-- page under the juteProduction hub (seed_weaving_menu.sql shape).
--
-- Two menu_mst rows under the 'Jute Production' hub, resolved by menu_path
-- 'juteProduction' (fallback by name — dev3 768, sls 769, never hardcoded):
--   Loom Groups          juteProduction/masters/loomGroupMaster  icon group_work  order 205
--   Loom Modifications   juteProduction/loomModification         icon build       order 206
-- report NULL (entry pages, not hub-dropdown reports). module_mst_id and the
-- role_menu_map grants are COPIED from the 'Weaving Standards' row (the sibling
-- master these pages sit after, order_by 204), so exactly today's Weaving Standards
-- audience can open the new pages. Backend routers: /api/loomModification.
--
-- Nothing hardcodes a menu_id, module id or role_id. If the hub or the 'Weaving
-- Standards' row fails to resolve the derived table is empty and the INSERT is a
-- silent no-op, so always run the VERIFICATION queries. Idempotent via NOT EXISTS.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users MUST
-- log out and back in before the rows appear.
;

-- A1. menu_mst — 'Loom Groups' (master) under the Jute Production hub
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Loom Groups', CONCAT(p.menu_path, '/masters/loomGroupMaster'), 1, p.menu_id, NULL, 'group_work',
       (SELECT ws.module_mst_id FROM menu_mst ws
         WHERE ws.menu_name = 'Weaving Standards' AND ws.menu_parent_id = p.menu_id
         ORDER BY ws.menu_id LIMIT 1),
       205, NULL
FROM (
    SELECT menu_id, menu_path
    FROM menu_mst
    WHERE menu_path = 'juteProduction' OR menu_name = 'Jute Production'
    ORDER BY CASE WHEN menu_path = 'juteProduction' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Loom Groups');

-- A2. menu_mst — 'Loom Modifications' (entry register) under the Jute Production hub
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Loom Modifications', CONCAT(p.menu_path, '/loomModification'), 1, p.menu_id, NULL, 'build',
       (SELECT ws.module_mst_id FROM menu_mst ws
         WHERE ws.menu_name = 'Weaving Standards' AND ws.menu_parent_id = p.menu_id
         ORDER BY ws.menu_id LIMIT 1),
       206, NULL
FROM (
    SELECT menu_id, menu_path
    FROM menu_mst
    WHERE menu_path = 'juteProduction' OR menu_name = 'Jute Production'
    ORDER BY CASE WHEN menu_path = 'juteProduction' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Loom Modifications');

-- B. role_menu_map — mirror each role's access on 'Weaving Standards' onto both new
-- rows (same access_type_id), so the new pages share that master's audience
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst ws ON ws.menu_id = rmm.menu_id
    WHERE ws.menu_name = 'Weaving Standards'
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT menu_id FROM menu_mst WHERE menu_name IN ('Loom Groups', 'Loom Modifications')
) m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, menu_icon, report, order_by, module_mst_id
--     FROM menu_mst WHERE menu_name IN ('Loom Groups', 'Loom Modifications')
--     expected 2 rows, parent = the juteProduction hub id, order_by 205 / 206,
--     module_mst_id = that of 'Weaving Standards'
--
--   SELECT m.menu_name, r.role_id, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Loom Groups', 'Loom Modifications', 'Weaving Standards')
--    ORDER BY r.role_id, m.menu_name
--     expected the same role_ids for all three names
--
-- Then re-login and confirm both rows under Jute Production, opening
-- juteProduction/masters/loomGroupMaster and juteProduction/loomModification.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name IN ('Loom Groups', 'Loom Modifications')
--   DELETE FROM menu_mst WHERE menu_name IN ('Loom Groups', 'Loom Modifications')
-- =============================================================================
;
