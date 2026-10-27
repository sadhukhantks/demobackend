-- Migration: Seed 'Wastage Area Master' menu under 'Jute Production'
-- Date: 2026-08-10
-- Applies to: tenant databases dev3 and sls
--
-- Route /dashboardportal/juteProduction/masters/wastageArea.
-- Mirrors seed_jute_waste_menu.sql for the menu row, then grants the new menu to exactly the
-- roles that already hold 'Waste Entry' (same access_type_id) -- whoever books waste maintains
-- the areas it is booked against. Tenants with no Waste Entry grants get no grants here either.
-- Run AFTER create_wastage_area_mst.sql.
-- NOTE keep semicolons out of these comments -- the migration runner splits on them
--
-- ROLLBACK:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--     WHERE m.menu_name = 'Wastage Area Master'
--   DELETE FROM menu_mst WHERE menu_name = 'Wastage Area Master'

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Wastage Area Master', CONCAT(p.menu_path, '/masters/wastageArea'), 1, p.menu_id, NULL, 'pin_drop',
       (SELECT module_mst_id FROM menu_mst WHERE menu_name = 'Waste Entry' LIMIT 1),
       211
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Wastage Area Master');

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT src.role_id, tgt.menu_id, src.access_type_id, src.updated_by
FROM role_menu_map src
JOIN menu_mst waste ON waste.menu_id = src.menu_id AND waste.menu_name = 'Waste Entry'
JOIN menu_mst tgt ON tgt.menu_name = 'Wastage Area Master'
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map existing
    WHERE existing.menu_id = tgt.menu_id AND existing.role_id = src.role_id
);
