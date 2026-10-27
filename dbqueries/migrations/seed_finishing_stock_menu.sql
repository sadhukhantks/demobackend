-- Migration: Seed Finishing Stock menus.
-- Date: 2026-09-17
-- Spec: docs/superpowers/specs/2026-09-17-finishing-stock-design.md §2 (Seeds)
-- Applies to: tenant DBs (dev3 first).
--
-- Adds:
--   1. 'Finishing Stock'        -> juteProduction/finishingStock              (entry page)
--   2. 'Finishing Stock Report' -> juteProduction/report/finishingStockReport (report)
--
-- menu_mst ONLY, like seed_finishing_menu.sql: role_menu_map grants are applied
-- per role by the tenant admin afterwards (intentionally NOT seeded here).
-- Paths are derived with CONCAT off the parent row, looked up BY NAME — parent
-- menu ids differ per tenant (dev3: 768 'Jute Production', 945 'Production Reports').
-- APPLIED: dev3 2026-09-17 (menu 1025/1026); sls 2026-09-17 (menu 1042/1043).

-- 1. Finishing Stock (entry page, under Jute Production)
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Finishing Stock', CONCAT(p.menu_path, '/finishingStock'), 1, p.menu_id, NULL, 'inventory_2',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       167
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Finishing Stock');

-- 2. Finishing Stock Report (under Production Reports; report = 1 like its siblings)
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Finishing Stock Report', CONCAT(p.menu_path, '/finishingStockReport'), 1, p.menu_id, NULL, 'assessment',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Production' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       62, 1
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_name = 'Production Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Finishing Stock Report');

-- =============================================================================
-- ROLLBACK:
--   DELETE FROM menu_mst WHERE menu_name IN ('Finishing Stock', 'Finishing Stock Report');
-- =============================================================================
