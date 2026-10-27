-- ejm_eff_code_type.eff_prod: what eff_pc holds, E = efficiency %, P = production
-- figure (e.g. 19 Spreader = kg per 8 h). Already on sls (added by hand), needed on dev3.
ALTER TABLE ejm_eff_code_type ADD COLUMN eff_prod CHAR(1) DEFAULT NULL AFTER eff_pc;

-- Same values as sls: production codes 15-19, efficiency codes 2-14
UPDATE ejm_eff_code_type SET eff_prod = 'P' WHERE effocode IN ('15', '16', '17', '18', '19');
UPDATE ejm_eff_code_type SET eff_prod = 'E' WHERE effocode IN ('2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14');

-- Rollback: ALTER TABLE ejm_eff_code_type DROP COLUMN eff_prod
