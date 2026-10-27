-- Pay register processing / view: index the per-period lookups.
-- pay_employee_payroll (24M rows in sls) and pay_employee_payperiod had only a
-- PRIMARY key, so the pre-process DELETE by PAYPERIOD_ID + PAYSCHEME_ID and the
-- view-page salary join were full table scans (minutes on the remote server,
-- longer than the 30s Next proxy timeout).
-- Safe on dev3 (empty) and sls. ALGORITHM=INPLACE keeps the table online.
-- Rollback: DROP INDEX idx_pep_period_scheme / idx_pepp_period_scheme.

ALTER TABLE pay_employee_payroll
  ADD INDEX idx_pep_period_scheme (PAYPERIOD_ID, PAYSCHEME_ID),
  ALGORITHM=INPLACE, LOCK=NONE;

ALTER TABLE pay_employee_payperiod
  ADD INDEX idx_pepp_period_scheme (PAY_PERIOD_ID, PAY_SCHEME_ID),
  ALGORITHM=INPLACE, LOCK=NONE;
