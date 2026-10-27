-- Holiday / Attendance-Incentive eligibility per employee.
-- Ported from the legacy EMPMILL12.tbl_holiday_att_inc_eligibility; the id
-- columns are BIGINT here instead of DOUBLE so eb_id matches
-- hrms_ed_personal_details.eb_id (bigint) in the tenant DBs.
--
-- Rollback: DROP TABLE `tbl_holiday_att_inc_eligibility`;

CREATE TABLE IF NOT EXISTS `tbl_holiday_att_inc_eligibility` (
  `hl_att_inc_id` bigint NOT NULL AUTO_INCREMENT,
  `eb_id` bigint NOT NULL,
  `holiday_eligibility` char(1) NOT NULL DEFAULT 'N',
  `att_incn_eligibility` char(1) NOT NULL DEFAULT 'N',
  `fn_att_inc_rate` decimal(8,2) NOT NULL DEFAULT '0.00',
  `mn_att_inc_rate` decimal(8,2) NOT NULL DEFAULT '0.00',
  `is_active` int NOT NULL DEFAULT '1',
  `updated_by` int DEFAULT NULL,
  `updated_date_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `status` int DEFAULT NULL,
  `fy_year` int DEFAULT NULL,
  PRIMARY KEY (`hl_att_inc_id`),
  KEY `idx_hl_att_inc_eb` (`eb_id`, `is_active`)
);
