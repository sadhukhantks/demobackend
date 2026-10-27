-- S4 Incentive Report (port of legacy Data-Portal-Php S4_loom_incentive_report_model,
-- legacy submenu 677): efficiency-slab incentive table + Production Reports menu row.
--
-- Target DB: any tenant (sls has the data — see sls_s4_incentive_data_from_empmill12.sql).
--
-- ROLLBACK:
--   DELETE r FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--     WHERE m.menu_path = 'juteProduction/report/s4Incentive';
--   DELETE FROM menu_mst WHERE menu_path = 'juteProduction/report/s4Incentive';
--   DROP TABLE tbl_incentive_data;

-- Same shape and name as EMPMILL12.tbl_incentive_data so the legacy ids copy 1:1.
-- One row per (company, branch, department, whole-number efficiency): a worker's
-- day at efficiency eff_inc earns total_inc (= rate + rate_inc).
CREATE TABLE IF NOT EXISTS tbl_incentive_data (
  incentive_data_id int NOT NULL AUTO_INCREMENT,
  department_id int DEFAULT NULL,
  rate decimal(7,2) DEFAULT NULL,
  rate_inc decimal(7,2) DEFAULT NULL,
  total_inc decimal(7,2) DEFAULT NULL,
  company_id int DEFAULT NULL,
  branch_id int DEFAULT NULL,
  is_active int DEFAULT '1',
  eff_inc decimal(6,2) DEFAULT NULL,
  PRIMARY KEY (incentive_data_id),
  KEY ix_incentive_scope (company_id, branch_id, department_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Menu row under Production Reports (menu_path juteProduction/report), placed
-- right after the other weaving reports.
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_icon, module_mst_id, order_by, report)
SELECT 'S4 Incentive Report', 'juteProduction/report/s4Incentive', 1, p.menu_id, 'paid', p.module_mst_id, 67, 1
FROM (SELECT menu_id, module_mst_id FROM menu_mst WHERE menu_path = 'juteProduction/report') p
WHERE NOT EXISTS (SELECT 1 FROM (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/s4Incentive') x);

-- Roles: whatever the sibling Weaving Low Producer report carries (sls and dev3
-- use different role conventions, so copy rather than hard-code).
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT sib.role_id, me.menu_id, sib.access_type_id, 1, NOW()
FROM (
  SELECT r.role_id, MAX(r.access_type_id) AS access_type_id
  FROM role_menu_map r
  JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteProduction/report/weavingLowProducer'
  GROUP BY r.role_id
) sib
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/report/s4Incentive') me
LEFT JOIN (
  SELECT r.role_id FROM role_menu_map r
  JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteProduction/report/s4Incentive'
) have ON have.role_id = sib.role_id
WHERE have.role_id IS NULL;
