-- pay_components_custom: add the indexes the payroll read/write path needs.
--
-- The table holds ~9M rows (485 MB) with ONLY a PRIMARY KEY. Every payroll query
-- against it therefore full-scans:
--
--   * soft_delete_pcc_for_period()  (payRoll.pay_roll_upload / pay_roll_data_save)
--       UPDATE ... WHERE (PAY_PERIOD_ID = ? OR (FROM_DATE = ? AND TO_DATE = ?))
--                    AND EMPLOYEEID IN (...) AND STATUS = 1
--     EXPLAIN: type=index, key=PRIMARY, rows=9,035,147. As an UPDATE that takes
--     row locks on the WHOLE table (observed trx_rows_locked = 9,435,673), so a
--     second payroll write for any scheme/period blocks until it commits and then
--     dies with "Lock wait timeout exceeded" (errno 1205).
--
--   * get_custom_component_values()  (payRegister._run_payroll_processing)
--       SELECT ... WHERE STATUS = 1 AND (PAY_PERIOD_ID = ? OR (FROM_DATE = ? AND TO_DATE = ?))
--
-- The OR prevents a single composite index from covering the soft-delete, so index
-- the two branches separately and let index_merge / the EMPLOYEEID range do the work.
-- ALGORITHM=INPLACE, LOCK=NONE keeps concurrent payroll reads/writes running during the
-- rebuild (MySQL 8 online DDL). If the server ever can't satisfy it, the statement ERRORS
-- rather than silently falling back to a table-copy that would block writes for minutes.
ALTER TABLE pay_components_custom
  ADD INDEX idx_pcc_emp_period (EMPLOYEEID, FROM_DATE, TO_DATE, STATUS),
  ADD INDEX idx_pcc_period     (FROM_DATE, TO_DATE, STATUS),
  ADD INDEX idx_pcc_payperiod  (PAY_PERIOD_ID, STATUS),
  ALGORITHM=INPLACE, LOCK=NONE;

-- Rollback
-- ALTER TABLE pay_components_custom
--   DROP INDEX idx_pcc_emp_period,
--   DROP INDEX idx_pcc_period,
--   DROP INDEX idx_pcc_payperiod;
