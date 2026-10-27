-- Sync sls-only columns into dev3 (tenant-schema-check 2026-08-16, user-approved). Rerunnable, guarded, nullable adds
-- (transfer_mode matches sls exactly TINYINT NOT NULL DEFAULT 0 so existing rows stay valid).
-- Rollback (one per line, no semicolons here):
--   ALTER TABLE hrms_ed_resign_details DROP COLUMN reason_status
--   ALTER TABLE hrms_ed_official_details DROP COLUMN off_day
--   ALTER TABLE hrms_ed_pf DROP COLUMN date_of_exit
--   ALTER TABLE hrms_ed_esi DROP COLUMN date_of_exit
--   ALTER TABLE pay_scheme_master DROP COLUMN branch_id
--   ALTER TABLE jute_mr DROP COLUMN transfer_mode

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE jute_mr ADD COLUMN transfer_mode TINYINT NOT NULL DEFAULT 0', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_mr' AND COLUMN_NAME = 'transfer_mode');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE pay_scheme_master ADD COLUMN branch_id INT NULL', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'pay_scheme_master' AND COLUMN_NAME = 'branch_id');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE hrms_ed_esi ADD COLUMN date_of_exit DATE NULL', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'hrms_ed_esi' AND COLUMN_NAME = 'date_of_exit');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE hrms_ed_pf ADD COLUMN date_of_exit DATE NULL', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'hrms_ed_pf' AND COLUMN_NAME = 'date_of_exit');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE hrms_ed_official_details ADD COLUMN off_day INT NULL', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'hrms_ed_official_details' AND COLUMN_NAME = 'off_day');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE hrms_ed_resign_details ADD COLUMN reason_status INT NULL', 'SELECT 1')
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'hrms_ed_resign_details' AND COLUMN_NAME = 'reason_status');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
