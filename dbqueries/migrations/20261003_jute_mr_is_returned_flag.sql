ALTER TABLE jute_mr ADD COLUMN is_returned TINYINT(1) NOT NULL DEFAULT 0 AFTER status_id;

UPDATE jute_mr SET is_returned = 1, status_id = 1, approval_level = NULL WHERE status_id = 48;

-- ===========================================================================
-- Migration: Returned becomes a flag on jute_mr, not a status        (2026-10-03)
--
-- NOTE ON COMMENT PLACEMENT: the migration runner splits on the semicolon
-- character, so all commentary lives after the last statement and has none.
--
-- WHY (owner ruling 2026-10-03)
-- A returned lorry may leave material the mill inspected and kept. That
-- retained material (the MR lines) is stocked, issued, paid for and transferable
-- like any other MR, so the MR must keep its normal status. Returned is only a
-- marker for PO logic and reports, held in jute_mr.is_returned.
--
-- WHAT THIS DOES
-- 1. Adds jute_mr.is_returned (0 or 1, default 0).
-- 2. Converts every legacy status 48 MR to status 1 (Open) with is_returned = 1,
--    so it re-enters the normal flow. Nothing is approved or paid automatically.
--    The status_mst row 48 is left in place and is simply no longer used.
--
-- ORDER: run BEFORE 20261003_vw_jute_stock_outstanding_returned_in_stock.sql,
-- and on EVERY tenant before the backend ships (the MR header query and the PO
-- settlement SQL read jute_mr.is_returned). dev3 first. sls / amcl only after
-- owner approval (run-migration skill asks for the target DB).
-- After running on a tenant with returned MRs, open and re-save an affected PO
-- or press recompute so its settlement picks up the retained weight.
--
-- ROLLBACK
--   UPDATE jute_mr SET status_id = 48 WHERE is_returned = 1 AND status_id = 1
--   (only rows still Open, converted rows that moved on cannot be restored)
--   then ALTER TABLE jute_mr DROP COLUMN is_returned
-- ===========================================================================
