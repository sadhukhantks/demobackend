-- =============================================================================
-- Migration: 'Low Performer Follow-up' menu (Jute Production reports hub)
-- =============================================================================
-- Date: 2026-10-07
-- TARGET: tenant DB only -- dev3 first, then sls (owner approved both).
-- Run AFTER 20261007_jute_prod_low_performer_followup.sql.
--
-- Adds ONE row under the 'Production Reports' hub (menu_path 'juteProduction/report',
-- resolved by PATH, never by id), next to the three Low Producer reports:
--   Low Performer Follow-up -> juteProduction/report/lowPerformerFollowup
-- report = 1 (it lives in the hub's dropdown with its siblings), menu_icon and
-- module_mst_id copied from the Weaving Low Producer sibling, order_by = the
-- Winding Low Producer's order_by (they tie, so it sorts beside the Low Producers
-- without renumbering anything else).
--
-- ROLE GRANTS (owner decision 6: same roles as the Low Producer reports): every role
-- holding ANY of the three Low Producer menus gets the new menu at the MAX access
-- level it holds on them. Read-only on 2026-10-07:
--   dev3 -> roles 1, 3, 12 (access 4)
--   sls  -> role 13 (access 4), roles 14, 53, 54, 56, 59, 61 (access 1 = view only)
-- Nothing hardcodes an id. vowconsole3.portal_menu_mst is deliberately NOT touched
-- (no juteProduction report rows there, same as the sibling report menus).
--
-- Runner note: the pymysql runner splits on semicolons and skips chunks that start
-- with a comment marker, so each comment block is terminated by a lone semicolon.
;

-- 1. Menu row
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, report, module_mst_id, order_by)
SELECT 'Low Performer Follow-up', 'juteProduction/report/lowPerformerFollowup', 1, s.menu_parent_id, s.menu_type_id,
       'person_search', 1, s.module_mst_id,
       COALESCE((SELECT w.order_by FROM menu_mst w WHERE w.menu_path = 'juteProduction/report/windingLowProducer' LIMIT 1), s.order_by)
FROM (SELECT menu_parent_id, menu_type_id, module_mst_id, order_by FROM menu_mst
      WHERE menu_path = 'juteProduction/report/weavingLowProducer' LIMIT 1) s
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'juteProduction/report/lowPerformerFollowup');

-- 2. role_menu_map -- union of the roles on the three Low Producer menus, MAX level
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst lp ON lp.menu_id = rmm.menu_id
    WHERE lp.menu_path IN ('juteProduction/report/weavingLowProducer',
                           'juteProduction/report/spinningLowProducer',
                           'juteProduction/report/windingLowProducer')
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/lowPerformerFollowup' LIMIT 1) m
WHERE NOT EXISTS (SELECT 1 FROM role_menu_map x WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- =============================================================================
-- VERIFICATION -- expect 1 menu row, 3 grants on dev3, 7 on sls
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, report, order_by, active
--     FROM menu_mst WHERE menu_path = 'juteProduction/report/lowPerformerFollowup'
--   SELECT rm.role_id, rm.access_type_id FROM role_menu_map rm
--     JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/lowPerformerFollowup'
--
-- Users must log out / in once: the sidebar caches the menu tree in localStorage.
--
-- ROLLBACK
--   DELETE rm FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--    WHERE m.menu_path = 'juteProduction/report/lowPerformerFollowup'
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/report/lowPerformerFollowup'
-- =============================================================================
;
