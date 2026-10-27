-- Attendance incentive on the EJM wages collection row (legacy holget = 2,
-- "Main Payroll FN Attendance Incentive"). The Wages Data Process 'INC' arm
-- posts the fortnight's incentive here instead of misc_earn, so the payroll
-- feed keeps the figure apart from manual miscellaneous earnings.
-- The column already exists on sls, so the ADD is guarded (no-op there).
-- Target DB: dev3
-- Rollback (statements one per line, no semicolons here):
--   ALTER TABLE tbl_ejm_wages_data_collection DROP COLUMN atten_incentive

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE tbl_ejm_wages_data_collection ADD COLUMN atten_incentive DECIMAL(8,2) NULL AFTER prev_cuml_fpf_contribution',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_ejm_wages_data_collection' AND COLUMN_NAME = 'atten_incentive');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
