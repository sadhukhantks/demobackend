-- EJM Cash/Outsider Daily Payment preparation (legacy oattprdprocess) writes
-- here; the "Daily Outsider Production(EJM)" report reads it.
-- Ported from EMPMILL12.tbl_daily_cash_outsider_payment_production; the name
-- (typo `targt_production` included) is kept so legacy-shaped readers match.
-- Additions over legacy: branch_id (multi-branch tenant), updated_by, and
-- decimal money/production columns instead of legacy's INTs.
-- Target DB: sls

CREATE TABLE IF NOT EXISTS tbl_daily_cash_outsider_payment_production (
  cash_outsider_prod_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  branch_id        INT NOT NULL,
  eb_id            INT NULL,
  dept_id          INT NULL,
  desig_id         INT NULL,
  production       DECIMAL(12,3) NULL,
  is_active        TINYINT NOT NULL DEFAULT 1,
  rate_type        INT NULL,
  prod_date        DATE NOT NULL,
  prod_shift       VARCHAR(2) NULL,
  targt_production DECIMAL(12,3) NULL,
  prod_amount      DECIMAL(12,2) NULL,
  prod_rate        DECIMAL(10,2) NULL,
  updated_by       INT NULL,
  updated_date_time TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_cash_outsider_prod_date (prod_date, branch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
