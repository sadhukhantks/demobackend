-- Weekly drift check (READ-ONLY): stored jute_prod_weaving_daily.open_jugar
-- vs the vw_weaving_daily LAG ordering (the reference oracle formula,
-- duplicated verbatim from backfill_weaving_open_jugar.sql).
--
-- Expect ZERO rows. Any row returned means a writer missed a chain repair or
-- a bulk import bypassed the app writers - re-run
-- dbqueries/migrations/backfill_weaving_open_jugar.sql to heal, then
-- investigate the writer path for the mismatched (co, machine, quality) day.
--
-- Suggested cadence: weekly per tenant (dev3, sls), and always right after
-- any raw-SQL data import touching jute_prod_weaving_daily.
--
-- EXECUTOR CONSTRAINT: no semicolon in comment prose, header terminated by the
-- lone semicolon below so split-on-semicolon runners skip it cleanly.
;

SELECT wd.weaving_daily_id, wd.co_id, wd.machine_id, wd.weaving_quality_id,
       wd.tran_date, wd.spell_id,
       wd.open_jugar AS stored_open_jugar,
       l.oj          AS lag_open_jugar
FROM jute_prod_weaving_daily wd
JOIN (
    SELECT x.weaving_daily_id,
           CASE WHEN x.weaving_quality_id IS NULL THEN 0
                WHEN LAG(x.tran_date) OVER w_chain IS NULL
                  OR DATEDIFF(x.tran_date, LAG(x.tran_date) OVER w_chain) > 45 THEN 0  -- keep in sync with src/juteProduction/constants.py WEAVING_CHAIN_MAX_GAP_DAYS
                ELSE LAG(x.close_jugar_z) OVER w_chain END AS oj
    FROM (
        SELECT wd2.weaving_daily_id, wd2.co_id, wd2.machine_id, wd2.tran_date,
               COALESCE(wd2.close_jugar, 0) AS close_jugar_z,
               TIMESTAMP(wd2.tran_date, COALESCE(sp.starting_time, '23:59:59')) AS eff_start,
               CASE sp.spell_code WHEN 'A1' THEN 1 WHEN 'B1' THEN 2
                    WHEN 'A2' THEN 3 WHEN 'B2' THEN 4 WHEN 'C' THEN 5
                    ELSE 99 END AS spell_rank
        FROM jute_prod_weaving_daily wd2
        LEFT JOIN spell_mst sp ON sp.spell_id = wd2.spell_id
        WHERE wd2.active = 1
    ) x
    WINDOW w_chain AS (PARTITION BY x.co_id, x.machine_id, x.weaving_quality_id
                       ORDER BY x.eff_start, x.spell_rank, x.weaving_daily_id)
) l ON l.weaving_daily_id = wd.weaving_daily_id
WHERE wd.active = 1
  AND NOT (wd.open_jugar <=> l.oj)
ORDER BY wd.co_id, wd.tran_date, wd.weaving_daily_id
LIMIT 100;
