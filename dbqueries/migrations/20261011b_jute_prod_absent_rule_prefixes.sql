-- =============================================================================
-- Long Absenteeism: excluded EB-code prefixes become a per-company setting
-- =============================================================================
-- Date: 2026-10-11 (owner decision, same day: "exclude C and R too", as a setting)
-- TARGET: dev3 AND sls. Additive, idempotent (checks information_schema first).
-- Run AFTER 20261011_jute_prod_absent_tables.sql.
--
-- jute_prod_absent_rule.exclude_prefixes: comma-separated single letters (e.g.
-- 'T,C,R'); workers whose emp_code starts with one of them are not listed as
-- long-absence candidates. NULL = the code default ('T,C,R',
-- long_absenteeism_common.RULE_DEFAULTS). The older exclude_t_codes flag stays in
-- the table but is no longer read (0 there no longer re-includes T codes).
;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_rule'
                      AND COLUMN_NAME = 'exclude_prefixes');
;
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE jute_prod_absent_rule ADD COLUMN exclude_prefixes VARCHAR(50) NULL AFTER exclude_t_codes',
    'SELECT ''exclude_prefixes already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;

-- VERIFICATION
--   SHOW COLUMNS FROM jute_prod_absent_rule LIKE 'exclude_prefixes';
-- ROLLBACK
--   ALTER TABLE jute_prod_absent_rule DROP COLUMN exclude_prefixes;
;
