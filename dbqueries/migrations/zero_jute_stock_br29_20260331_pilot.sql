-- ============================================================================
-- Zero jute MR stock as on 2026-03-31 — sls, branch 29 — PILOT BATCH (100 lines)
-- ============================================================================
-- Target DB   : sls   (production tenant — THE EMPIRE JUTE COMPANY LTD., co_id 2)
-- Branch      : 29 (FACTORY)
-- Date stamped: 2026-03-31
-- Tag         : jute_issue.updated_by = 'STOCKADJ-20260331'
--
-- WHY
-- Raw jute stock in branch 29 was physically zero as on 31-Mar-2026, but the data
-- migrated from the previous system left 2,802 MR lines carrying a residual balance
-- (+6,024.545 qtl net) because that system did not reconcile all receipts and issues.
-- This is the PILOT batch: the first 100 lines with a non-zero bal_qty, used to verify
-- that the adjustment mechanism drives every downstream report to zero before the
-- remaining ~2,700 lines are processed.
--
-- MECHANISM
-- jute_stock balance is computed by vw_jute_stock_outstanding as
--     bal_qty    = jute_mr_li.actual_qty    - SUM(jute_issue.quantity)
--     bal_weight = jute_mr_li.actual_weight - SUM(jute_issue.weight)
-- grouped on jute_mr_li_id, over jute_issue rows with status_id <> 4, for MRs whose
-- header status_id IN (3, 13).
--
-- So one jute_issue row per MR line carrying quantity = bal_qty and weight = bal_weight
-- drives both ledgers to zero. The values are SIGNED: an over-issued line (negative
-- balance) receives a NEGATIVE issue row, which adds the stock back. No special-casing.
--
-- WHY jute_issue AND NOT jute_mr_li
-- Editing jute_mr_li.actual_qty / actual_weight would rewrite purchase history and
-- desync jute_mr header totals, party TDS accumulation, and already-posted bill-pass
-- purchase vouchers. jute_issue is read by nothing in src/accounting/ and nothing in
-- src/juteProduction/ — its entire blast radius is the two stock views and the jute
-- reports under /api/juteReports. No FK constraints and no triggers exist on the table.
--
-- SCOPE OF THIS BATCH
--   SELECT ... FROM vw_jute_stock_outstanding
--   WHERE branch_id = 29 AND bal_qty NOT IN (0) ORDER BY jute_mr_li_id LIMIT 100
-- The id list below is that query's result, pinned so the batch is reproducible
-- (LIMIT without a pinned set is non-deterministic once rows start changing).
--   30 lines positive bal_qty, 70 negative.   Sum bal_qty -524.0, sum bal_weight -318.92.
--   All 100 have jute_mr.status_id = 3 and jute_mr_li.active = 1.
--
-- NOT COVERED BY THIS BATCH (deliberate)
--   * 184 further lines with bal_qty <> 0 beyond the LIMIT 100.
--   * ~2,500 lines whose bal_qty is already <= 0 but whose bal_weight is still non-zero.
--     The weight ledger is where the bulk of the residue lives (2,798 lines vs 284 on
--     quantity). The mrInStock report shows a row when EITHER ledger is non-zero, so it
--     will still be nearly full after this batch.
--
-- UOM NOTE
-- Weights in sls are QUINTAL, not kg — jute_mr.total_amount reproduces exactly as
-- SUM(actual_weight * rate) with no /100, and the BALE average is 1.451. issue_value
-- below therefore uses bal_weight * rate directly. (The application code's
-- weight/100*rate in issue.py / mr.py / jutePO.py is a separate, pre-existing bug.)
--
-- FLOAT PRECISION
-- jute_issue.quantity and .weight are FLOAT (single precision). The inserted value is
-- rounded to single precision on storage, so a residual of order 1e-7 can remain on
-- lines whose balance is not exactly representable in binary. ROUND(bal, 3) will read 0
-- and every report will read 0; a bare `bal_qty NOT IN (0)` test will still match the
-- dust. Use ROUND(bal_qty, 3) <> 0 as the zero-check, not <> 0.
--
-- ROLLBACK
--   DELETE FROM jute_issue
--   WHERE updated_by = 'STOCKADJ-20260331' AND branch_id = 29 AND issue_date = '2026-03-31';
-- (Rerunnable: the INSERT is guarded by NOT EXISTS on the same tag, so re-running after
--  a partial failure will not double-post.)
-- ============================================================================

INSERT INTO jute_issue (
    branch_id,
    issue_date,
    status_id,
    jute_mr_li_id,
    waste_entry_id,
    yarn_type_id,
    item_id,
    quantity,
    weight,
    issue_value,
    unit_conversion,
    updated_by,
    update_date_time
)
SELECT
    29,
    '2026-03-31',
    3,                                   -- Approved: visible to every jute report
    v.jute_mr_li_id,
    NULL,                                -- MR-sourced, not waste
    NULL,                                -- no production destination for an adjustment
    li.actual_item_id,
    v.bal_qty,                           -- SIGNED
    v.bal_weight,                        -- SIGNED
    ROUND(v.bal_weight * COALESCE(li.rate, 0), 2),
    v.unit_conversion,
    'STOCKADJ-20260331',
    NOW()
FROM vw_jute_stock_outstanding v
JOIN jute_mr_li li ON li.jute_mr_li_id = v.jute_mr_li_id
WHERE v.branch_id = 29
  AND v.jute_mr_li_id IN (
      1786,14708,14895,14897,14933,15338,15394,15612,15665,15741,
      15764,15977,16087,16102,16115,16120,16136,16269,16293,16306,
      16372,16450,16551,16589,16625,16744,16814,16829,16868,16876,
      17092,17192,17352,17548,17583,17619,17623,17651,17771,17781,
      17900,18041,18123,18129,18228,18232,18262,18280,18289,18290,
      18301,18305,18329,18335,18349,18515,19124,19680,19682,19694,
      19795,19832,19850,19891,19917,19918,19941,19943,20059,20124,
      20215,20254,20307,20308,20352,20360,20375,20390,20426,20500,
      20907,21163,21173,21213,21270,21340,21454,21471,21485,21621,
      21714,21715,21725,21762,21766,21889,22091,22149,22220,22276
  )
  AND NOT EXISTS (
      SELECT 1 FROM jute_issue x
      WHERE x.jute_mr_li_id = v.jute_mr_li_id
        AND x.updated_by    = 'STOCKADJ-20260331'
        AND x.issue_date    = '2026-03-31'
  );

-- ---------------------------------------------------------------------------
-- Verification (expect 0 rows returned)
-- ---------------------------------------------------------------------------
-- SELECT jute_mr_li_id, bal_qty, bal_weight
-- FROM vw_jute_stock_outstanding
-- WHERE branch_id = 29
--   AND jute_mr_li_id IN ( <same id list> )
--   AND (ROUND(bal_qty, 3) <> 0 OR ROUND(bal_weight, 3) <> 0);
