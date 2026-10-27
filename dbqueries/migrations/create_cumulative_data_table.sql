-- EJM-Cumulative Data Transfer (legacy holiday_att_incn_process.php hol_get=20):
-- one row per employee per pay period with the period's hours / PF / earnings,
-- posted from pay_employee_payroll by /hrmsMasters/ejm_cumulative_transfer.
-- Structure mirrors sls.cumulative_data_table (already exists there — the
-- guarded CREATE is a no-op; keep for tenants that lack it).
-- Target DB: dev3

CREATE TABLE IF NOT EXISTS cumulative_data_table (
  cumulative_id      INT NOT NULL AUTO_INCREMENT,
  date_from          DATE NULL,
  date_to            VARCHAR(100) NULL,
  eb_id              INT NULL,
  working_hours      DECIMAL(8,3) DEFAULT '0.000',
  night_shift_hours  DECIMAL(7,3) DEFAULT '0.000',
  fest_hours         DECIMAL(3,0) DEFAULT '0',
  stl_days           INT DEFAULT '0',
  pf_gross           DECIMAL(8,2) NULL,
  pf_contibution     DECIMAL(8,2) DEFAULT '0.00',
  bf_amount          DECIMAL(5,2) DEFAULT '0.00',
  cf_amount          DECIMAL(5,2) DEFAULT '0.00',
  fest_amount        DECIMAL(8,2) DEFAULT '0.00',
  puja_adv_bal       DECIMAL(8,2) DEFAULT '0.00',
  co_loan_bal        DECIMAL(8,2) DEFAULT '0.00',
  total_earnings     DECIMAL(8,2) DEFAULT '0.00',
  PRIMARY KEY (cumulative_id),
  KEY idx_cdt_period_eb (date_from, eb_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Rollback:
-- DROP TABLE IF EXISTS cumulative_data_table;
