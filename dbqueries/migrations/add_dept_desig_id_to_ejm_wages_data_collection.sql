-- Worked department / designation ids on the EJM wages collection row.
-- The Wages Data Process 'ATT' arm now groups attendance by
-- daily_attendance.worked_department_id (a sub_dept_mst id) / worked_designation_id
-- and stamps them here; the list joins sub_dept_mst / designation_mst on them for
-- the Dept / Occu codes.
-- The columns already exist on sls (INT), so each ADD is guarded (no-op there).
-- Target DB: dev3
-- Rollback (statements one per line, no semicolons here):
--   ALTER TABLE tbl_ejm_wages_data_collection DROP COLUMN desig_id
--   ALTER TABLE tbl_ejm_wages_data_collection DROP COLUMN dept_id

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE tbl_ejm_wages_data_collection ADD COLUMN dept_id INT NULL AFTER eb_id',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_ejm_wages_data_collection' AND COLUMN_NAME = 'dept_id');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE tbl_ejm_wages_data_collection ADD COLUMN desig_id INT NULL AFTER dept_id',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_ejm_wages_data_collection' AND COLUMN_NAME = 'desig_id');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
