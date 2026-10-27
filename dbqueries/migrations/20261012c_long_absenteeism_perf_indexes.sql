-- =============================================================================
-- Long Absenteeism: performance indexes (owner complaint 2026-10-12: slow loads on sls)
-- =============================================================================
-- TARGET: dev3 AND sls, with the owner's explicit OK for sls (production DDL).
-- All three are plain secondary indexes: InnoDB adds them ONLINE (INPLACE, no table
-- copy, reads and writes continue; only a brief metadata lock at start and end).
-- Measured on sls before: daily_attendance ~1.2M rows, leave_transactions 5.5k rows
-- with NO index at all, leave_tran_details 77k rows with NO index at all.
--
-- 1. daily_attendance (branch_id, eb_id, attendance_date, is_active, status_id): the
--    candidates query needs "last attendance date per worker of the branch" over the
--    whole history (no look-back limit, owner ruling). The index must COVER the filter
--    columns: a plain (branch_id, eb_id, attendance_date) index made the query 4x SLOWER
--    on sls (875k row lookups through it); the covering one lets MySQL answer the leg
--    from the index alone (measured on sls 2026-10-12: 2.2 s -> 1.7 s for the whole
--    query, the leg itself 0.4 s).
-- 2. leave_transactions (eb_id, status, leave_to_date): the per-case / per-candidate
--    leave pick and the leave legs of the candidates query.
-- 3. leave_tran_details (ltran_id, leave_date): the approved-leave-day leg of the
--    candidates query (join to its header).
-- Idempotent: each ADD INDEX is skipped when the index name already exists.
;
SET @i1 := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'daily_attendance' AND INDEX_NAME = 'idx_da_branch_eb_cov');
;
SET @ddl1 := IF(@i1 = 0, 'ALTER TABLE daily_attendance ADD INDEX idx_da_branch_eb_cov (branch_id, eb_id, attendance_date, is_active, status_id), ALGORITHM=INPLACE, LOCK=NONE', 'SELECT ''idx_da_branch_eb_cov present'' AS note');
;
PREPARE s1 FROM @ddl1;
;
EXECUTE s1;
;
DEALLOCATE PREPARE s1;
;
SET @i2 := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'leave_transactions' AND INDEX_NAME = 'idx_lt_eb_status_to');
;
SET @ddl2 := IF(@i2 = 0, 'ALTER TABLE leave_transactions ADD INDEX idx_lt_eb_status_to (eb_id, status, leave_to_date), ALGORITHM=INPLACE, LOCK=NONE', 'SELECT ''idx_lt_eb_status_to present'' AS note');
;
PREPARE s2 FROM @ddl2;
;
EXECUTE s2;
;
DEALLOCATE PREPARE s2;
;
SET @i3 := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'leave_tran_details' AND INDEX_NAME = 'idx_ltd_ltran_date');
;
SET @ddl3 := IF(@i3 = 0, 'ALTER TABLE leave_tran_details ADD INDEX idx_ltd_ltran_date (ltran_id, leave_date), ALGORITHM=INPLACE, LOCK=NONE', 'SELECT ''idx_ltd_ltran_date present'' AS note');
;
PREPARE s3 FROM @ddl3;
;
EXECUTE s3;
;
DEALLOCATE PREPARE s3;

-- VERIFICATION
--   SHOW INDEX FROM daily_attendance WHERE Key_name = 'idx_da_branch_eb_cov';
--   SHOW INDEX FROM leave_transactions WHERE Key_name = 'idx_lt_eb_status_to';
--   SHOW INDEX FROM leave_tran_details WHERE Key_name = 'idx_ltd_ltran_date';
-- ROLLBACK
--   ALTER TABLE daily_attendance DROP INDEX idx_da_branch_eb_cov;
--   ALTER TABLE leave_transactions DROP INDEX idx_lt_eb_status_to;
--   ALTER TABLE leave_tran_details DROP INDEX idx_ltd_ltran_date;
;
