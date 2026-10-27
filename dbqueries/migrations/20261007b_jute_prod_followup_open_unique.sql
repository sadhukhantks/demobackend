-- =============================================================================
-- Migration: Low Performer Follow-up -- one OPEN follow-up per person, DB-enforced
-- =============================================================================
-- Date: 2026-10-07 (code review of the feature, finding 3)
-- TARGET: tenant DB only -- dev3 first, then sls (owner approved this feature's DDL
-- on both). Run AFTER 20261007_jute_prod_low_performer_followup.sql.
--
-- The router already refuses a second OPEN (status = 1, active = 1) follow-up of
-- the same (co_id, branch_id, eb_id, dept_code) with a SELECT guard, but two
-- requests arriving together can both pass that SELECT. This MySQL 8 (8.0.13+)
-- functional unique key closes the race: its first key part is 1 only for an
-- open active row and NULL otherwise, and NULLs never collide in a unique index,
-- so closed / soft-deleted history rows may repeat freely while a second open
-- row (INSERT or a reopen UPDATE) fails with error 1062. The router maps that
-- IntegrityError to the guard's 409 message (low_performer_followup.py,
-- _is_open_unique_violation / _duplicate_conflict).
--
-- Pre-check (must return no rows, else the ALTER fails with 1062):
--   SELECT co_id, branch_id, eb_id, dept_code, COUNT(*) FROM jute_prod_followup_person
--    WHERE status = 1 AND active = 1
--    GROUP BY co_id, branch_id, eb_id, dept_code HAVING COUNT(*) > 1
--
-- Runner note: tools/run_sql_migration.py strips comments before splitting on
-- semicolons. Not idempotent -- a second run fails with 1061 (duplicate key name),
-- which changes nothing.
;

ALTER TABLE jute_prod_followup_person
    ADD UNIQUE KEY uq_jpfp_open (
        (CASE WHEN status = 1 AND active = 1 THEN 1 END),
        co_id, branch_id, eb_id, dept_code
    );

-- =============================================================================
-- VERIFICATION
--   SHOW INDEX FROM jute_prod_followup_person WHERE Key_name = 'uq_jpfp_open'
--   (5 rows: Seq_in_index 1 has Expression = case when ... , then co_id,
--    branch_id, eb_id, dept_code; Non_unique = 0)
--
-- ROLLBACK
--   ALTER TABLE jute_prod_followup_person DROP INDEX uq_jpfp_open
-- =============================================================================
;
