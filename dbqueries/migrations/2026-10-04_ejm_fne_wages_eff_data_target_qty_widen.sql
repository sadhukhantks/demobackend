-- ejm_fne_wages_eff_data.target_qty (the 100%-efficiency quantity of a row) must
-- hold group-code and loom-mistry totals of up to a few million (weaving yards,
-- spinning kg), beyond DECIMAL(8,3)'s 99,999.999. Target: sls.
ALTER TABLE ejm_fne_wages_eff_data MODIFY target_qty DECIMAL(14,3) NULL;

-- Rollback (fails if any value exceeds 99,999.999):
-- ALTER TABLE ejm_fne_wages_eff_data MODIFY target_qty DECIMAL(8,3) NULL;
