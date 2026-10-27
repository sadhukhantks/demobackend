-- Widen tbl_ejm_wages_data_collection.update_for varchar(5) -> varchar(10) so the
-- Wages Manual Entries page can stamp its rows 'Manual' (6 chars).
--
-- Target DB: sls (and dev3)
--
-- ROLLBACK: ALTER TABLE tbl_ejm_wages_data_collection MODIFY update_for varchar(5) DEFAULT NULL

ALTER TABLE tbl_ejm_wages_data_collection MODIFY update_for varchar(10) DEFAULT NULL;
