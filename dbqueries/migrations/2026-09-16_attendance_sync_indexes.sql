-- Migration: 2026-09-16_attendance_sync_indexes.sql
-- Weaving Sync / Process read attendance for ONE day and spell (no_worker warning,
-- operator candidates, attendance hours in the day-slice freeze). The rewritten
-- queries start from daily_attendance on the date, then walk to the machine
-- marks. These two indexes make both hops index seeks on the 1.2M-row tables.
-- Measured on sls before any index: no_worker 4.2 s -> 0.5 s from the rewrite alone,
-- and (review 2026-09-16, proxy via use_invisible_indexes) with a daily_atten_id index:
-- no_worker 584 -> 48 ms, candidates 537 -> 64 ms, day-slice 676 -> 122 ms, drift 489 -> 44 ms.
-- MUST ship to every weaving tenant WITH the code: the STRAIGHT_JOIN hint without this
-- index pins a full hash scan of daily_ebmc_attendance (~0.5 s) on four queries, one of
-- them (the day-slice) on every weaving page read.
-- sls note: daily_ebmc_attendance already carries FK_DAILYATTENID_idx (daily_atten_id)
-- but it is INVISIBLE there. The composite below supersedes it (same leading column,
-- covers is_active + mc_id). Leave the invisible one alone, it backs the FK.
--
-- EXECUTOR CONSTRAINT: no semicolon in comment prose, header terminated by the lone
-- semicolon below.
--
-- ROLLBACK:
-- ALTER TABLE daily_attendance DROP INDEX idx_da_date_spell_active
-- ALTER TABLE daily_ebmc_attendance DROP INDEX idx_dea_atten_active_mc
;

ALTER TABLE daily_attendance
    ADD INDEX idx_da_date_spell_active (attendance_date, spell_id, is_active);

ALTER TABLE daily_ebmc_attendance
    ADD INDEX idx_dea_atten_active_mc (daily_atten_id, is_active, mc_id);
