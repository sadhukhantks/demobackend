-- EJM fortnight-wise efficiency codes, backing the "Ejm Eff Code for FNE" page
-- (dashboardportal/wages/ejm/efffneCodeEntry). One row per (fortnight,
-- efficiency code) with the efficiency % that fortnight is measured against.
-- Already created by hand on sls, so this file is the source of truth for every
-- other tenant.
--
--   forthnig_id -> forthnight_period_mst.forthnight_id
--   eff_code    -> the numeric efficiency code value, i.e.
--                  CAST(ejm_eff_code_type.effocode AS UNSIGNED)
--   status_id   -> approval lifecycle, shared by every row of a fortnight:
--                  21 Drafted, 1 Open, 20 Pending, 3 Approved, 4 Rejected,
--                  6 Cancelled. Single level -- there is no approval_level
--                  column, so add one before wiring a multi-level hierarchy.
--
-- ponytail: no FKs and no audit columns, matching the sibling ejm_* tables.
-- Target DB: sls (done), dev3
--
-- ROLLBACK: DROP TABLE ejm_fne_eff_code_type;

CREATE TABLE IF NOT EXISTS ejm_fne_eff_code_type (
  wages_eff_code_id INT NOT NULL AUTO_INCREMENT,
  forthnig_id       INT          DEFAULT NULL,
  eff_code          INT          DEFAULT NULL,
  eff_pc            DECIMAL(10,3) DEFAULT NULL,
  status_id         INT          DEFAULT NULL,
  PRIMARY KEY (wages_eff_code_id),
  -- The page always reads and writes a whole fortnight at a time.
  KEY idx_ejm_fne_eff_code_forthnight (forthnig_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
