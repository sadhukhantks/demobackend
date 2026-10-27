-- EJM fortnightly wages quality target, backing the "Ejm Wages Fne Target" page
-- (dashboardportal/wages/ejm/fnewagesEntry). One row per (department, wages
-- quality) with the efficiency % that combination is targeted at.
-- Already created by hand on sls, so this file is the source of truth for every
-- other tenant.
--
--   quality_it         -> tbl_wages_quality_master.q_code_id (the wages rate card)
--   dept_id            -> dept_mst.dept_id
--   fne_qual_traget_id -> surrogate row id (legacy misspelling kept so the
--                         tenant's hand-made table matches)
--
-- The tenant's table has no key of any kind, so src/hrms/ejmWagesFneQualTarget.py
-- assigns fne_qual_traget_id (MAX+1) and enforces one target per
-- (dept_id, quality_it) in code. Fresh tenants get the keys below instead.
--
-- Target DB: sls (table exists, keyless), dev3
--
-- ROLLBACK: DROP TABLE ejm_wages_fne_qual_target;

CREATE TABLE IF NOT EXISTS ejm_wages_fne_qual_target (
  fne_qual_traget_id INT          NOT NULL,
  quality_it         INT          DEFAULT NULL,
  dept_id            INT          DEFAULT NULL,
  eff_pc             DECIMAL(6,2) DEFAULT NULL,
  period_id          INT          DEFAULT NULL,
  status_id          INT          DEFAULT NULL,
  -- link_prodquality_wagequality_mst.process; set only when the quality is
  -- linked under more than one process in the department, else NULL
  process            VARCHAR(100) DEFAULT NULL,
  PRIMARY KEY (fne_qual_traget_id),
  UNIQUE KEY uq_ewfqt_dept_quality (period_id, dept_id, quality_it, process)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Existing tenants (sls already has all three by hand):
-- ALTER TABLE ejm_wages_fne_qual_target ADD COLUMN process VARCHAR(100) DEFAULT NULL;
