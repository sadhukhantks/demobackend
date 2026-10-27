-- EJM fortnight wage report rows, mirroring legacy FoxPro E:\ejmhands\WG*\WAGEREPO.DBF.
-- Legacy codes are replaced by ERP ids:
--   FNEDATE -> period_id  (forthnight_period_mst.forthnight_id)
--   EMPNO   -> eb_id      (hrms_ed_personal_details.eb_id)
--   QCODE   -> quality_id
--   SHIFT   -> shift_id   (shift_mst.shift_id)
-- Every other DBF column is kept as-is.
--
-- Target DB: sls
--
-- ROLLBACK: DROP TABLE ejm_fne_wages_eff_data;

CREATE TABLE IF NOT EXISTS ejm_fne_wages_eff_data (
  fne_wages_eff_id INT      NOT NULL AUTO_INCREMENT,
  period_id   INT           DEFAULT NULL,
  eb_id       INT           DEFAULT NULL,
  quality_id  INT           DEFAULT NULL,
  shift_id    INT           DEFAULT NULL,
  detcode     VARCHAR(2)    DEFAULT NULL,
  details     VARCHAR(20)   DEFAULT NULL,
  deptcode    VARCHAR(2)    DEFAULT NULL,
  occucode    VARCHAR(2)    DEFAULT NULL,
  name        VARCHAR(25)   DEFAULT NULL,
  mcno        VARCHAR(6)    DEFAULT NULL,
  quantity    DECIMAL(10,2) DEFAULT NULL,
  eff         DECIMAL(6,2)  DEFAULT NULL,
  effcode     INT           DEFAULT NULL,
  tareff      DECIMAL(6,2)  DEFAULT NULL,
  acteff      DECIMAL(6,2)  DEFAULT NULL,
  line        VARCHAR(3)    DEFAULT NULL,
  mcs         VARCHAR(10)   DEFAULT NULL,
  PRIMARY KEY (fne_wages_eff_id),
  KEY idx_efwed_period_eb (period_id, eb_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
