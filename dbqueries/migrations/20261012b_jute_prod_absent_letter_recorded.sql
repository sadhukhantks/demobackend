-- =============================================================================
-- Long Absenteeism: letters entered after the fact ("Record a letter already issued")
-- =============================================================================
-- Date: 2026-10-12 (owner decision C: back-dated entries for letters issued before the
-- case was tracked in VoWERP). TARGET: tenant DB only -- dev3 AND sls.
-- Additive, idempotent (checks information_schema first). Run AFTER
-- 20261011_jute_prod_absent_tables.sql.
--
-- jute_prod_absent_letter.recorded: 1 = the letter was recorded as already issued
-- (POST /api/longAbsenteeism/letter_record: issue / sent dates typed in, text merged from
-- the template at entry time), 0 = issued through VoWERP (letter_issue / batch).
-- The ORM maps it as a deferred column, so the code also runs before this migration
-- (only /letter_record needs it).
-- Runner note: each statement ends with a lone semicolon line for the pymysql runner.
;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_prod_absent_letter'
                      AND COLUMN_NAME = 'recorded');
;
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE jute_prod_absent_letter ADD COLUMN recorded TINYINT NOT NULL DEFAULT 0 AFTER forced',
    'SELECT ''recorded already present'' AS note');
;
PREPARE stmt FROM @ddl;
;
EXECUTE stmt;
;
DEALLOCATE PREPARE stmt;

-- VERIFICATION
--   SHOW COLUMNS FROM jute_prod_absent_letter LIKE 'recorded';
-- ROLLBACK
--   ALTER TABLE jute_prod_absent_letter DROP COLUMN recorded;
;
