-- ejm_fne_eff_code_type.eff_pc holds an efficiency % or a production figure
-- (e.g. 16000), beyond DECIMAL(6,2)'s 9,999.99. sls already has DECIMAL(10,3);
-- this brings other tenants in line. Target: dev3.
ALTER TABLE ejm_fne_eff_code_type MODIFY eff_pc DECIMAL(10,3) NULL;

-- Rollback (fails if any value exceeds 9,999.99):
-- ALTER TABLE ejm_fne_eff_code_type MODIFY eff_pc DECIMAL(6,2) NULL;
