-- Pay register create/process was a full scan of PK-only tables on sls:
--   pay_components_custom   10M rows  -> get_custom_component_values (9s) and the
--                                        _require_uploaded_inputs guard, which
--                                        runs a COUNT(*) per scheme component =
--                                        11 full scans = 59s, twice per create,
--                                        so the Next proxy 504'd before finishing.
--   pay_employee_structure  1.67M rows -> get_employee_pay_structure by PAYSCHEME_ID.
-- Both queries match on period_id OR (from_date, to_date), so one index per branch;
-- COMPONENT_ID trails so the same two indexes serve the guard's per-component count.
-- ALGORITHM=INPLACE, LOCK=NONE keeps the tables online. Safe on dev3 (small) and sls.
-- Rollback: DROP INDEX idx_pcc_dates / idx_pcc_period / idx_pes_scheme_status.

ALTER TABLE pay_components_custom
  ADD INDEX idx_pcc_dates (FROM_DATE, TO_DATE, STATUS, COMPONENT_ID),
  ADD INDEX idx_pcc_period (PAY_PERIOD_ID, STATUS, COMPONENT_ID),
  ALGORITHM=INPLACE, LOCK=NONE;

ALTER TABLE pay_employee_structure
  ADD INDEX idx_pes_scheme_status (PAYSCHEME_ID, STATUS, EMPLOYEEID),
  ALGORITHM=INPLACE, LOCK=NONE;
