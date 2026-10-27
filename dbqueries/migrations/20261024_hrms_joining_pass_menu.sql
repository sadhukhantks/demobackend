-- =============================================================================
-- Migration: 'Joining Pass' menu (HRMS)
-- =============================================================================
-- Date: 2026-10-24
-- TARGET: tenant DB only -- dev3 first, sls only after the owner's OK.
-- Run AFTER 20261024_hrms_joining_pass.sql.
--
-- Adds ONE row under the HRMS menu (resolved by PATH, never by id), right after
-- Long Absenteeism:  Joining Pass -> hrms/joiningPass
-- menu_type_id / module_mst_id / parent copied from the 'hrms/longAbsenteeism' sibling.
-- ROLE GRANTS: every role holding Long Absenteeism gets the new menu at the same level.
-- Users must log out / in once (the sidebar caches the menu tree).
-- Runner note: each comment block ends with a lone semicolon for the pymysql runner.
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Joining Pass', 'hrms/joiningPass', 1, s.menu_parent_id, s.menu_type_id, 'badge', NULL, s.module_mst_id, 10
FROM (SELECT menu_parent_id, menu_type_id, module_mst_id FROM menu_mst
      WHERE menu_path = 'hrms/longAbsenteeism' LIMIT 1) s
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'hrms/joiningPass');

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst la ON la.menu_id = rmm.menu_id
    WHERE la.menu_path = 'hrms/longAbsenteeism'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'hrms/joiningPass' LIMIT 1) m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- =============================================================================
-- VERIFICATION
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, order_by, active FROM menu_mst WHERE menu_path = 'hrms/joiningPass';
--   SELECT rm.role_id, rm.access_type_id FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'hrms/joiningPass',
-- ROLLBACK
--   DELETE rm FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id WHERE m.menu_path = 'hrms/joiningPass';
--   DELETE FROM menu_mst WHERE menu_path = 'hrms/joiningPass';
-- =============================================================================
;
