-- Holiday Master (hrmsmasters/HolidayMaster).
-- Branch-scoped holiday calendar; holiday_type 1=Occasional, 2=Weekly, 3=Monthly.
-- period_start_date/period_end_date bound the attendance period the holiday is
-- paid in (legacy vowsls.holiday_master used the same pair); spell_id replaces
-- the legacy free-text `spell` column and points at spell_mst.
-- Run with the `run-migration` skill against the target tenant DB.

CREATE TABLE IF NOT EXISTS `holiday_master` (
  `holiday_id` bigint NOT NULL AUTO_INCREMENT,
  `branch_id` int DEFAULT NULL,
  `holiday` varchar(255) DEFAULT NULL,
  `holiday_date` date DEFAULT NULL,
  `holiday_type` int DEFAULT NULL COMMENT '1.occasional 2.Weekly 3. Monthly',
  `period_start_date` date DEFAULT NULL,
  `period_end_date` date DEFAULT NULL,
  `spell_id` int DEFAULT NULL,
  `status` int DEFAULT 1 COMMENT '1=Open, 3=Approved',
  `updated_by` int DEFAULT NULL,
  `updated_date_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`holiday_id`),
  KEY `idx_holiday_branch_date` (`branch_id`, `holiday_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Rollback:
-- DROP TABLE `holiday_master`;
