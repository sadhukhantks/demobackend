-- =============================================================================
-- Long Absenteeism: link a case to the HRMS leave module (TKT-000223)
-- =============================================================================
-- Date: 2026-10-12 (ticket TKT-000223, sls: "make leave dynamic ... persons who are
-- already on leave should automatically get the data from there").
-- TARGET: dev3 AND sls. Additive, idempotent (checks information_schema per column).
-- Run AFTER 20261011_jute_prod_absent_tables.sql.
--
-- jute_prod_absent_case gains five nullable columns:
--   leave_type_id        hrms_leave_types_mst.leave_type_id picked on /verify or /exit
--                        (esi_stl_result / exit_reason = 'LEAVE'), or copied from the
--                        approved leave found in the leave module.
--   leave_type_name      snapshot of the type's description (code when blank) when set.
--   leave_from, leave_to the approved leave span found in leave_transactions
--                        (status 3, leave_to_date > last_working_date), refreshed on
--                        /list and /get like rejoined_seen_date.
--   leave_transaction_id leave_transactions.leave_transaction_id of that leave;
--                        NULL = no module leave (a manual pick keeps only the type).
--   leave_parked         1 = the case was PARKED automatically because that leave
--                        covers today (owner ruling 12.10.2026: no manual verification,
--                        the leave module is the source); cleared when the case resumes
--                        (leave ended / withdrawn, or a user resumes it by hand).
-- The legacy codes ESI / STL / MATERNITY in esi_stl_result / exit_reason stay readable.
;
SET @c1 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_type_id');
;
SET @ddl := IF(@c1 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_type_id BIGINT NULL AFTER expected_return_date',
    'SELECT ''leave_type_id already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;
;
SET @c2 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_type_name');
;
SET @ddl := IF(@c2 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_type_name VARCHAR(100) NULL AFTER leave_type_id',
    'SELECT ''leave_type_name already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;
;
SET @c3 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_from');
;
SET @ddl := IF(@c3 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_from DATE NULL AFTER leave_type_name',
    'SELECT ''leave_from already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;
;
SET @c4 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_to');
;
SET @ddl := IF(@c4 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_to DATE NULL AFTER leave_from',
    'SELECT ''leave_to already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;
;
SET @c5 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_transaction_id');
;
SET @ddl := IF(@c5 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_transaction_id BIGINT NULL AFTER leave_to',
    'SELECT ''leave_transaction_id already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;
;
SET @c6 := (SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_case'
              AND COLUMN_NAME = 'leave_parked');
;
SET @ddl := IF(@c6 = 0,
    'ALTER TABLE jute_prod_absent_case ADD COLUMN leave_parked TINYINT NOT NULL DEFAULT 0 AFTER leave_transaction_id',
    'SELECT ''leave_parked already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;

-- VERIFICATION
--   SHOW COLUMNS FROM jute_prod_absent_case LIKE 'leave%';   -- 6 rows
-- ROLLBACK
--   ALTER TABLE jute_prod_absent_case
--     DROP COLUMN leave_parked, DROP COLUMN leave_transaction_id, DROP COLUMN leave_to,
--     DROP COLUMN leave_from, DROP COLUMN leave_type_name, DROP COLUMN leave_type_id;
;
