-- =============================================================================
-- Migration: Period Master -- portal menu row + grants (TENANT DBs)
-- =============================================================================
-- Date: 2026-10-24
-- TARGET: tenant DBs -- dev3 FIRST. sls only on the owner's go.
-- Needs:  create_period_master.sql applied on the same tenant.
--
-- Adds 'Period Master' (hrmsmasters/PeriodMaster) under the 'HRMS Master'
-- parent (resolved by menu_path 'hrmsmasters', no hardcoded ids). Route:
--   vowerp3ui src/app/dashboardportal/hrmsmasters/PeriodMaster/page.tsx
-- vowconsole3.portal_menu_mst holds no HRMS-master rows, so there is no
-- template row to add.
--
-- GRANTS: every role holding Holiday Master (hrmsmasters/HolidayMaster) gets
-- the same access level on Period Master.
--
-- IDEMPOTENT: guarded by NOT EXISTS on menu_path and (role_id, menu_id).
-- POST-APPLY: users must log out and back in (sidebar caches the menu tree).
--
-- ROLLBACK:
--   DELETE r FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--     WHERE m.menu_path = 'hrmsmasters/PeriodMaster'
--   DELETE FROM menu_mst WHERE menu_path = 'hrmsmasters/PeriodMaster'
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Period Master', 'hrmsmasters/PeriodMaster', 1, p.menu_id, 1, 'calendar', 1, 11
FROM menu_mst p
WHERE p.menu_path = 'hrmsmasters' AND p.menu_parent_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM menu_mst x WHERE x.menu_path = 'hrmsmasters/PeriodMaster')
LIMIT 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT r.role_id, pm.menu_id, r.access_type_id, 1
FROM role_menu_map r
JOIN menu_mst sib ON sib.menu_id = r.menu_id AND sib.menu_path = 'hrmsmasters/HolidayMaster'
JOIN menu_mst pm ON pm.menu_path = 'hrmsmasters/PeriodMaster'
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = pm.menu_id);

-- VERIFICATION (run after apply):
--   SELECT m.menu_id, m.menu_name, r.role_id, r.access_type_id FROM menu_mst m
--   LEFT JOIN role_menu_map r ON r.menu_id = m.menu_id WHERE m.menu_path = 'hrmsmasters/PeriodMaster'
;
