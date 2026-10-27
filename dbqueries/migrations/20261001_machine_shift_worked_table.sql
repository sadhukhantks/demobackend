-- =============================================================================
-- Migration: Machines worked from attendance — jute_prod_machine_shift_worked
-- =============================================================================
-- Date: 2026-10-01   Spec: docs/superpowers/specs/2026-09-30-hands-norms-v2-machine-groups.md
-- (§11.1). Owner rulings of 2026-10-01 after the first sls machine groups were keyed.
-- TARGET: tenant DB only — dev3 first, then sls ONLY when the owner names the DB
-- (sls holds production data). Needs 20260930_hands_norms_v2_machine_groups.sql
-- on the tenant first (the report code that fills this table reads the v2 columns).
-- Apply BEFORE deploying the code: every Hands Report request rebuilds its window
-- into this table and then reads it, so the report fails with "table doesn't
-- exist" until this file has run.
--
-- WHAT. One row per (branch, date, shift letter, machine) that shows up in ANY
-- machine-keyed entry of the day — production (weaving, doffs, spinning, beaming,
-- finishing, spreader, drawing, winding, bale press), waste, the man-machine
-- attendance mark, stoppage hours and breakdowns — with everything the Hands
-- Report needs to decide whether the machine WORKED that shift:
--
--   workers_marked     distinct workers marked on the machine in attendance
--                      (daily_ebmc_attendance through a live, non-rejected
--                      daily_attendance header)
--   stopped_all_shift  GREATEST(att_stoppage_hours, stoppage_hours,
--                      breakdown_min / 60) >= shift_hours
--   worked             workers_marked > 0 AND stopped_all_shift = 0 — the ONLY
--                      thing that makes a machine count for a machine-based (M)
--                      hands norm. A production, waste, stoppage or breakdown
--                      entry alone no longer does
--   shift_hours        spell_mst.working_hours of the branch's full-shift spell
--                      of the letter (spell_code exactly A / B / C / G, through
--                      shift_mst.branch_id), 8 when the branch has none
--   prod_kg, prod_cuts production the entries booked on the machine-shift (the
--                      numerator of a production (P) norm, summed worked or not)
--   winding_kg         the Winding leg's share of prod_kg
--   sources            the entries the machine-shift was seen in
--
-- The table is a CACHE, not a book of record: the report deletes and re-inserts
-- the rows of its own window (1st of the From Date's month .. To Date) for its
-- co_id / branch_id on every open, in one transaction
-- (hands_report.rebuild_machine_shift_worked), so a row is as fresh as the last
-- open (synced_at) and nothing else ever writes here. Other reports may read it.
--
-- House DDL style of 20260930_hands_norms_v2_machine_groups.sql. No FK to
-- machine_mst: the rebuild is a bulk INSERT ... SELECT of rows that already join
-- it, and an FK would only add a lock on the master per inserted row. No
-- created_* / updated_* columns — synced_at is the only timestamp and it is set
-- by the rebuild. uq_msw has no co_id because a branch belongs to one company.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own. Re-running is harmless
-- (CREATE TABLE IF NOT EXISTS).
;

-- 1. Machine-shift worked table (§11.1)
;
CREATE TABLE IF NOT EXISTS jute_prod_machine_shift_worked (
  machine_shift_worked_id INT           NOT NULL AUTO_INCREMENT,
  co_id                   INT           NOT NULL,
  branch_id               INT           NOT NULL,
  tran_date               DATE          NOT NULL,
  shift                   CHAR(1)       NOT NULL,               -- A | B | C | G (shift letter)
  machine_id              INT           NOT NULL,
  workers_marked          INT           NOT NULL DEFAULT 0,     -- distinct workers marked on the machine in attendance
  att_stoppage_hours      DECIMAL(6,2)  NOT NULL DEFAULT 0,     -- max mc_stoppage_hours on those marks
  stoppage_hours          DECIMAL(6,2)  NOT NULL DEFAULT 0,     -- jute_prod_stoppage_hours
  breakdown_min           DECIMAL(8,2)  NOT NULL DEFAULT 0,     -- jute_mech_breakdown
  shift_hours             DECIMAL(5,2)  NULL,                   -- full-shift spell hours of the letter, 8 when none
  prod_kg                 DECIMAL(14,3) NULL,
  prod_cuts               DECIMAL(12,3) NULL,
  winding_kg              DECIMAL(14,3) NULL,
  sources                 VARCHAR(160)  NULL,                   -- e.g. 'Attendance,Stoppage,Weaving'
  stopped_all_shift       TINYINT       NOT NULL DEFAULT 0,     -- GREATEST(att_stoppage_hours, stoppage_hours, breakdown_min / 60) >= shift_hours
  worked                  TINYINT       NOT NULL DEFAULT 0,     -- workers_marked > 0 AND stopped_all_shift = 0
  synced_at               DATETIME      NOT NULL,
  PRIMARY KEY (machine_shift_worked_id),
  UNIQUE KEY uq_msw (branch_id, tran_date, shift, machine_id),
  KEY idx_msw_co_branch_date (co_id, branch_id, tran_date),
  KEY idx_msw_machine (machine_id, tran_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW CREATE TABLE jute_prod_machine_shift_worked
--     expected the 18 columns above, PRIMARY KEY machine_shift_worked_id,
--     UNIQUE uq_msw (branch_id, tran_date, shift, machine_id),
--     KEY idx_msw_co_branch_date, KEY idx_msw_machine
--
--   SELECT COUNT(*) FROM jute_prod_machine_shift_worked
--     expected 0 right after the migration. The first Hands Report open of a
--     branch fills its window (or call hands_report.rebuild_machine_shift_worked
--     for the first live run)
--
--   After the first rebuild of a window:
--   SELECT shift, COUNT(*) AS machine_shifts, SUM(worked) AS worked,
--          SUM(workers_marked > 0 AND stopped_all_shift = 1) AS stopped_all_shift,
--          SUM(workers_marked = 0) AS no_worker_marked, MAX(synced_at) AS synced_at
--     FROM jute_prod_machine_shift_worked
--    WHERE co_id = <co> AND branch_id = <branch> AND tran_date = '<date>'
--    GROUP BY shift
--     expected worked + stopped_all_shift + no_worker_marked = machine_shifts on
--     every line, and worked = 1 only where workers_marked > 0
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DROP TABLE IF EXISTS jute_prod_machine_shift_worked
--   The table is a rebuildable cache, so dropping it loses nothing. The report
--   code of this change fails without it, so roll the code back first.
-- =============================================================================
;
