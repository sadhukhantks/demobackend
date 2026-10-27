-- =============================================================================
-- Migration: range-read indexes for the Loom Modification Impact report
-- =============================================================================
-- Date: 2026-09-28   Phase 2 of
-- docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md (§4.5, D11)
-- TARGET: tenant DBs that run weaving — dev3 first, then sls OFF-SHIFT with the
-- owner naming the DB on the day (online DDL, ALGORITHM=INPLACE, LOCK=NONE, but
-- the pair is 2.24M + 1.06M rows on sls so allow a few minutes of build time).
--
-- WHY. weaving_range_loomday_sql (src/juteProduction/loom_modification_impact_query.py)
-- reads jute_prod_weaving_daily for a LIST OF LOOMS over a DATE RANGE. None of the
-- eight existing indexes has machine_id and tran_date adjacent, so the optimizer
-- ranges on co_id + tran_date (every loom of the company for the span, machine_id
-- filtered afterwards) — 3.5 s vs 0.6 s hinted on sls for 40 looms x 270 days.
-- Until idx_wd_co_machine_date exists the query builder forces idx_wd_jugar_chain
-- (decided per request from SHOW INDEX by weaving_range_index_hint), after it the
-- hint is dropped automatically — no code change is needed with this migration.
--
-- daily_ebmc_attendance has nothing leading with mc_id, so the weaver-hours
-- derived table (att) must scan the tenant's whole attendance for the span
-- (3.7 s of a 7.2 s statement for 40 looms). With (mc_id, daily_atten_id,
-- is_active) the att leg and the section-F hands leg can start from the loom list
-- and join back to daily_attendance by PK.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks
-- that start with a comment marker, so every comment block ends with a bare
-- semicolon line and contains no semicolon of its own. Each ALTER is its own
-- chunk. Re-running after success fails with "Duplicate key name" — harmless,
-- check the VERIFICATION queries instead.
--
-- ROLLBACK:
--   ALTER TABLE jute_prod_weaving_daily DROP INDEX idx_wd_co_machine_date
--   ALTER TABLE daily_ebmc_attendance DROP INDEX idx_dea_mc_atten_active
;

ALTER TABLE jute_prod_weaving_daily
    ADD INDEX idx_wd_co_machine_date (co_id, machine_id, tran_date),
    ALGORITHM=INPLACE, LOCK=NONE;

ALTER TABLE daily_ebmc_attendance
    ADD INDEX idx_dea_mc_atten_active (mc_id, daily_atten_id, is_active),
    ALGORITHM=INPLACE, LOCK=NONE;

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW INDEX FROM jute_prod_weaving_daily WHERE Key_name = 'idx_wd_co_machine_date'
--   SHOW INDEX FROM daily_ebmc_attendance WHERE Key_name = 'idx_dea_mc_atten_active'
--
--   EXPLAIN of the range read should now show key = idx_wd_co_machine_date on wd
--   without any FORCE INDEX, and meta.index_hint of the report response is null.
-- =============================================================================
;
