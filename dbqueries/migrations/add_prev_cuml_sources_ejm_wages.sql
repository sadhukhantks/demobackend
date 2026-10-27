-- EJM Wages Data Process, "Cumulative Data Process" step: previous-year
-- cumulatives per hand, summed from cumulative_data_table into the
-- prev_cuml_* columns of tbl_ejm_wages_data_collection (the 'CUM' arm in
-- src/hrms/ejmWagesData.py).
--   * cumulative_data_table gains the two per-period figures the financial-year
--     sums need that the legacy CUMLDATA.DBF never carried: bonus_gross
--     (pay component 207 C_BON_EARN) and fpf_contribution (232 C_FPF_CONT).
--     ejm_cumulative_transfer posts them; rows loaded from the DBF stay 0 there
--     until the transfer is re-run for their periods.
--   * prev_cuml_w_days widens to 3 decimals: (working + night hours) / 8.
--   * prev_cuml_bonus_gross was varchar(100) but holds money. The wages table
--     is empty in dev3 and sls, so the retype is lossless.
-- Target: dev3 (sls needs the same before the step is run there).

ALTER TABLE cumulative_data_table
  ADD COLUMN bonus_gross decimal(10,2) DEFAULT '0.00' AFTER pf_contibution,
  ADD COLUMN fpf_contribution decimal(10,2) DEFAULT '0.00' AFTER bonus_gross;

ALTER TABLE tbl_ejm_wages_data_collection
  MODIFY COLUMN prev_cuml_w_days decimal(8,3) DEFAULT '0.000',
  MODIFY COLUMN prev_cuml_bonus_gross decimal(12,2) DEFAULT '0.00';

-- Rollback:
-- ALTER TABLE cumulative_data_table DROP COLUMN bonus_gross, DROP COLUMN fpf_contribution;
-- ALTER TABLE tbl_ejm_wages_data_collection
--   MODIFY COLUMN prev_cuml_w_days decimal(8,2) DEFAULT '0.00',
--   MODIFY COLUMN prev_cuml_bonus_gross varchar(100) DEFAULT '0';
