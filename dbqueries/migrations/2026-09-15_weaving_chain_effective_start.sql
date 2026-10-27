-- Migration: 2026-09-15_weaving_chain_effective_start.sql (revised 2026-09-16)
-- TKT-000150 / TKT-000156: the weaving jugar chain (open_jugar = previous row's
-- close_jugar) now orders rows by tran_date + spell_mst.starting_time instead of
-- the hardcoded spell-code rank. An overnight spell (is_overnight = 1) is dated by
-- the day it STARTS (C dated 14-Sep = night 14-Sep 22:00 to 15-Sep 06:00), so it
-- sorts last on its own date and the next morning shift follows it. The rank CASE
-- (A1 B1 A2 B2 C) survives only as a tiebreak for spells sharing a start time.
-- Gap cap: a predecessor older than 45 days counts as none (opening 0).
--
-- Why: sls branch 29 uses spells A / B / C. The old CASE ranked A and B as 99 and
-- C as 5, so every day sorted C, A, B - the morning shift opened with its own
-- night shift's closing and the night shift opened with the previous day's.
-- (A 2026-09-15 draft that moved overnight rows to the previous day was rejected
-- by the crew's data-entry order and is superseded by this file.)
--
-- This file re-creates the reference oracle view vw_weaving_daily with the new
-- ordering (DDL copied from create_weaving_tables.sql, LAG ORDER BY changed).
-- Code side: weaving_query._open_jugar_probe_sql + get_weaving_chain_successor_query.
-- Data side: re-run dbqueries/migrations/backfill_weaving_open_jugar.sql (chunked on
-- big tenants) or the scoped repair dbqueries/repairs/weaving_open_jugar_repair_20260915.py.
--
-- EXECUTOR CONSTRAINT: no semicolon in comment prose, header terminated by the lone
-- semicolon below.
--
-- ROLLBACK: re-run the CREATE OR REPLACE VIEW block from
-- dbqueries/migrations/weaving_standards_machine_dimension.sql (previous ordering).
;

CREATE OR REPLACE VIEW vw_weaving_daily AS
SELECT
    c.weaving_daily_id, c.co_id, c.branch_id, c.tran_date,
    c.spell_id, c.spell_code, c.shift_bucket, c.spell_rank,
    c.machine_id, c.mech_code, c.machine_name, c.line_no,
    c.weaving_quality_id, c.item_id, c.item_code, c.item_name,
    c.weaving_quality_code, c.weaving_quality_name, c.is_composite,
    c.eb_id, c.beam_no,
    c.cuts, c.close_jugar, c.less_production,
    c.finished_length, c.ozs_yds, c.std_ozs_yds, c.no_of_jugar_per_cut,
    c.std_speed, c.act_speed, c.std_picks, c.act_picks, c.std_eff, c.target_eff,
    c.eff_speed, c.eff_picks, c.working_hours,
    c.open_jugar, c.jugar,
    ROUND(c.production_yds, 3)                                            AS production_yds,
    ROUND(c.production_yds * c.ozs_yds * 28.35 / 1000, 3)                 AS production_kg,
    ROUND(c.production_yds * c.ozs_yds * 28.35 / 1000 / 1000, 4)          AS production_mt,
    ROUND(c.std_prod_yds, 3)                                             AS std_prod_yds,
    ROUND(CASE WHEN c.target_eff > 0 THEN c.std_prod_yds * c.target_eff / 100 ELSE 0 END, 3) AS target_prod_yds,
    ROUND(CASE WHEN c.std_prod_yds > 0 THEN c.production_yds * 100 / c.std_prod_yds ELSE 0 END, 2) AS efficiency,
    ROUND(CASE WHEN c.std_ozs_yds IS NOT NULL THEN c.production_yds * c.std_ozs_yds * 28.35 / 1000 ELSE 0 END, 3) AS std_prod_kg,
    ROUND(CASE WHEN c.std_ozs_yds IS NOT NULL AND c.target_eff > 0
               THEN c.production_yds * c.std_ozs_yds * 28.35 / 1000 * (c.target_eff / 100) ELSE 0 END, 3) AS target_kg
FROM (
    SELECT
        b.*,
        b.total_jugar AS jugar,
        CASE WHEN b.no_of_jugar_per_cut > 0
             THEN b.total_jugar * b.finished_length / b.no_of_jugar_per_cut
             ELSE 0 END AS production_yds,
        -- std_prod_yds uses std_picks (exact-day SQC avg), NOT eff_picks: no SQC that day
        -- => std_picks 0 => std_prod_yds 0 => efficiency 0 (no last-date/target-map fallback).
        CASE WHEN (36 * b.std_picks) > 0
             THEN (b.eff_speed * b.working_hours * 60) / (36 * b.std_picks)
             ELSE 0 END AS std_prod_yds
    FROM (
        SELECT
            a.*,
            (a.cuts * a.no_of_jugar_per_cut + a.close_jugar - a.open_jugar
             - COALESCE(a.less_production, 0)) AS total_jugar
        FROM (
            SELECT
                wd.weaving_daily_id, wd.co_id, wd.branch_id, wd.tran_date,
                wd.spell_id, sp.spell_code, LEFT(sp.spell_code, 1) AS shift_bucket,
                CASE sp.spell_code WHEN 'A1' THEN 1 WHEN 'B1' THEN 2 WHEN 'A2' THEN 3
                                   WHEN 'B2' THEN 4 WHEN 'C' THEN 5 ELSE 99 END AS spell_rank,
                wd.machine_id, m.mech_code, m.machine_name, m.line_no,
                wd.weaving_quality_id AS weaving_quality_id,
                q.item_id, im.item_code, im.item_name,
                q.weaving_quality_code, q.weaving_quality_name, q.is_composite,
                wd.eb_id, wd.beam_no,
                wd.cuts,
                COALESCE(wd.close_jugar, 0)                AS close_jugar,
                COALESCE(wd.less_production, 0)            AS less_production,
                COALESCE(q.finished_length, 0)            AS finished_length,
                COALESCE(q.ozs_yds, 0)                     AS ozs_yds,
                q.std_ozs_yds,
                COALESCE(q.no_of_jugar_per_cut, 0)        AS no_of_jugar_per_cut,
                COALESCE(s.std_speed, 0)                   AS std_speed,
                COALESCE(s.act_speed, 0)                   AS act_speed,
                COALESCE(s.std_picks, 0)                   AS std_picks,
                COALESCE(s.act_picks, 0)                   AS act_picks,
                COALESCE(s.std_eff, 0)                     AS std_eff,
                COALESCE(s.target_eff, 0)                  AS target_eff,
                CASE WHEN COALESCE(s.act_speed,0) > 0 THEN s.act_speed ELSE COALESCE(s.std_speed,0) END AS eff_speed,
                CASE WHEN COALESCE(s.act_picks,0) > 0 THEN s.act_picks ELSE COALESCE(s.std_picks,0) END AS eff_picks,
                GREATEST(0, COALESCE(sp.working_hours, 0) - COALESCE((
                    SELECT SUM(st.stoppage_hours) FROM jute_prod_stoppage_hours st
                    WHERE st.active = 1 AND st.co_id = wd.co_id AND st.machine_id = wd.machine_id
                      AND st.tran_date = wd.tran_date AND st.spell_id = wd.spell_id), 0)) AS working_hours,
                -- chain order (2026-09-16, TKT-000150/156): tran_date + spell START TIME, not
                -- spell codes. An overnight spell is dated by the day it starts and sorts last
                -- on that date. Rank CASE = tiebreak only (spells sharing a start time).
                -- gap cap (2026-09-16): predecessor older than 45 days -> opening 0
                -- NULL quality (capture-before-map loom) never chains: opening 0
                CASE WHEN wd.weaving_quality_id IS NULL THEN 0
                     WHEN LAG(wd.tran_date) OVER w_chain IS NULL
                       OR DATEDIFF(wd.tran_date, LAG(wd.tran_date) OVER w_chain) > 45 THEN 0  -- keep in sync with src/juteProduction/constants.py WEAVING_CHAIN_MAX_GAP_DAYS
                     ELSE COALESCE(LAG(wd.close_jugar) OVER w_chain, 0) END AS open_jugar
            FROM jute_prod_weaving_daily wd
            LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
            LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
            LEFT JOIN jute_prod_weaving_quality q
                   ON q.weaving_quality_id = wd.weaving_quality_id
            LEFT JOIN item_mst im ON im.item_id = q.item_id
            LEFT JOIN (
                SELECT
                    d2.weaving_daily_id,
                    (SELECT tm.value FROM jute_prod_weaving_target_map tm
                      WHERE tm.co_id = d2.co_id AND tm.ref_id = d2.mid AND tm.id_type='mcid'
                        AND tm.value_role='standard' AND tm.param='speed' AND tm.active=1
                        AND tm.effective_date <= d2.tran_date
                      ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC LIMIT 1) AS std_speed,
                    (SELECT tm.value FROM jute_prod_weaving_target_map tm
                      WHERE tm.co_id = d2.co_id AND tm.ref_id = d2.mid AND tm.id_type='mcid'
                        AND tm.value_role='actual' AND tm.param='speed' AND tm.active=1
                        AND tm.effective_date <= d2.tran_date
                      ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC LIMIT 1) AS act_speed,
                    -- std_picks = exact-day SQC quality-average picks (R-08-21 jute_sqc_weaving_pick
                    -- via vw_weaving_pick_act). entry_date = tran_date EXACTLY (no last-date carry,
                    -- no target-map fallback): no SQC reading that day => NULL => std_prod_yds/eff 0.
                    (SELECT pv.avg_picks FROM vw_weaving_pick_act pv
                      WHERE pv.co_id = d2.co_id AND pv.weaving_quality_id = d2.qid
                        AND pv.entry_date = d2.tran_date
                      LIMIT 1) AS std_picks,
                    (SELECT pv.avg_picks FROM vw_weaving_pick_act pv
                      WHERE pv.co_id = d2.co_id AND pv.weaving_quality_id = d2.qid
                        AND pv.entry_date <= d2.tran_date
                      ORDER BY pv.entry_date DESC LIMIT 1) AS act_picks,
                    (SELECT tm.value FROM jute_prod_weaving_target_map tm
                      WHERE tm.co_id = d2.co_id AND tm.ref_id = d2.qid AND tm.id_type='qid'
                        AND tm.value_role='standard' AND tm.param='eff' AND tm.active=1
                        AND tm.effective_date <= d2.tran_date
                      ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC LIMIT 1) AS std_eff,
                    (SELECT tm.value FROM jute_prod_weaving_target_map tm
                      WHERE tm.co_id = d2.co_id AND tm.ref_id = d2.qid AND tm.id_type='qid'
                        AND tm.value_role='target' AND tm.param='eff' AND tm.active=1
                        AND tm.effective_date <= d2.tran_date
                      ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC LIMIT 1) AS target_eff
                FROM (
                    SELECT w.weaving_daily_id, w.co_id, w.tran_date,
                           w.weaving_quality_id AS qid, w.machine_id AS mid
                    FROM jute_prod_weaving_daily w
                    WHERE w.active = 1
                ) d2
            ) s ON s.weaving_daily_id = wd.weaving_daily_id
            WHERE wd.active = 1
            WINDOW w_chain AS (PARTITION BY wd.co_id, wd.machine_id, wd.weaving_quality_id
                               ORDER BY TIMESTAMP(wd.tran_date, COALESCE(sp.starting_time, '23:59:59')),
                                        CASE sp.spell_code WHEN 'A1' THEN 1 WHEN 'B1' THEN 2 WHEN 'A2' THEN 3
                                                           WHEN 'B2' THEN 4 WHEN 'C' THEN 5 ELSE 99 END,
                                        wd.weaving_daily_id)
        ) a
    ) b
) c;
