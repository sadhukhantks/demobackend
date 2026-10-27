-- Production Quality <-> Wages Quality link (hrmsmasters/ProdWagesQualLink).
-- One row per (department, production quality) naming the wages-quality rate
-- card (tbl_wages_quality_master.q_code_id) it is paid on. prod_quality_id points
-- at a different table depending on the department: jute_prod_weaving_quality,
-- jute_prod_bm_quality, jute_prod_finishing_quality, or item_mst (yarn) for winding.
-- branch_id is copied from dept_mst on save. Already exists on sls (empty).
-- Run with the `run-migration` skill against the target tenant DB.

CREATE TABLE IF NOT EXISTS `link_prodquality_wagequality_mst` (
  `link_prod_wages_id` int NOT NULL AUTO_INCREMENT,
  `prod_quality_id` int DEFAULT NULL,
  `wages_quality_id` int DEFAULT NULL,
  `branch_id` int DEFAULT NULL,
  `dept_id` int DEFAULT NULL,
  `active` int DEFAULT NULL,
  PRIMARY KEY (`link_prod_wages_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The wages-quality rate card the link points at. Canonical DDL lives in
-- njm_wages_masters.sql; repeated here (IF NOT EXISTS) because that file's view
-- needs tbl_njm_wages_data_collection, which tenants without NJM wages lack.
CREATE TABLE IF NOT EXISTS `tbl_wages_quality_master` (
  `q_code_id` int NOT NULL AUTO_INCREMENT,
  `q_code` varchar(10) DEFAULT NULL,
  `quality` varchar(100) DEFAULT NULL,
  `rate1` decimal(11,8) DEFAULT '0.00000000',
  `rate2` decimal(11,8) DEFAULT '0.00000000',
  `unit` varchar(10) DEFAULT NULL,
  `std_rate_pu` int DEFAULT '1',
  `curr_rate_pu` int DEFAULT '1',
  `master_dept_id` int DEFAULT NULL,
  `min_prod` decimal(9,2) DEFAULT '0.00',
  `max_prod` decimal(9,2) DEFAULT '0.00',
  `target_prod` decimal(9,2) DEFAULT '0.00',
  `is_active` int DEFAULT '1',
  `company_id` int DEFAULT NULL,
  `branch_id` int DEFAULT NULL,
  `cnt_rate` decimal(10,5) DEFAULT NULL,
  PRIMARY KEY (`q_code_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Rollback:
-- DROP TABLE `link_prodquality_wagequality_mst`;
