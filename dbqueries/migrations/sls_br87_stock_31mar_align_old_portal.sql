-- ============================================================================
-- sls branch 87 — align 31-Mar-2026 jute stock to the old-portal quality-wise
--                 balances (Limelight co 106, FACTORY)
-- ============================================================================
-- Target DB   : sls ONLY
-- Date        : 2026-08-31   User: "make adjustments for the entries done on 29th
--               March so that the stock as on 31-Mar-2026 matches the old portal."
-- Depends on  : sls_br87_repoint_30mar_issues.sql,
--               sls_br87_stock_asof_20260329_adjust.sql (STOCKADJ-20260329),
--               sls_br87_continuing_stock_x100.sql — all already live on sls.
--
-- WHY — the /api/juteReports/stock report for 2026-03-31 differs from the old
-- portal on exactly 5 items, every diff pinned to one MR line (all weights kg):
--
--   item          line  MR     sls bal  old bal  diff   cause
--   C TD-5        45033 27952  10,580   10,530    +50   sls holds mill weight 10,580; old accepted 105.30 qtl
--   C TD-6        45029 27950   5,482    2,650  +2832   30-Mar issues 81781/81782 (21 bales, 2,832 kg) were
--   J TD-8        44563 27637   1,527    4,359  -2832     re-pointed onto 44563 on 08-23 ("nearest grade");
--                                                         old portal keeps J TD-8 intact and consumes C TD-6
--   A TD-9 (U)    45014 27938   5,650   10,595  -4945   30-Mar issues 81777/81778 (35 bales, 4,945 kg) hit
--                                                         45014; in the old system they consumed A TD-7 (L)
--                                                         legacy lines already written off by STOCKADJ-20260329
--                                                         (44501/44975 — still quintal-scale, cannot take kg
--                                                         issues back)
--   A TD-7 (U)    45034 27953  10,998   10,928    +70   mill 12,970 - issue 1,972; old accepted 109.28 qtl
--
-- FIX (two parts):
--
-- PART 1 — re-point 81781/81782 from 44563 (J TD-8) to 45029 (C TD-6, item 1127).
--   Fixes both the +2,832 and the -2,832 at once; 29-Mar sheet values stay intact
--   (C TD-6 5,482 on 29-Mar is then consumed on 30-Mar). No negative: 45029 bal
--   30-Mar = 5,482 - 2,832 = 2,650; after the April 'system' issue 86356 -> 583.
--   This refines the 08-23 ruling for these two rows (that target was an
--   availability pick, not physical truth).
--
-- PART 2 — three signed adjustment rows, tag STOCKADJ2-20260329:
--   45033  2026-03-29  qty   0  wt    +50  (issue; trims mill->accepted)
--   45014  2026-03-29  qty -35  wt -4,945  (adds back what the 30-Mar issues take;
--                                           qty -35 restores the 74-count old world)
--   45034  2026-03-31  qty   0  wt    +70  (dated 31-Mar because the MR itself is
--                                           received 31-Mar — a 29-Mar row would
--                                           print a transient negative)
--   issue_value = weight/100 * rate (these lines are kg-scale; rate is per quintal).
--
-- RESULT — stock report closing per item on 2026-03-31 equals the old portal:
--   C TD-5 10,530 · C TD-6 2,650 · J TD-8 7,212 · A TD-7 (U) 10,928 ·
--   A TD-9 (U) 21,904 · grand total 103,410.00 kg (was 98,585.00).
--   29-Mar closing moves 103,944 -> 108,839 (A TD-9 +4,945, C TD-5 -50) — accepted
--   consequence of the user's instruction; 30-Mar closing = 92,412... (per item as
--   old). Nothing after 2026-03-31 is touched.
--
-- ROLLBACK (reverse order):
--   DELETE FROM jute_issue WHERE updated_by = 'STOCKADJ2-20260329' AND branch_id = 87;
--   UPDATE jute_issue SET jute_mr_li_id = 44563, item_id = 1138
--    WHERE jute_issue_id = 81781 AND jute_mr_li_id = 45029;
--   UPDATE jute_issue SET jute_mr_li_id = 44563, item_id = 1138
--    WHERE jute_issue_id = 81782 AND jute_mr_li_id = 45029;
-- ============================================================================

