-- =============================================================================
-- Migration: NJM wages data-collection tables (NJM Wages Data Preparation screen)
-- =============================================================================
-- Date: 2026-08-23   |   Target tenant DB: sls (as directed). dev3 has none of the
--   three and needs the same file before the screen can be exercised there.
-- DO NOT auto-run — apply via the run-migration skill after review.
--
-- WHAT: Creates the three tables the NJM "Updates" actions write to, so the
--   ported endpoints (src/hrms/njmWagesData.py) have somewhere to land:
--     tbl_njm_wages_data_collection      <- Cad/Vard/GWF, FA and Other/Adjs uploads
--     tbl_njm_cnt_wages_data_collection  <- Contractor upload
--     tbl_run_loom_line_hours            <- Line Hours upload
--
-- The DDL is copied VERBATIM from the legacy EMPMILL12 schema (AUTO_INCREMENT
-- seeds dropped). sls ALREADY carries the first two, column-for-column identical
-- to EMPMILL12 and empty, so IF NOT EXISTS makes those two a no-op there and only
-- tbl_run_loom_line_hours is actually created. dev3 has none of the three.
--
-- No branch_id column, deliberately: the sibling tbl_ejm_wages_data_collection
-- has none either. These rows are keyed by (date_from, date_to, eb_id,
-- payscheme_id) and a pay scheme already belongs to one branch. branch_id only
-- filters the SOURCE daily_attendance rows inside the endpoints.
-- Every table is IF NOT EXISTS, so re-running is a no-op.

