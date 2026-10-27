-- =============================================================================
-- Migration: 'Loom Modification Impact' report row under the Production Reports hub
-- =============================================================================
-- Date: 2026-09-28   Phase 2 of
-- docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md (§7, D7)
-- TARGET: tenant DB only (dev3 first, sls ONLY after the §9 timing gate passes and
-- the owner names the DB). vowconsole3 portal_menu_mst deliberately untouched,
-- exactly as for every other report under the juteProduction hub
-- (clone of 20260926_purchased_yarn_ledger_menu.sql).
--
-- Backend: GET /api/juteProductionReports/loom_modification_impact
-- (src/juteProduction/loom_modification_impact.py). Frontend page path resolves to
-- <hub menu_path>/loomModificationImpact, i.e.
-- juteProduction/report/loomModificationImpact.
--
-- Nothing hardcodes a menu_id, module id or role_id: the hub resolves by path
-- (fallback by name), the module rides the hub's own module_mst_id, and role
-- access mirrors the hub's audience. If the hub fails to resolve the derived
-- table is empty and the INSERT is a silent no-op, so always run the
-- VERIFICATION queries. Idempotent via NOT EXISTS.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks
-- that start with a comment marker, so every comment block ends with a bare
-- semicolon line and contains no semicolon of its own.
--
-- POST-APPLY: portal sidebars cache their menu tree in localStorage, so users
-- MUST log out and back in before the report appears in the hub dropdown.
;

-- B1. menu_mst — report row under the Production Reports hub. report = 1 is
-- what puts it in the hub dropdown (fetchReportMenuTree filters report = 1)
;
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by, module_mst_id)
SELECT 'Loom Modification Impact', CONCAT(p.menu_path, '/loomModificationImpact'), p.menu_id, 'insights', 1, 1,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id),
       p.module_mst_id
FROM (
    SELECT menu_id, menu_path, module_mst_id
    FROM menu_mst
    WHERE menu_path LIKE '%juteProduction/report' OR menu_name = 'Production Reports'
    ORDER BY CASE WHEN menu_path LIKE '%juteProduction/report' THEN 0 ELSE 1 END
    LIMIT 1
) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Loom Modification Impact');

-- B2. role_menu_map — mirror each role's access on the hub (or any hub child),
-- taking the MAX level held, so exactly today's Production Reports audience
-- can open the new report
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
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Loom Modification Impact') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by, module_mst_id
--     FROM menu_mst WHERE menu_name = 'Loom Modification Impact'
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name = 'Loom Modification Impact'
--    ORDER BY r.role_id
--
-- Then re-login and confirm 'Loom Modification Impact' in the Production Reports
-- dropdown, opening juteProduction/report/loomModificationImpact.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name = 'Loom Modification Impact'
--   DELETE FROM menu_mst WHERE menu_name = 'Loom Modification Impact'
-- =============================================================================
;
