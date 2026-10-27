-- Grant the Waste Entry + Wastage Area Master menus to dev3's portal role 1.
-- Date: 2026-08-10
-- Applies to: tenant database dev3 ONLY (sls already carries its own grants).
--
-- Every other juteProduction menu in dev3 is mapped to role 1 with access_type_id 4, but
-- 'Waste Entry' was seeded menu-only and never granted, so the page was unreachable from the
-- sidebar. This closes that gap and grants the new area master the same way.
-- NOTE keep semicolons out of these comments -- the migration runner splits on them
--
-- ROLLBACK:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--     WHERE rmm.role_id = 1 AND m.menu_name IN ('Waste Entry', 'Wastage Area Master')

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT 1, m.menu_id, 4, 1
FROM menu_mst m
WHERE m.menu_name IN ('Waste Entry', 'Wastage Area Master')
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map rmm WHERE rmm.menu_id = m.menu_id AND rmm.role_id = 1
  );
