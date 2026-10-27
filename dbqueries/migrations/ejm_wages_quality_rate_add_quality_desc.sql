-- ejm_wages_quality_rate: add the quality description (legacy QUALRATE.DBF DESC, C(15)).
-- Target: sls (the only tenant with this table). Backfilled from
-- E:\ejmhands\WG150426\QUALRATE.XLS by (dept, QCODE) after this runs.

ALTER TABLE ejm_wages_quality_rate
    ADD COLUMN quality_desc VARCHAR(100) DEFAULT NULL AFTER quality_code;

-- Rollback:
-- ALTER TABLE ejm_wages_quality_rate DROP COLUMN quality_desc;
