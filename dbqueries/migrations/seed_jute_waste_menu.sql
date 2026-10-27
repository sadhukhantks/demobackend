-- Migration: Seed 'Waste Entry' menu under 'Jute Production'
-- Date: 2026-08-08
-- Applies to: tenant database dev3 (QA)
--
-- Route /dashboardportal/juteProduction/wasteEntry (dev3 relative path 'juteProduction/wasteEntry').
-- Mirrors seed_stoppage_menu.sql: menu_mst ONLY. role_menu_map grants are applied per role by the
-- tenant admin afterwards (intentionally NOT seeded here). Path derived via CONCAT off the parent.
-- Run AFTER create_jute_prod_waste_entry.sql.

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Waste Entry', CONCAT(p.menu_path, '/wasteEntry'), 1, p.menu_id, NULL, 'delete_sweep',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       210
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Waste Entry');

-- =============================================================================
-- ROLLBACK:
-- DELETE FROM menu_mst WHERE menu_name = 'Waste Entry';
-- =============================================================================
