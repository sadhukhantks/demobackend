-- =============================================================================
-- Jute PO settlement (closing) -- schema
-- =============================================================================
-- Date: 2026-09-25
-- Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md (sections 3, 11)
-- Revision 2 (2026-09-25 evening): the rule table carries tolerance_lorry_fraction
-- (fraction of one lorry, default 0.50 = half a lorry) instead of the original
-- weight_tolerance_pct. dev3, migrated in the first shape, was brought to this
-- shape by 2026-09-25b_jute_po_settle_rule_lorry_fraction.sql -- do NOT run
-- this base script on dev3 again.
-- Target: tenants not yet migrated (sls only with the owner's explicit OK).
--
-- Adds the four close columns to jute_po, the status log and the per-company
-- settlement rule table, makes sure status_mst has 48 RETURNED (sls lacks it,
-- return_mr needs it -- section 11.3), marks every pre-existing CLOSED PO as a
-- MIGRATED close, and retro-logs the 2026-09-25 branch-29 close-out from its
-- backup table when that table exists in the target DB (sls only -- harmless
-- elsewhere, done through a prepared statement because plain MySQL has no
-- conditional DDL/DML).
--
-- Runner: the pymysql runner splits this file on the semicolon character and
-- skips chunks that start with a comment marker, so comment blocks end with a
-- lone semicolon line and contain no semicolons themselves.
;
ALTER TABLE jute_po
  ADD COLUMN closed_date  DATETIME     NULL,
  ADD COLUMN closed_by    INT          NULL,
  ADD COLUMN close_type   VARCHAR(10)  NULL,
  ADD COLUMN close_remark VARCHAR(500) NULL;

CREATE TABLE IF NOT EXISTS jute_po_status_log (
  log_id        INT AUTO_INCREMENT PRIMARY KEY,
  jute_po_id    INT NOT NULL,
  old_status_id INT NULL,
  new_status_id INT NOT NULL,
  close_type    VARCHAR(10) NULL,
  source        VARCHAR(10) NOT NULL,
  trigger_ref   VARCHAR(100) NULL,
  remark        VARCHAR(500) NULL,
  user_id       INT NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_po_status_log_po (jute_po_id, log_id)
);

CREATE TABLE IF NOT EXISTS jute_po_settle_rule (
  co_id                    INT PRIMARY KEY,
  lorry_rule_enabled       TINYINT NOT NULL DEFAULT 0,
  tolerance_lorry_fraction DECIMAL(4,2) NOT NULL DEFAULT 0.50,
  stale_days               INT NOT NULL DEFAULT 30,
  updated_by               INT NULL,
  updated_date_time        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- MR status 48 RETURNED (lorry sent back without settlement) -- dev3 has it, sls does not.
;
INSERT INTO status_mst (status_id, status_name)
SELECT 48, 'RETURNED' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM status_mst WHERE status_id = 48);

-- Backfill: every existing CLOSED PO is a migrated close.
;
UPDATE jute_po SET close_type = 'MIGRATED' WHERE status_id = 5 AND close_type IS NULL;

-- Retro-log the 2026-09-25 branch-29 close-out (close_migrated_jute_pos_before_2026_07_01.sql)
-- as source MIGRATION when its backup table exists in this DB. Idempotent:
-- a PO already carrying a MIGRATION row is not logged twice.
;
SET @has_backup = (
  SELECT COUNT(*) FROM information_schema.tables
  WHERE table_schema = DATABASE() AND table_name = 'jute_po_status_backup_20260925'
);

SET @backfill_sql = IF(@has_backup > 0,
  'INSERT INTO jute_po_status_log (jute_po_id, old_status_id, new_status_id, close_type, source, trigger_ref, remark, user_id, created_at)
   SELECT b.jute_po_id, b.old_status_id, 5, ''MIGRATED'', ''MIGRATION'',
          ''close_migrated_jute_pos_before_2026_07_01'',
          ''Legacy close-out of migrated POs dated before 2026-07-01 (branch 29)'',
          NULL, b.backed_up_at
   FROM jute_po_status_backup_20260925 b
   WHERE NOT EXISTS (SELECT 1 FROM jute_po_status_log l
                     WHERE l.jute_po_id = b.jute_po_id AND l.source = ''MIGRATION'')',
  'SELECT 1');

PREPARE jute_po_backfill_stmt FROM @backfill_sql;

EXECUTE jute_po_backfill_stmt;

DEALLOCATE PREPARE jute_po_backfill_stmt;

-- VERIFICATION
;
SELECT COUNT(*) AS migrated_closes FROM jute_po WHERE status_id = 5 AND close_type = 'MIGRATED';

SELECT COUNT(*) AS migration_log_rows FROM jute_po_status_log WHERE source = 'MIGRATION';

SELECT status_id, status_name FROM status_mst WHERE status_id = 48;

-- =============================================================================
-- ROLLBACK:
-- (the status_mst 48 row is left in place)
-- DROP TABLE IF EXISTS jute_po_settle_rule
-- DROP TABLE IF EXISTS jute_po_status_log
-- ALTER TABLE jute_po
--   DROP COLUMN close_remark,
--   DROP COLUMN close_type,
--   DROP COLUMN closed_by,
--   DROP COLUMN closed_date
-- (run each rollback statement separately, terminated with its own semicolon)
-- =============================================================================
;
