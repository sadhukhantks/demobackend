-- =============================================================================
-- Migration: NJM wages masters + advance-balance view
-- =============================================================================
-- Date: 2026-08-23   |   Target tenant DB: sls (as directed). dev3 needs this
--   file too, after njm_wages_data_tables.sql.
-- DO NOT auto-run -- apply via the run-migration skill after review.
--
-- WHAT: the reference tables the remaining NJM checklists and wages arms read,
--   copied from the legacy EMPMILL12 schema:
--     tbl_wages_quality_master          quality/rate card (Production Checklist)
--     tbl_njm_food_allowance            per-employee DA/FA rates (FA Details)
--     tbl_company_advance               advance headers (Advance Details)
--     tbl_wages_data_payschm_map_table  wages column -> pay component map
--   plus vw_company_advance_balance, rewritten against vowerp3 table names.
--
-- DATA: only the two code-keyed tables are seeded here, and only by the paired
--   loader (dbqueries/seed_njm_wages_masters.py):
--     tbl_wages_quality_master   keyed by q_code (a string)  -> portable
--     tbl_njm_food_allowance     keyed by eb_no (emp_code)   -> portable
--   tbl_company_advance and tbl_wages_data_payschm_map_table are left EMPTY on
--   purpose. They key on legacy eb_id / payscheme_id / component_id, which are
--   a different id space in sls (see sls._map_pay_eb, which is many-to-one).
--   Copying them without going through that map would attach advances and pay
--   components to the wrong people. Seed advances through the eb map separately;
--   the payscheme map has its own seeder that translates scheme and component
--   by CODE: dbqueries/seed_njm_wages_payschm_map.py (applied to sls 2026-09-09).
--
-- Every table is IF NOT EXISTS and the view is CREATE OR REPLACE, so the file
-- is re-runnable.

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

CREATE TABLE IF NOT EXISTS `tbl_njm_food_allowance` (
  `food_allow_id` int NOT NULL AUTO_INCREMENT,
  `eb_no` varchar(10) DEFAULT NULL,
  `da_rate` decimal(12,6) DEFAULT NULL,
  `fa_rate` decimal(12,6) DEFAULT NULL,
  PRIMARY KEY (`food_allow_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `tbl_company_advance` (
  `tbl_comp_adv_id` int NOT NULL AUTO_INCREMENT,
  `company_id` int DEFAULT NULL,
  `eb_id` int DEFAULT NULL,
  `advance_date` date DEFAULT NULL,
  `advance_amount` int DEFAULT NULL,
  `installment_amount` int DEFAULT NULL,
  `adj_trans_amount` int DEFAULT NULL,
  PRIMARY KEY (`tbl_comp_adv_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `tbl_wages_data_payschm_map_table` (
  `wages_data_payschm_map_id` int NOT NULL AUTO_INCREMENT,
  `payscheme_id` int DEFAULT NULL,
  `data_column_no` varchar(50) DEFAULT NULL,
  `payscheme_component_id` int DEFAULT NULL,
  PRIMARY KEY (`wages_data_payschm_map_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Advance balance per employee and advance date. Ported from EMPMILL12's
-- vw_company_advance_balance with the legacy table names swapped for the
-- vowerp3 ones (tbl_hrms_ed_* -> hrms_ed_*, is_active -> active).
--
-- Deductions come from the 'IADV' arm of tbl_njm_wages_data_collection: a
-- running total per (employee, advance date) ordered by wage period, so
-- balamt is the advance less everything deducted up to and including that
-- period. On the LAST period for an advance the adjustment amount comes off
-- too, which is what settles the advance to zero.
CREATE OR REPLACE VIEW `vw_company_advance_balance` AS
SELECT
    od.emp_code                AS emp_code,
    esi.esi_no                 AS esi_no,
    CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ',
           TRIM(IFNULL(pd.middle_name, '')), ' ',
           TRIM(IFNULL(pd.last_name, ''))) AS empname,
    adv.eb_id                  AS eb_id,
    adv.company_id             AS company_id,
    adv.advance_date           AS advance_date,
    adv.advance_amount         AS advance_amount,
    adv.installment_amount     AS installment_amount,
    adv.adj_amount             AS adj_amount,
    adv.date_from              AS date_from,
    adv.ded_amount             AS ded_amount,
    adv.cumulative_ded         AS cumulative_ded,
    adv.balamt                 AS balamt
FROM (
    SELECT
        tca.eb_id, tca.company_id, tca.advance_date, tca.advance_amount,
        tca.installment_amount,
        IFNULL(tca.adj_trans_amount, 0) AS adj_amount,
        d.date_from, d.ded_amount, d.cumulative_ded,
        CASE WHEN d.date_from = d.last_date_from
             THEN tca.advance_amount - d.cumulative_ded - IFNULL(tca.adj_trans_amount, 0)
             ELSE tca.advance_amount - d.cumulative_ded
        END AS balamt
    FROM tbl_company_advance tca
    LEFT JOIN (
        SELECT
            w.eb_id, w.advance_date, w.date_from,
            SUM(w.advance) AS ded_amount,
            SUM(SUM(w.advance)) OVER (
                PARTITION BY w.eb_id, w.advance_date ORDER BY w.date_from
            ) AS cumulative_ded,
            MAX(w.date_from) OVER (
                PARTITION BY w.eb_id, w.advance_date
            ) AS last_date_from
        FROM tbl_njm_wages_data_collection w
        WHERE w.update_for = 'IADV' AND w.is_active = 1
        GROUP BY w.eb_id, w.advance_date, w.date_from
    ) d ON d.eb_id = tca.eb_id AND d.advance_date = tca.advance_date
) adv
LEFT JOIN hrms_ed_official_details od ON od.eb_id = adv.eb_id AND od.active = 1
LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = adv.eb_id
LEFT JOIN hrms_ed_esi esi ON esi.eb_id = adv.eb_id AND esi.active = 1
WHERE adv.advance_amount > 0;

-- Lookup indexes for the checklists that read these.
SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_wages_quality_master'
      AND index_name = 'idx_wages_quality_code');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_wages_quality_code` ON `tbl_wages_quality_master` (q_code)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_njm_food_allowance'
      AND index_name = 'idx_njm_food_allowance_eb');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_njm_food_allowance_eb` ON `tbl_njm_food_allowance` (eb_no)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

SET @x := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'tbl_company_advance'
      AND index_name = 'idx_company_advance_eb');
SET @s := IF(@x > 0, 'SELECT 1',
    'CREATE INDEX `idx_company_advance_eb` ON `tbl_company_advance` (eb_id, advance_date)');
PREPARE st FROM @s;
EXECUTE st;
DEALLOCATE PREPARE st;

-- Rollback:
--   DROP VIEW IF EXISTS vw_company_advance_balance
--   DROP TABLE IF EXISTS tbl_wages_quality_master
--   DROP TABLE IF EXISTS tbl_njm_food_allowance
--   DROP TABLE IF EXISTS tbl_company_advance
--   DROP TABLE IF EXISTS tbl_wages_data_payschm_map_table
