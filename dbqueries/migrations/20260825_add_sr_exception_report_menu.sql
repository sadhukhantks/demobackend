-- SR Exception Report (inventory): menu entry + role grants.
-- Run per tenant DB (dev3, sls). Parent = 'Inventory Report' (menu_path
-- 'inventory/reports'); grants cloned from the sibling 'Stores Min-Max'
-- report so the same roles see it.
--
-- Rollback:
--   DELETE rmm FROM role_menu_map rmm
--     JOIN menu_mst m ON m.menu_id = rmm.menu_id
--     WHERE m.menu_path = 'inventory/reports/srException';
--   DELETE FROM menu_mst WHERE menu_path = 'inventory/reports/srException';

INSERT INTO menu_mst
    (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon,
     module_mst_id, order_by, report)
SELECT 'SR Exception', 'inventory/reports/srException', 1, p.menu_id, NULL,
       'assessment', NULL, 110, 1
FROM menu_mst AS p
WHERE p.menu_path = 'inventory/reports'
  AND NOT EXISTS (
      SELECT 1 FROM menu_mst WHERE menu_path = 'inventory/reports/srException'
  );

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT src.role_id, m.menu_id, src.access_type_id, src.updated_by
FROM menu_mst AS m
JOIN menu_mst AS sib ON sib.menu_path = 'inventory/reports/inventoryMinMax'
JOIN role_menu_map AS src ON src.menu_id = sib.menu_id
WHERE m.menu_path = 'inventory/reports/srException'
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map AS x
      WHERE x.menu_id = m.menu_id AND x.role_id = src.role_id
  );