-- PART 1: re-point the two 30-Mar issues J TD-8 -> C TD-6 (guarded, rerunnable)
UPDATE jute_issue
SET jute_mr_li_id = 45029, item_id = 1127
WHERE jute_issue_id = 81781 AND jute_mr_li_id = 44563
  AND branch_id = 87 AND issue_date = '2026-03-30';

UPDATE jute_issue
SET jute_mr_li_id = 45029, item_id = 1127
WHERE jute_issue_id = 81782 AND jute_mr_li_id = 44563
  AND branch_id = 87 AND issue_date = '2026-03-30';

-- PART 2: signed adjustment rows (each guarded against re-insert by tag+line)
INSERT INTO jute_issue (
    branch_id, issue_date, status_id, jute_mr_li_id, waste_entry_id, yarn_type_id,
    item_id, quantity, weight, issue_value, unit_conversion, updated_by, update_date_time
)
SELECT 87, '2026-03-29', 3, li.jute_mr_li_id, NULL, NULL,
       li.actual_item_id, 0, 50,
       ROUND(50 / 100 * COALESCE(li.rate, 0), 2),
       jm.unit_conversion, 'STOCKADJ2-20260329', NOW()
FROM jute_mr_li li
JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
WHERE li.jute_mr_li_id = 45033
  AND NOT EXISTS (SELECT 1 FROM jute_issue g
                  WHERE g.updated_by = 'STOCKADJ2-20260329' AND g.jute_mr_li_id = 45033);

INSERT INTO jute_issue (
    branch_id, issue_date, status_id, jute_mr_li_id, waste_entry_id, yarn_type_id,
    item_id, quantity, weight, issue_value, unit_conversion, updated_by, update_date_time
)
SELECT 87, '2026-03-29', 3, li.jute_mr_li_id, NULL, NULL,
       li.actual_item_id, -35, -4945,
       ROUND(-4945 / 100 * COALESCE(li.rate, 0), 2),
       jm.unit_conversion, 'STOCKADJ2-20260329', NOW()
FROM jute_mr_li li
JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
WHERE li.jute_mr_li_id = 45014
  AND NOT EXISTS (SELECT 1 FROM jute_issue g
                  WHERE g.updated_by = 'STOCKADJ2-20260329' AND g.jute_mr_li_id = 45014);

INSERT INTO jute_issue (
    branch_id, issue_date, status_id, jute_mr_li_id, waste_entry_id, yarn_type_id,
    item_id, quantity, weight, issue_value, unit_conversion, updated_by, update_date_time
)
SELECT 87, '2026-03-31', 3, li.jute_mr_li_id, NULL, NULL,
       li.actual_item_id, 0, 70,
       ROUND(70 / 100 * COALESCE(li.rate, 0), 2),
       jm.unit_conversion, 'STOCKADJ2-20260329', NOW()
FROM jute_mr_li li
JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
WHERE li.jute_mr_li_id = 45034
  AND NOT EXISTS (SELECT 1 FROM jute_issue g
                  WHERE g.updated_by = 'STOCKADJ2-20260329' AND g.jute_mr_li_id = 45034);

-- Verification:
-- SELECT jute_issue_id, jute_mr_li_id, item_id, quantity, weight, issue_date, updated_by
--   FROM jute_issue WHERE jute_issue_id IN (81781, 81782)
--     OR updated_by = 'STOCKADJ2-20260329';
-- Expect: 81781/81782 on 45029 item 1127; 3 tagged rows (45033 +50, 45014 -35/-4945,
-- 45034 +70). Then the /stock report for branch 87, 2026-03-31 closes at 103,410.00:
-- C TD-5 10,530 / C TD-6 2,650 / J TD-8 7,212 / A TD-7 (U) 10,928 / A TD-9 (U) 21,904.
