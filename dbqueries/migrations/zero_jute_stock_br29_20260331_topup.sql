-- ============================================================================
-- Zero jute MR stock as on 2026-03-31 — sls, branch 29 — TOP-UP BATCH
-- ============================================================================
-- Target DB   : sls   (production tenant — THE EMPIRE JUTE COMPANY LTD., co_id 2)
-- Branch      : 29 (FACTORY)  — BRANCH 29 ONLY
-- Date stamped: 2026-03-31
-- Tag         : jute_issue.updated_by = 'STOCKADJ-20260331'  (same tag as the pilot
--               and balance batches, so all three reverse as ONE set)
--
-- Follows:
--   zero_jute_stock_br29_20260331_pilot.sql    (100 rows, jute_issue_id 81874-81973)
--   zero_jute_stock_br29_20260331_balance.sql  (2,703 rows, 81974-84703)
--   update_vw_jute_stock_outstanding_eligibility.sql  (MUST be applied first)
--
-- WHY A THIRD BATCH
-- The first two batches were computed off vw_jute_stock_outstanding when that view
-- still filtered `jute_mr.status_id IN (3,13)`. They therefore zeroed only the
-- APPROVED slice of branch 29 — and they did it perfectly: receipts 470,329.75 minus
-- issues 470,329.75 = exactly 0.00 on status 3.
--
-- The eligibility migration widened the view to `status_id NOT IN (4,6,21)`, admitting
-- OPEN (1), PENDING-L2 (17) and TRANSFER-PENDING (24). Those 578 MRs were never in
-- the view, so the earlier batches could not see them, and they carry a residue:
--
--     MR status  1 OPEN               418 MRs   47,611.90 recv - 47,145.28 iss = +466.617
--     MR status 24 TRANSFER PENDING   129 MRs   14,774.80 recv - 14,517.84 iss = +256.957
--     MR status 17 PENDING APPROVAL L2 31 MRs    3,454.40 recv -  3,454.38 iss = +  0.024
--                                                                        total = +723.598
--
-- Spread over 184 MR lines (985.310 qty). This batch zeroes exactly those.
--
-- AS-ON-DATE SAFETY (verified 2026-08-13 against the NEW view, not assumed)
--   * All 184 residual lines have COALESCE(out_date, jute_mr_date, jute_gate_entry_date)
--     on or before 2026-03-31. ZERO fall after the cut, zero are NULL.
--   * Branch 29 still has 0 jute_issue rows dated after 2026-03-31.
--   * Restricting the batch to eff_date <= cut selects the identical 184 lines, so no
--     current-period stock is being zeroed.
--
-- NOT COVERED (deliberate)
--   The 500 qtl jute_prod_waste_entry (id 1, item 335637 Thread Wastage, branch 29,
--   tran_date 2026-08-10). Waste is a separate stock source, it is NOT in this view,
--   and it is dated August 2026 — current-period stock that must survive a 31-Mar
--   exercise. After this batch the Jute Stock Report reads 0.00 as-on 2026-03-31 and
--   500.00 as-on today, which is correct.
--
-- MECHANISM (unchanged, proven by the two earlier batches landing on exact zero)
-- One jute_issue row per non-zero line carrying the SIGNED balance. A negative-balance
-- line receives a NEGATIVE issue row, which adds the stock back.
--
-- ROLLBACK (reverses ALL THREE batches together)
--   DELETE FROM jute_issue
--   WHERE updated_by = 'STOCKADJ-20260331' AND branch_id = 29 AND issue_date = '2026-03-31'
--
-- Rerunnable: the NOT EXISTS guard prevents double-posting against any line that
-- already carries a tagged adjustment row.
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
    3,
    v.jute_mr_li_id,
    NULL,
    NULL,
    li.actual_item_id,
    v.bal_qty,
    v.bal_weight,
    ROUND(v.bal_weight * COALESCE(li.rate, 0), 2),
    v.unit_conversion,
    'STOCKADJ-20260331',
    NOW()
FROM vw_jute_stock_outstanding v
JOIN jute_mr_li li ON li.jute_mr_li_id = v.jute_mr_li_id
JOIN jute_mr    jm ON jm.jute_mr_id    = li.jute_mr_id
WHERE v.branch_id = 29
  AND (ROUND(v.bal_qty, 3) <> 0 OR ROUND(v.bal_weight, 3) <> 0)
  AND COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date) <= '2026-03-31'
  AND NOT EXISTS (
      SELECT 1 FROM jute_issue x
      WHERE x.jute_mr_li_id = v.jute_mr_li_id
        AND x.updated_by    = 'STOCKADJ-20260331'
        AND x.issue_date    = '2026-03-31'
  );

-- ---------------------------------------------------------------------------
-- Verification (expect 0 rows)
-- ---------------------------------------------------------------------------
-- SELECT jute_mr_li_id, bal_qty, bal_weight FROM vw_jute_stock_outstanding
-- WHERE branch_id = 29 AND (ROUND(bal_qty,3) <> 0 OR ROUND(bal_weight,3) <> 0)
