-- Copy the legacy weaving efficiency-incentive slab (EMPMILL12.tbl_incentive_data,
-- 41 rows: company 2 / branch 29 / department 21 = sls co 2 / branch 29 /
-- WEAVING(EJM) — legacy and sls ids are identical for co 2) into sls.
-- Run AFTER s4_incentive_report_menu_and_table.sql.
--
-- Target DB: sls only (same server as EMPMILL12).
--
-- ROLLBACK: DELETE FROM tbl_incentive_data WHERE incentive_data_id <= 41;

INSERT IGNORE INTO tbl_incentive_data
  (incentive_data_id, department_id, rate, rate_inc, total_inc, company_id, branch_id, is_active, eff_inc)
SELECT incentive_data_id, department_id, rate, rate_inc, total_inc, company_id, branch_id, is_active, eff_inc
FROM EMPMILL12.tbl_incentive_data;
