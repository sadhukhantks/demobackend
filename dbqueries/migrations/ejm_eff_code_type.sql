-- EJM efficiency-code lookup, backing the "Efficiency Code Entry (EJM)" page
-- (dashboardportal/wages/ejm/effCodeEntry). One row per legacy FoxPro
-- OCCUMAST.EFFCODE value, carrying the efficiency % the occupation is measured
-- against. Already created by hand on sls, so this file is the source of truth
-- for every other tenant.
--
-- ponytail: no co_id/branch_id/is_active -- plain tenant-wide lookup, scoped by
-- the tenant DB itself. Add scoping when a tenant needs per-company codes.
-- Target DB: sls (done), dev3
--
-- ROLLBACK: DROP TABLE ejm_eff_code_type;

CREATE TABLE IF NOT EXISTS ejm_eff_code_type (
  eff_code_type_id INT NOT NULL AUTO_INCREMENT,
  effocode         VARCHAR(10)  DEFAULT NULL,
  code_details     VARCHAR(100) DEFAULT NULL,
  eff_pc           DECIMAL(6,2) DEFAULT NULL,
  PRIMARY KEY (eff_code_type_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
