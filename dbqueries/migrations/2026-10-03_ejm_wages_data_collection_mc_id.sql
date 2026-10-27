-- EJM Wages Data Process: attendance of BEAMING (dept 20) and FINISHING (dept 22)
-- is collected machine-wise, and the machine (machine_mst.machine_id) goes here.
-- Target: sls.
ALTER TABLE tbl_ejm_wages_data_collection ADD COLUMN mc_id INT NULL AFTER mc_nos;

-- Rollback:
-- ALTER TABLE tbl_ejm_wages_data_collection DROP COLUMN mc_id;