CREATE TABLE IF NOT EXISTS `tbl_njm_wages_data_collection` (
  `njm_wages_data_id` int NOT NULL AUTO_INCREMENT,
  `date_from` date DEFAULT NULL,
  `date_to` date DEFAULT NULL,
  `hours_wkd_1` decimal(8,3) DEFAULT '0.000',
  `hours_wkd_2` decimal(8,2) DEFAULT '0.00',
  `esi_days` int DEFAULT '0',
  `el_days` int DEFAULT '0',
  `sl_days` int DEFAULT '0',
  `lay_off_hrs` decimal(8,3) DEFAULT '0.000',
  `piece_hours` decimal(8,3) DEFAULT '0.000',
  `canteen` int DEFAULT '0',
  `sunday_adv` int DEFAULT '0',
  `other_adv` int DEFAULT '0',
  `el_advance` int DEFAULT '0',
  `adv_ded` int DEFAULT '0',
  `installment_advance` int DEFAULT '0',
  `extra_hours_t` decimal(8,3) DEFAULT '0.000',
  `extra_hours_p` decimal(8,3) DEFAULT '0.000',
  `c_shift_days` int DEFAULT '0',
  `festival_hours` int DEFAULT '0',
  `arrear_plus` decimal(8,2) DEFAULT '0.00',
  `arrear_minus` decimal(8,2) DEFAULT '0.00',
  `piece_wages` decimal(8,2) DEFAULT '0.00',
  `piece_wages_inc` decimal(8,2) DEFAULT '0.00',
  `other_pay` int DEFAULT '0',
  `master_dept_code` varchar(10) DEFAULT NULL,
  `cl_days` int DEFAULT '0',
  `prod_balance` decimal(8,2) DEFAULT '0.00',
  `ul_days` int DEFAULT '0',
  `advance` int DEFAULT '0',
  `eb_id` int DEFAULT '0',
  `is_active` int DEFAULT '1',
  `dept_code` varchar(10) DEFAULT NULL,
  `act_prod_amount` decimal(8,2) DEFAULT '0.00',
  `loom_production` int DEFAULT '0',
  `update_for` varchar(10) DEFAULT NULL,
  `iltime_hrs` decimal(8,3) DEFAULT '0.000',
  `ilpiece_hrs` decimal(8,3) DEFAULT '0.000',
  `sardhelp_amt` decimal(12,6) DEFAULT '0.000000',
  `cadeligibility` int DEFAULT '0',
  `gwf_eleg` int DEFAULT '0',
  `iftu_eleg` int DEFAULT '0',
  `minus_bal` decimal(8,2) DEFAULT '0.00',
  `upd_type` int DEFAULT '1',
  `gwf_amount` int DEFAULT NULL,
  `iftu_amount` int DEFAULT NULL,
  `cad_amount` int DEFAULT NULL,
  `payscheme_id` int DEFAULT NULL,
  `qualcode` varchar(10) DEFAULT NULL,
  `production` decimal(10,3) DEFAULT NULL,
  `qual_rate` decimal(10,6) DEFAULT NULL,
  `lineno` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `linehrs` decimal(8,3) DEFAULT NULL,
  `advance_date` date DEFAULT NULL,
  PRIMARY KEY (`njm_wages_data_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `tbl_njm_cnt_wages_data_collection` (
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `eb_id` int DEFAULT NULL,
  `dept_id` int DEFAULT NULL,
  `desig_id` int DEFAULT NULL,
  `wrk_hours_reg` decimal(10,3) DEFAULT NULL,
  `wrk_hours_ot` decimal(10,3) DEFAULT NULL,
  `wrk_hours_adj` decimal(10,2) DEFAULT NULL,
  `rates` decimal(8,2) DEFAULT NULL,
  `prod_id` int DEFAULT NULL,
  `production` int DEFAULT NULL,
  `amount_reg` decimal(10,2) DEFAULT NULL,
  `amount_ot` decimal(10,2) DEFAULT NULL,
  `amount_adj` decimal(10,2) DEFAULT NULL,
  `canteen` int DEFAULT NULL,
  `advance` int DEFAULT NULL,
  `plus_amount` int DEFAULT NULL,
  `minus_amount` int DEFAULT NULL,
  `travel_allowance` int DEFAULT NULL,
  `updt_from` char(1) DEFAULT NULL,
  `loom_type` char(1) DEFAULT NULL,
  `time_piece` char(1) DEFAULT NULL,
  `reg_amount` int DEFAULT NULL,
  `prod_amount` decimal(10,2) DEFAULT NULL,
  `esi_days` int DEFAULT '0',
  `auto_id` int NOT NULL AUTO_INCREMENT,
  `is_active` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`auto_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `tbl_run_loom_line_hours` (
  `loom_line_hours_id` int NOT NULL AUTO_INCREMENT,
  `date_from` date DEFAULT NULL,
  `date_to` date DEFAULT NULL,
  `line_no` varchar(10) DEFAULT NULL,
  `sard_helper` varchar(1) DEFAULT NULL,
  `running_hours` decimal(8,2) DEFAULT NULL,
  `lost_hours` decimal(8,2) DEFAULT NULL,
  `wgroup` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `no_of_looms` int DEFAULT NULL,
  `line_no_id` int DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `pamount` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`loom_line_hours_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The period lookups every endpoint does. Legacy had no index at all and
-- EMPMILL12's copy is 217k rows and the deactivate-then-insert cycle scans on
-- exactly these columns. MySQL has no CREATE INDEX IF NOT EXISTS, so each is
-- guarded through information_schema to keep the whole file re-runnable.
SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_njm_wages_data_collection' AND index_name = 'idx_njm_wages_period');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_njm_wages_period` ON `tbl_njm_wages_data_collection` (date_from, date_to, payscheme_id)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_njm_cnt_wages_data_collection' AND index_name = 'idx_njm_cnt_wages_period');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_njm_cnt_wages_period` ON `tbl_njm_cnt_wages_data_collection` (start_date, end_date)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_run_loom_line_hours' AND index_name = 'idx_run_loom_line_period');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_run_loom_line_period` ON `tbl_run_loom_line_hours` (date_from, date_to, line_no)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

-- Rollback:
--   DROP TABLE IF EXISTS `tbl_njm_wages_data_collection`;
--   DROP TABLE IF EXISTS `tbl_njm_cnt_wages_data_collection`;
--   DROP TABLE IF EXISTS `tbl_run_loom_line_hours`;
