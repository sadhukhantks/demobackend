-- =============================================================================
-- Migration: Winding Quality Wise report + Winding Standards master menus
-- =============================================================================
-- Date: 2026-08-19
-- TARGET: tenant DB only (dev3, then sls once BE+FE are deployed there).
-- vowconsole3.portal_menu_mst deliberately untouched -- it has no juteProduction
-- hub template at all, every sibling row under the hub is tenant-only.
--
-- Nothing hardcodes a menu_id, module id or role_id: every id resolves at run
-- time off existing rows, so the same script promotes to other tenants
-- unmodified. If an anchor fails to resolve the derived table is empty and the
-- INSERT is a silent no-op -- always run the VERIFICATION queries.
--
-- The MASTER row's path is CONCAT(hub.menu_path, '/masters/windingTargetMap'),
-- copying the anchor seed_weaving_menu.sql / seed_beaming_menu.sql used: the
-- parent is the 'Jute Production' hub itself and the '/masters/' segment is part
-- of the path (there is NO separate masters hub menu row). That matches the FE
-- route, which lives beside weavingTargetMap under .../juteProduction/masters/.
--
-- Runner note: the pymysql migration runner splits on ';' and skips chunks that
-- start with '--', so each comment block is terminated by a lone ';' below and no
-- ';' appears inside any string.
;

-- A1. menu_mst -- 'Winding Quality Wise' report under the Production Reports hub.
-- report = 1 is what puts it in the hub dropdown (fetchReportMenuTree filters
-- report = 1 children); order_by = MAX(sibling) + 1.
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Winding Quality Wise', CONCAT(p.menu_path, '/windingQualityWise'), p.menu_id, 'table_chart', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Winding Quality Wise');

-- A2. role_menu_map -- mirror each role's access on the report hub (or any hub
-- child), taking the MAX level held, so exactly today's Production Reports
-- audience can open the new report.
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Winding Quality Wise') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- B1. menu_mst -- 'Winding Standards' master, flat sibling of Weaving Standards
-- / Beaming Standards under the Jute Production hub (report = 0, icon 'tune' like
-- every other standards master). order_by = MAX(sibling) + 1.
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Winding Standards', CONCAT(p.menu_path, '/masters/windingTargetMap'), 1, p.menu_id, NULL, 'tune', 0,
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Winding Standards');

-- B2. role_menu_map -- mirror whoever can already reach Weaving Standards, the
-- nearest equivalent standards master. Falls back to nothing if that row was
-- never granted, in which case the tenant admin grants it by hand.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    WHERE existing.menu_name IN ('Weaving Standards', 'Beaming Standards', 'Spinning Standards')
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Winding Standards') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION -- run manually after applying
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by
--     FROM menu_mst WHERE menu_name IN ('Winding Quality Wise', 'Winding Standards')
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Winding Quality Wise', 'Winding Standards')
--    ORDER BY m.menu_name, r.role_id
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users MUST
-- log out and back in before either entry appears. Then confirm: 'Winding Quality
-- Wise' in the Production Reports dropdown and 'Winding Standards' in the Jute
-- Production sidebar next to Weaving Standards.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name IN ('Winding Quality Wise', 'Winding Standards')
--   DELETE FROM menu_mst WHERE menu_name IN ('Winding Quality Wise', 'Winding Standards')
-- =============================================================================
