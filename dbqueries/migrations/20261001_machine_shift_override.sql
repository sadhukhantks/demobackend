-- =============================================================================
-- Migration: Manual additions and exclusions of worked machines —
--            jute_prod_machine_shift_override + jute_prod_machine_shift_worked.manual_action
-- =============================================================================
-- Date: 2026-10-01   Spec: docs/superpowers/specs/2026-09-30-hands-norms-v2-machine-groups.md
-- (§12.1). Owner rulings of 2026-10-01: attendance may miss a machine or mark a
-- wrong one, so a role with EDIT access to the Hands Report menu may add a missed
-- machine-shift or exclude a wrongly marked one, with a reason.
-- TARGET: tenant DB only — dev3 first, then sls ONLY when the owner names the DB
-- (sls holds production data). Needs 20261001_machine_shift_worked_table.sql on
-- the tenant first (step 2 alters the table that file creates).
-- Apply BEFORE deploying the code: every Hands Report request rebuilds its window
-- of jute_prod_machine_shift_worked and then applies the active overrides of
-- that window in the same transaction, so the report fails with "Unknown column
-- manual_action" / "table doesn't exist" until this file has run.
--
-- WHAT.
--   jute_prod_machine_shift_override   the BOOK OF RECORD of the manual changes:
--       one row per change of one machine-shift (branch, date, shift letter,
--       machine) — action ADD (count the machine though attendance has no
--       worker on it, or the stoppage books say stopped all shift) or EXCLUDE
--       (do not count it though a worker is marked), the reason, who and when.
--       active = 1 is the change in force. An undo, or a newer change of the
--       same machine-shift, sets active = 0 — rows are never deleted. One
--       active row per machine-shift is kept in code (the save retires the
--       previous one in the same transaction), and the readers take the newest
--       active row should two saves ever race
--   jute_prod_machine_shift_worked.manual_action   ADD | EXCLUDE | NULL, copied
--       from the active override at every rebuild, next to worked which then
--       reads: ADD override OR (workers_marked > 0 AND stopped_all_shift = 0
--       AND no EXCLUDE override). An ADD of a machine-shift that no entry
--       names gets a row of its own with sources NULL
--
-- Unlike jute_prod_machine_shift_worked (a cache the report rebuilds), the
-- override table is never rebuilt — nothing but the two override endpoints
-- (hands_report_machine_override / hands_report_machine_override_undo) writes it.
--
-- House DDL style of 20260930_hands_norms_v2_machine_groups.sql: co_id/branch_id
-- NOT NULL, active TINYINT DEFAULT 1, updated_by / updated_date_time ON UPDATE.
-- No created_* columns (DB triggers). No FK to machine_mst (the save checks the
-- machines are active machines of the branch) and no DB UNIQUE on the
-- machine-shift (retired rows share it).
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own. Re-running after success fails on
-- the ALTER with "Duplicate column name" — harmless, check VERIFICATION instead.
;

-- 1. Manual override of a machine-shift (§12.1)
;
CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_override (
  machine_shift_override_id INT          NOT NULL AUTO_INCREMENT,
  co_id                     INT          NOT NULL,
  branch_id                 INT          NOT NULL,
  tran_date                 DATE         NOT NULL,
  shift                     CHAR(1)      NOT NULL,              -- A | B | C | G (shift letter)
  machine_id                INT          NOT NULL,
  action                    VARCHAR(10)  NOT NULL,              -- ADD | EXCLUDE
  reason                    VARCHAR(255) NOT NULL,
  active                    TINYINT      NOT NULL DEFAULT 1,    -- undo = 0, one active row per machine-shift (code)
  updated_by                INT          NULL,
  updated_date_time         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (machine_shift_override_id),
  KEY idx_mso_window (co_id, branch_id, tran_date, active),
  KEY idx_mso_machine (machine_id, tran_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. The override in force, copied onto the cache row at every rebuild (§12.1)
;
ALTER TABLE jute_prod_machine_shift_worked
  ADD COLUMN manual_action VARCHAR(10) NULL                     -- ADD | EXCLUDE, copied from the active override at rebuild
  AFTER worked;

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW CREATE TABLE jute_prod_machine_shift_override
--     expected the 11 columns above, PRIMARY KEY machine_shift_override_id,
--     KEY idx_mso_window (co_id, branch_id, tran_date, active),
--     KEY idx_mso_machine (machine_id, tran_date)
--
--   SHOW COLUMNS FROM jute_prod_machine_shift_worked LIKE 'manual_action'
--     expected one row, varchar(10), NULL allowed, placed right after worked
--
--   SELECT COUNT(*) FROM jute_prod_machine_shift_override
--     expected 0 right after the migration
--
--   After the first manual changes and a Hands Report open of the window:
--   SELECT w.manual_action, COUNT(*) AS machine_shifts, SUM(w.worked) AS worked
--     FROM jute_prod_machine_shift_worked w
--    WHERE w.co_id = <co> AND w.branch_id = <branch> AND w.tran_date = '<date>'
--    GROUP BY w.manual_action
--     expected worked = machine_shifts on the ADD line and worked = 0 on the
--     EXCLUDE line
--   SELECT tran_date, shift, machine_id, COUNT(*) AS active_rows
--     FROM jute_prod_machine_shift_override
--    WHERE active = 1
--    GROUP BY tran_date, shift, machine_id, branch_id
--   HAVING COUNT(*) > 1
--     expected no rows (one active override per machine-shift)
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   ALTER TABLE jute_prod_machine_shift_worked DROP COLUMN manual_action
--   DROP TABLE IF EXISTS jute_prod_machine_shift_override
--   The override table is a book of record: dropping it loses every manual
--   addition / exclusion and its reason, so export it first if any were keyed.
--   The report code of this change fails without the column and the table, so
--   roll the code back first.
-- =============================================================================
;
