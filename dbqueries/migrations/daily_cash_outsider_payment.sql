-- EJM Cash/Outsider Daily Payment build (legacy Loan_adv_model.php holget==14)
-- and the generic Cash Hands process both write here; the Daily Cash Outsider
-- Payment / Cash Hands reports read it.
-- Structure mirrors sls.daily_cash_outsider_payment (which already exists —
-- this guarded CREATE is a no-op there; keep for tenants that lack it).
-- Target DB: sls

CREATE TABLE IF NOT EXISTS daily_cash_outsider_payment (
  eb_id         INT NULL,
  pay_date      DATE NULL,
  shift         VARCHAR(100) NULL,
  dept_id       INT NULL,
  desig_id      INT NULL,
  subloca_id    INT NULL,
  working_hours DECIMAL(7,3) NULL,
  rate          DECIMAL(8,2) NULL,
  oth_rate      DECIMAL(8,2) NULL,
  amount        DECIMAL(8,3) NULL,
  branch_id     INT NULL,
  cont_id       INT NULL,
  is_active     INT NULL,
  KEY idx_dcop_pay_date (pay_date, branch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
