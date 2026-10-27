-- Holiday processing target tables (hrmsmasters/HolidayMaster -> Process).
--
-- hrms_holiday_transactions already exists in the `sls` tenant with exactly this
-- shape (branch_id instead of the legacy vowsls company_id, updated_by/
-- updated_date_time instead of created_by/auto_date_time_insert) — this script
-- brings the same table to tenants that lack it.
--
-- Run with the `run-migration` skill against the target tenant DB.

CREATE TABLE IF NOT EXISTS `hrms_holiday_transactions` (
  `holiday_tran_id` int NOT NULL AUTO_INCREMENT,
  `holiday_id` bigint DEFAULT NULL,
  `eb_id` bigint NOT NULL,
  `holiday_hours` double DEFAULT NULL,
  `eligibility_date` date DEFAULT NULL,
  `branch_id` bigint DEFAULT NULL,
  `updated_by` int DEFAULT NULL,
  `updated_date_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `is_active` tinyint DEFAULT '1',
  PRIMARY KEY (`holiday_tran_id`),
  KEY `idx_hht_holiday` (`holiday_id`, `is_active`),
  KEY `idx_hht_eb` (`eb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Rollback:
-- DROP TABLE `hrms_holiday_transactions`;
