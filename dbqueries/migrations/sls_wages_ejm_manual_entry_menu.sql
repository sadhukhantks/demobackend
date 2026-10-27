-- "Wages Manual Entries" (wages/ejm/wagesManualEntry) under sls.menu_mst 1051 "Wages":
-- hand entry of working/ot/ns/fest hours, stl days and the efficiency hours on
-- tbl_ejm_wages_data_collection. Granted to slssuperadmin (role 13) at access 4,
-- the same grant its siblings carry -- users get it through their role.
--
-- Target DB: sls
--
-- ROLLBACK:
--   DELETE r FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path = 'wages/ejm/wagesManualEntry';
--   DELETE FROM menu_mst WHERE menu_path = 'wages/ejm/wagesManualEntry';

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Wages Manual Entries', 'wages/ejm/wagesManualEntry', 1, 1051, NULL, 'compare_arrows', 2, 65, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM (SELECT menu_id FROM menu_mst WHERE menu_path = 'wages/ejm/wagesManualEntry') have);

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT 13, m.menu_id, 4, 4, NOW()
FROM menu_mst m
WHERE m.menu_path = 'wages/ejm/wagesManualEntry'
  AND m.menu_id NOT IN (SELECT menu_id FROM (SELECT menu_id FROM role_menu_map WHERE role_id = 13) have);
