-- =============================================================================
-- Migration: move the 'Long Absenteeism' menu from Jute Production > Reports to HRMS
-- =============================================================================
-- Date: 2026-10-12 (owner: "shift this to the hrms module instead of the jute production menu")
-- TARGET: tenant DB only -- dev3 AND sls.
-- Run AFTER 20261011_jute_prod_absent_menu.sql. Deploy TOGETHER with the code that
-- resolves the menu by 'hrms/longAbsenteeism' first (long_absenteeism_common.MENU_PATHS)
-- and moves the frontend route to src/app/dashboardportal/hrms/longAbsenteeism.
--
-- menu_mst.menu_name is UNIQUE, so the row is RE-POINTED rather than re-created: the
-- same menu_id keeps its role grants and any approval_mst (struck-off approver) rows.
-- Idempotent: the UPDATE matches the old path only. Resolves parents by path, never by id.
--   new parent = the HRMS menu ('hrms'); menu_type_id / module_mst_id copied from the
--   'hrms/LeaveRequest' sibling; order_by 9 (right after Leave Request); report NULL (a
--   working page with its own tabs, not a row of the HRMS Reports hub).
-- Runner note: each comment block ends with a lone semicolon for the pymysql runner.
;
UPDATE menu_mst m
JOIN (SELECT menu_id AS hrms_id FROM menu_mst WHERE menu_path = 'hrms' AND active = 1 LIMIT 1) h
JOIN (SELECT menu_type_id, module_mst_id FROM menu_mst WHERE menu_path = 'hrms/LeaveRequest' LIMIT 1) s
SET m.menu_path      = 'hrms/longAbsenteeism',
    m.menu_parent_id = h.hrms_id,
    m.menu_type_id   = s.menu_type_id,
    m.module_mst_id  = s.module_mst_id,
    m.report         = NULL,
    m.order_by       = 9,
    m.active         = 1
WHERE m.menu_path = 'juteProduction/report/longAbsenteeism';

-- =============================================================================
-- VERIFICATION
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, menu_type_id, module_mst_id, report, order_by, active
--     FROM menu_mst WHERE menu_name = 'Long Absenteeism';          -- path hrms/longAbsenteeism, parent = hrms
--   SELECT rm.role_id, rm.access_type_id FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'hrms/longAbsenteeism';                      -- unchanged grants
-- Users must log out / in once (the sidebar caches the menu tree).
--
-- ROLLBACK (re-point back under the Production Reports hub)
--   UPDATE menu_mst m
--   JOIN (SELECT menu_id AS hub_id, menu_type_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report' LIMIT 1) r
--   JOIN (SELECT menu_type_id AS t, module_mst_id AS mod, order_by AS ob FROM menu_mst WHERE menu_path = 'juteProduction/report/lowPerformerFollowup' LIMIT 1) s
--   SET m.menu_path = 'juteProduction/report/longAbsenteeism', m.menu_parent_id = r.hub_id, m.menu_type_id = s.t,
--       m.module_mst_id = s.mod, m.report = 1, m.order_by = s.ob
--   WHERE m.menu_path = 'hrms/longAbsenteeism';
-- =============================================================================
;
