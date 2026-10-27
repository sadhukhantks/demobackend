-- =============================================================================
-- Migration: 'Jute Costing' sidebar group + two pages (ITM-019)
-- =============================================================================
-- Date: 2026-10-18
-- TARGET: tenant DB only -- dev3 first, sls only when the owner names the DB
--         (the sls role block below stays commented out until then).
-- Pages (vowerp3ui src/app/dashboardportal/juteCosting/...):
--   Jute Costing      juteCosting                       hub, parent NULL, order 300
--   Cost Parameters   juteCosting/masters/costParams    icon tune,       order 310
--   Daily P&L         juteCosting/report/dailyPL        icon assessment, order 320
-- Backend prefixes /api/juteCostParam and /api/juteCostReports.
-- Schema: run 20261018_jute_cost_param.sql first (any order is safe, but the
-- pages are empty without the table).
--
-- A NEW top-level group (module_mst row + root menu_mst row) rather than rows
-- under Jute Production: costing is the commercial / MIS desk's screen, and
-- dailyPL is deliberately NOT added to the Production Reports hub dropdown.
-- Both pages are level 2 so they reach the sidebar (menu.py drops level 3).
-- Shape follows seed_jute_mech_menus_tenant.sql (module + root + level-2 pages)
-- and 20260930_hands_norms_menu.sql (grants mirrored from an existing row).
--
-- GRANTS (STEP 4): the hub and Daily P&L mirror MAX(access_type_id) per role from
-- the 'Daily Production Report' row -- the production audience that views the
-- DPR views the P&L. Cost Parameters mirrors 'Hands Norms' when that row exists
-- (whoever maintains norms maintains cost parameters), else the DPR row.
--
-- Nothing hardcodes a menu_id, module id or role_id. If an anchor row fails to
-- resolve the derived table is empty and the INSERT is a silent no-op, so always
-- run the VERIFICATION queries. Idempotent via NOT EXISTS on menu_path /
-- module_name / (role_id, menu_id).
--
-- COLLISION PRE-CHECK -- run BEFORE applying. menu_name is UNIQUE table-wide, so a
-- name already taken by an unrelated row would silently turn its INSERT into a
-- no-op. Expect ZERO rows back:
--   SELECT menu_id, menu_name, menu_path FROM menu_mst
--    WHERE menu_name IN ('Jute Costing', 'Cost Parameters', 'Daily P&L')
--      AND menu_path NOT LIKE 'juteCosting%'
--
-- POST-APPLY: users MUST log out and back in -- the portal sidebar caches the
-- menu tree in localStorage, so nothing new appears until it is refetched.
--
-- vowconsole3 portal_menu_mst TEMPLATE rows: a commented block at the end of
-- this file, to run separately on the console DB if the owner wants the
-- template kept in step (as seed_jute_mech_menus_console.sql did).
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
--
-- ROLLBACK:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_path IN ('juteCosting', 'juteCosting/masters/costParams', 'juteCosting/report/dailyPL')
--   then
--   DELETE FROM menu_mst
--    WHERE menu_path IN ('juteCosting', 'juteCosting/masters/costParams', 'juteCosting/report/dailyPL')
--   then
--   DELETE FROM module_mst WHERE module_name = 'Jute Costing'
--      AND NOT EXISTS (SELECT 1 FROM menu_mst WHERE module_mst_id = module_mst.module_mst_id)
;

-- =============================================================================
-- STEP 1 -- module_mst : the new application module (module_type inherited
-- from 'Jute Production' so it is classified like its sibling)
-- =============================================================================
;
INSERT INTO module_mst (module_name, module_type, active)
SELECT 'Jute Costing',
       (SELECT jp.module_type FROM module_mst jp WHERE jp.module_name = 'Jute Production' ORDER BY jp.module_mst_id LIMIT 1),
       1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM module_mst WHERE module_name = 'Jute Costing');

-- =============================================================================
-- STEP 2 -- menu_mst : the root hub (level 1, menu_parent_id NULL, order 300,
-- after Jute Production 100 and Jute Mechanical 200)
-- =============================================================================
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Costing', 'juteCosting', 1, NULL, NULL, 'calculate',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Costing' ORDER BY module_mst_id LIMIT 1),
       300, 0
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteCosting');

-- =============================================================================
-- STEP 3 -- menu_mst : level-2 pages (sidebar-visible), parent resolved by
-- menu_path 'juteCosting', module inherited from the hub
-- =============================================================================

-- 3.1 Cost Parameters -> juteCosting/masters/costParams
--     (collision pre-check on menu_name 'Cost Parameters' -- see header)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Cost Parameters', CONCAT(p.menu_path, '/masters/costParams'), 1, p.menu_id, NULL, 'tune',
       p.module_mst_id, 310, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_path = 'juteCosting' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteCosting/masters/costParams');

-- 3.2 Daily P&L -> juteCosting/report/dailyPL
--     (collision pre-check on menu_name 'Daily P&L' -- see header)
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Daily P&L', CONCAT(p.menu_path, '/report/dailyPL'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 320, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_path = 'juteCosting' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteCosting/report/dailyPL');

