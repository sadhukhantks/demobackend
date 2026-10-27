-- Weaving opening-jugar trace: where does each row's opening come from?
-- SELECT only. MySQL 8 (window functions). Written 2026-09-15 for TKT-000150 / TKT-000156.
--
-- Rule being traced: opening of a row = closing of the immediately previous row for the
-- SAME loom (and quality), where "previous" = latest earlier row by EFFECTIVE START TIME.
-- LAG() over that window IS the "max earlier row" - no aggregate needed. Shifts / days
-- with no entry are skipped automatically because the window only sees rows that exist.
--
-- Effective start time of a row:
--     eff_start = TIMESTAMP(tran_date - (is_overnight * @overnight_prev_day) days, starting_time)
--   @overnight_prev_day = 1 : an overnight spell dated D started on D-1 (C dated 02-Sep =
--                             night 01-Sep 22:00 -> 02-Sep 06:00). Chain: B(01) -> C(02) -> A(02).
--   @overnight_prev_day = 0 : an overnight spell dated D started on D (C dated 01-Sep =
--                             night 01-Sep 22:00 -> 02-Sep 06:00). Chain: B(01) -> C(01) -> A(02).
--   Same physical night, different date label. RULING 2026-09-16: the crew dates C by the
--   night it starts (C dated 14-Sep keyed 15-Sep 07:40, A dated 15-Sep keyed 15-Sep 16:09),
--   so 0 is correct and is what the code implements. 1 is kept only for comparison.
--
-- What the query has to cover (each one bites if missed):
--   1. co_id filter and active = 1 (soft-deleted rows must not feed the chain).
--   2. Partition per loom AND quality (current doctrine). Switch to per loom only by
--      dropping weaving_quality_id from both PARTITION BY clauses (ruling pending).
--   3. Order by eff_start, then the legacy rank CASE (tiebreak for spells sharing a start
--      time, e.g. br87 A2/B1 both 14:00), then weaving_daily_id. This is what the code does.
--   4. Lookback before @from so the first rows in the range still find their predecessor.
--   5. Branch filter applied AFTER the window (chain is per company + loom, not branch).
--   6. Predecessor with NULL closing counts as 0, no predecessor = 0 (code semantics).
--   7. Gap exposed so legacy / long-idle predecessors are visible (@maxgap cap).
--   8. Beam change resets opening to 0 (jute_prod_weaving_beam_map) - no rows on br29 yet.

SET @co := 2;
SET @branch := 29;          -- NULL = whole company
SET @from := '2026-09-09';
SET @to := '2026-09-11';
SET @lookback := 400;       -- days before @from scanned for predecessors (400 reaches the Jan-2026 legacy rows)
SET @maxgap := 30;          -- predecessor older than this (days) -> capped opening shown as 0
SET @overnight_prev_day := 0;  -- 0 = overnight spell dated by the day it STARTS (RULING 2026-09-16, what the code does); 1 = rejected 2026-09-15 variant

WITH base AS (
    SELECT jpwd.weaving_daily_id, jpwd.co_id, jpwd.branch_id, jpwd.machine_id, m.mech_code,
           jpwd.weaving_quality_id, jpwd.tran_date, jpwd.spell_id, sm.spell_code, sm.starting_time,
           sm.is_overnight,
           TIMESTAMP(DATE_SUB(jpwd.tran_date, INTERVAL COALESCE(sm.is_overnight, 0) * @overnight_prev_day DAY),
                     sm.starting_time)                                                 AS eff_start,
           jpwd.cuts, jpwd.close_jugar, jpwd.open_jugar AS stored_open, jpwd.less_production,
           q.no_of_jugar_per_cut AS jc, q.finished_length AS fl,
           -- the CURRENT code's rank (hardcoded) - only here to show what Sync would write
           CASE sm.spell_code WHEN 'A1' THEN 1 WHEN 'B1' THEN 2 WHEN 'A2' THEN 3
                              WHEN 'B2' THEN 4 WHEN 'C' THEN 5 ELSE 99 END AS code_rank
    FROM jute_prod_weaving_daily jpwd
    JOIN spell_mst sm ON sm.spell_id = jpwd.spell_id
    LEFT JOIN machine_mst m ON m.machine_id = jpwd.machine_id
    LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = jpwd.weaving_quality_id
    WHERE jpwd.co_id = @co
      AND jpwd.active = 1
      AND jpwd.tran_date BETWEEN DATE_SUB(@from, INTERVAL @lookback DAY) AND @to
),
chain AS (
    SELECT b.*,
           LAG(b.close_jugar)      OVER w_ok   AS prev_close,
           LAG(b.tran_date)        OVER w_ok   AS prev_date,
           LAG(b.spell_code)       OVER w_ok   AS prev_spell,
           LAG(b.eff_start)        OVER w_ok   AS prev_eff_start,
           LAG(b.weaving_daily_id) OVER w_ok   AS prev_id,
           LAG(b.close_jugar)      OVER w_code AS code_prev_close,
           LAG(b.tran_date)        OVER w_code AS code_prev_date,
           LAG(b.spell_code)       OVER w_code AS code_prev_spell
    FROM base b
    WINDOW w_ok   AS (PARTITION BY b.co_id, b.machine_id, b.weaving_quality_id
                      ORDER BY b.eff_start, b.code_rank, b.weaving_daily_id),
           w_code AS (PARTITION BY b.co_id, b.machine_id, b.weaving_quality_id
                      ORDER BY b.tran_date, b.code_rank, b.weaving_daily_id)
)
SELECT weaving_daily_id, mech_code, weaving_quality_id AS qual, tran_date, spell_code AS shift,
       eff_start,
       cuts, close_jugar AS closing,
       stored_open,
       CASE WHEN weaving_quality_id IS NULL THEN 0 ELSE COALESCE(prev_close, 0) END AS correct_open,  -- NULL quality never chains
       CONCAT(prev_spell, ' ', prev_date)                        AS correct_from,
       TIMESTAMPDIFF(HOUR, prev_eff_start, eff_start)            AS gap_hours,
       CASE WHEN prev_eff_start IS NULL OR TIMESTAMPDIFF(DAY, prev_eff_start, eff_start) > @maxgap
            THEN 0 ELSE COALESCE(prev_close, 0) END              AS correct_open_capped,
       COALESCE(code_prev_close, 0)                              AS code_open,
       CONCAT(code_prev_spell, ' ', code_prev_date)              AS code_from,
       CASE WHEN stored_open <> COALESCE(prev_close, 0)      THEN 'WRONG'
            WHEN stored_open <> COALESCE(code_prev_close, 0) THEN 'OK-lucky'
            ELSE 'OK' END                                        AS verdict,
       cuts * jc + close_jugar - COALESCE(prev_close, 0) - COALESCE(less_production, 0)          AS jugar_correct,
       ROUND((cuts * jc + close_jugar - COALESCE(prev_close, 0) - COALESCE(less_production, 0))
             * fl / NULLIF(jc, 0), 3)                                                          AS production_yds_correct
FROM chain
WHERE tran_date BETWEEN @from AND @to
  AND (@branch IS NULL OR branch_id = @branch)
ORDER BY machine_id, eff_start, weaving_daily_id;
