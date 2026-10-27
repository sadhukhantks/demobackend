-- Copy the legacy loom-type masters from EMPMILL12 into sls (structure + data):
--   tbl_loom_type                  (8 rows: 1 = S4 Loom, 7 = Sacking, ...)
--   tbl_loom_master_other_details  (629 rows: machine -> loom_type_id, sardar/helper line)
-- mechine_id values are identical to sls.machine_mst.machine_id (verified: all 629
-- resolve — co-1 looms to branch 4, co-2 looms/frames to branch 29).
-- Used by the S4 Incentive Report (loom_type_id = 1), as in the legacy data portal.
--
-- Target DB: sls only (same server as EMPMILL12).
--
-- ROLLBACK: DROP TABLE tbl_loom_master_other_details; DROP TABLE tbl_loom_type;

CREATE TABLE IF NOT EXISTS tbl_loom_type LIKE EMPMILL12.tbl_loom_type;
INSERT IGNORE INTO tbl_loom_type SELECT * FROM EMPMILL12.tbl_loom_type;

CREATE TABLE IF NOT EXISTS tbl_loom_master_other_details LIKE EMPMILL12.tbl_loom_master_other_details;
INSERT IGNORE INTO tbl_loom_master_other_details SELECT * FROM EMPMILL12.tbl_loom_master_other_details;