-- =============================================================================
-- STEP 4 -- role_menu_map : one row per (role_id, menu_id), updated_by 1
-- =============================================================================

-- 4.1 Hub 'juteCosting' -- mirror MAX(access_type_id) per role from
--     'Daily Production Report'
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst src ON src.menu_id = rmm.menu_id
    WHERE src.menu_name = 'Daily Production Report'
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT menu_id FROM menu_mst WHERE menu_path = 'juteCosting'
) m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- 4.2 Daily P&L -- same DPR audience
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst src ON src.menu_id = rmm.menu_id
    WHERE src.menu_name = 'Daily Production Report'
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT menu_id FROM menu_mst WHERE menu_path = 'juteCosting/report/dailyPL'
) m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- 4.3 Cost Parameters -- mirror 'Hands Norms' when that row exists, else the
--     DPR row (the CASE picks the anchor name at run time)
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst src ON src.menu_id = rmm.menu_id
    WHERE src.menu_name = (
        SELECT CASE WHEN EXISTS (SELECT 1 FROM menu_mst h WHERE h.menu_name = 'Hands Norms')
                    THEN 'Hands Norms' ELSE 'Daily Production Report' END
    )
    GROUP BY rmm.role_id
) r
CROSS JOIN (
    SELECT menu_id FROM menu_mst WHERE menu_path = 'juteCosting/masters/costParams'
) m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- STEP 5 -- sls ONLY (COMMENTED OUT until the owner says go for sls): the
-- commercial / MIS roles 13, 14, 36 and 53 get access 4 on all three rows,
-- and the DPR audience gets 1 (view) on Daily P&L. The mirroring in STEP 4
-- already grants the DPR audience on sls too, so this block adds the four
-- maintainer roles only. Remove the leading comment markers to run.
--
-- INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
-- SELECT r.role_id, m.menu_id, 4, 1, NOW()
-- FROM (SELECT 13 AS role_id UNION ALL SELECT 14 UNION ALL SELECT 36 UNION ALL SELECT 53) r
-- CROSS JOIN (
--     SELECT menu_id FROM menu_mst
--      WHERE menu_path IN ('juteCosting', 'juteCosting/masters/costParams', 'juteCosting/report/dailyPL')
-- ) m
-- WHERE EXISTS (SELECT 1 FROM roles_mst rm WHERE rm.role_id = r.role_id)
--   AND NOT EXISTS (
--     SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
-- )
--
-- UPDATE role_menu_map rmm
--   JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    SET rmm.access_type_id = 4, rmm.updated_by = 1, rmm.updated_date_time = NOW()
--  WHERE rmm.role_id IN (13, 14, 36, 53)
--    AND m.menu_path IN ('juteCosting', 'juteCosting/masters/costParams', 'juteCosting/report/dailyPL')
--    AND rmm.access_type_id < 4
;

-- =============================================================================
-- VERIFICATION (expect 3 menu rows and one grant per role of the anchor rows)
-- =============================================================================
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, module_mst_id, order_by
FROM menu_mst
WHERE menu_path LIKE 'juteCosting%'
ORDER BY order_by;

SELECT m.menu_path, rmm.role_id, rmm.access_type_id
FROM role_menu_map rmm
JOIN menu_mst m ON m.menu_id = rmm.menu_id
WHERE m.menu_path LIKE 'juteCosting%'
ORDER BY 1, 2;

-- =============================================================================
-- vowconsole3 portal_menu_mst TEMPLATE (COMMENTED OUT -- run separately on the
-- console DB only if the owner wants the template kept in step). Column
-- differences vs the tenant menu_mst (see seed_jute_mech_menus_console.sql):
-- the FK is module_id (NOT module_mst_id), there is NO report column,
-- menu_type_id is NOT NULL (inherited), top-level rows use menu_parent_id = 0.
--
-- INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
-- SELECT 'Jute Costing', 'juteCosting', 1, 0, a.menu_type_id, 'calculate', a.module_id, 300
-- FROM (
--     SELECT menu_type_id, module_id
--     FROM portal_menu_mst
--     WHERE (menu_parent_id = 0 OR menu_parent_id IS NULL) AND active = 1
--     ORDER BY CASE WHEN menu_name = 'Jute Production' THEN 0 ELSE 1 END, menu_id
--     LIMIT 1
-- ) a
-- WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_path = 'juteCosting')
--
-- INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
-- SELECT 'Cost Parameters', CONCAT(p.menu_path, '/masters/costParams'), 1, p.menu_id, p.menu_type_id, 'tune', p.module_id, 310
-- FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_path = 'juteCosting' LIMIT 1) p
-- WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_path = 'juteCosting/masters/costParams')
--
-- INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
-- SELECT 'Daily P&L', CONCAT(p.menu_path, '/report/dailyPL'), 1, p.menu_id, p.menu_type_id, 'assessment', p.module_id, 320
-- FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_path = 'juteCosting' LIMIT 1) p
-- WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_path = 'juteCosting/report/dailyPL')
;
