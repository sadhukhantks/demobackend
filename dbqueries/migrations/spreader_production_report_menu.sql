-- Spreader Production Report: Production Reports menu row (date-wise pivot of
-- spreader production per machine × quality and issue per quality).
--
-- Target DB: any tenant (applied on sls).
--
-- ROLLBACK:
--   DELETE r FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--     WHERE m.menu_path = 'juteProduction/report/spreaderProduction';
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/report/spreaderProduction';

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Spreader Production Report', 'juteProduction/report/spreaderProduction', 1, p.menu_id, 'assessment', p.module_mst_id, 70, 1
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report') p
WHERE NOT EXISTS (SELECT 1 FROM (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/spreaderProduction') x);

-- Roles: whatever the sibling Daily Production Report carries (sls and dev3
-- use different role conventions, so copy rather than hard-code).
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT sib.role_id, me.menu_id, sib.access_type_id, 1, NOW()
FROM (
  SELECT r.role_id, MAX(r.access_type_id) AS access_type_id
  FROM role_menu_map r
  JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteProduction/report/dailyProductionReport'
  GROUP BY r.role_id
) sib
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/spreaderProduction') me
LEFT JOIN (
  SELECT r.role_id FROM role_menu_map r
  JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteProduction/report/spreaderProduction'
) have ON have.role_id = sib.role_id
WHERE have.role_id IS NULL;
