-- =============================================================================
-- Migration: 'Long Absenteeism' menu (Jute Production reports hub)
-- =============================================================================
-- Date: 2026-10-11
-- TARGET: tenant DB only -- dev3 AND sls (owner decision 2026-10-11).
-- Run AFTER 20261011_jute_prod_absent_tables.sql.
--
-- Adds ONE row under the 'Production Reports' hub (menu_path 'juteProduction/report',
-- resolved by PATH, never by id), beside Low Performer Follow-up:
--   Long Absenteeism -> juteProduction/report/longAbsenteeism
-- report = 1 (hub dropdown), menu_type_id / module_mst_id / parent copied from the
-- Low Performer Follow-up sibling, order_by = that sibling's order_by (ties sort
-- beside it without renumbering anything else).
--
-- ROLE GRANTS: every role holding the Low Performer Follow-up menu gets the new
-- menu at the same access level (owner: "same roles as Low Performer Follow-up").
-- Known on 2026-10-08: dev3 roles 1, 3, 12 (level 4); sls role 13 and 14 (level 4),
-- roles 53, 54, 56, 59, 61 (level 1 view only). Nothing hardcodes an id.
-- vowconsole3.portal_menu_mst is deliberately NOT touched (no juteProduction
-- report rows there, same as the sibling report menus).
--
-- Struck-off approval: the standard approval engine reads approval_mst for this
-- menu_id + branch_id (Tenant Admin > Approval Hierarchy). With no row, approval
-- falls back to "any user with edit access approves directly".
--
-- Runner note: the pymysql runner splits on semicolons and skips chunks that start
-- with a comment marker, so each comment block is terminated by a lone semicolon.
;

-- 1. Menu row
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Long Absenteeism', 'juteProduction/report/longAbsenteeism', 1, s.menu_parent_id, s.menu_type_id,
       'person_off', 1, s.module_mst_id, s.order_by
FROM (SELECT menu_parent_id, menu_type_id, module_mst_id, order_by FROM menu_mst
      WHERE menu_path = 'juteProduction/report/lowPerformerFollowup' LIMIT 1) s
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/longAbsenteeism');

-- 2. role_menu_map -- same roles + levels as Low Performer Follow-up
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst lp ON lp.menu_id = rmm.menu_id
    WHERE lp.menu_path = 'juteProduction/report/lowPerformerFollowup'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/longAbsenteeism' LIMIT 1) m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- =============================================================================
-- VERIFICATION -- expect 1 menu row; grants = the follow-up menu's grants
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by, active
--     FROM menu_mst WHERE menu_path = 'juteProduction/report/longAbsenteeism'
--   SELECT rm.role_id, rm.access_type_id FROM role_menu_map rm
--     JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/longAbsenteeism'
--
-- Users must log out / in once: the sidebar caches the menu tree in localStorage.
--
-- ROLLBACK
--   DELETE rm FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/longAbsenteeism'
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/report/longAbsenteeism'
-- =============================================================================
;
