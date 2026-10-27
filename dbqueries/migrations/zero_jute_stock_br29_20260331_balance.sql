-- ============================================================================
-- Zero jute MR stock as on 2026-03-31 — sls, branch 29 — BALANCE BATCH
-- ============================================================================
-- Target DB   : sls   (production tenant — THE EMPIRE JUTE COMPANY LTD., co_id 2)
-- Branch      : 29 (FACTORY)  — BRANCH 29 ONLY. Do not extend to 4 / 30 / 87 / 106,
--                               which have live FY26-27 activity and a different cut-off.
-- Date stamped: 2026-03-31
-- Tag         : jute_issue.updated_by = 'STOCKADJ-20260331'  (same tag as the pilot,
--               so pilot + balance reverse together as one set)
--
-- Follows: zero_jute_stock_br29_20260331_pilot.sql (100 rows, jute_issue_id 81874-81973,
-- committed 2026-08-12, all keys verified to exact zero on both ledgers).
--
-- WHY THE FILTER CHANGED FROM THE PILOT
-- The pilot used `bal_qty NOT IN (0)`, which matches only 284 keys. The bulk of the
-- residue lives on the WEIGHT ledger: of the 2,703 keys still outstanding,
--     2,519 carry weight residue with bal_qty ALREADY zero,
--       182 carry residue on both ledgers,
--         2 carry quantity residue only.
-- mrInStock shows a row when EITHER ledger is non-zero, so a quantity-only filter can
-- never empty the report. This batch therefore uses:
--     (ROUND(bal_qty,3) <> 0 OR ROUND(bal_weight,3) <> 0)
--
-- AS-ON-DATE SAFETY (verified 2026-08-12, not assumed)
-- vw_jute_stock_outstanding has NO date filter — bal_qty/bal_weight net EVERY issue ever
-- booked against the line. That is only safe to treat as an as-on-31-Mar balance if the
-- branch has no post-cut activity. Verified for branch 29:
--   * 0 jute_issue rows in br29 dated after 2026-03-31
--   * 0 jute_issue rows from ANY branch after the cut touching a br29 MR line
--   * 0 jute_issue rows with NULL issue_date
--   * 0 MRs (status 3/13) with out_date after the cut
--   * per-key diff of view balance vs a strictly date-restricted as-on-cut balance:
--     0.000 qty / 0.000 weight across all 7,202 keys
-- The only post-cut dates in br29 are 6 FY26-27 gate entries (status_id 1, no line
-- items) which the view excludes. 30 MRs / 52 lines have NULL out_date but their
-- jute_mr_date and jute_gate_entry_date all fall 2025-04-26..2025-12-20, i.e. pre-cut,
-- so they are correctly in scope.
--
-- MECHANISM (unchanged from the pilot, proven there)
--     bal_qty    = jute_mr_li.actual_qty    - SUM(jute_issue.quantity)
--     bal_weight = jute_mr_li.actual_weight - SUM(jute_issue.weight)
-- grouped on jute_mr_li_id, over jute_issue with status_id <> 4, MR header status IN (3,13).
-- One jute_issue row per key carrying the SIGNED balance drives both ledgers to zero.
-- An over-issued (negative-balance) line receives a NEGATIVE issue row, which adds the
-- stock back. The pilot confirmed this lands on EXACT zero — no float residue, despite
-- quantity/weight being FLOAT.
--
-- WHY jute_issue AND NOT jute_mr_li
-- Editing jute_mr_li.actual_qty / actual_weight would rewrite purchase history and desync
-- jute_mr header totals, party TDS accumulation, and already-posted bill-pass purchase
-- vouchers. jute_issue is read by nothing in src/accounting/ and nothing in
-- src/juteProduction/; it has no FK constraints and no triggers. Its entire blast radius
-- is the two stock views and the reports under /api/juteReports.
--
-- EXPECTED RESULT
--   ~2,703 rows inserted. Afterwards, for branch 29:
--     vw_jute_stock_outstanding  -> every key 0 on both ledgers
--     mrInStock / MR Wise / Godown Wise / Jute Issue stock picker -> empty
--   NOT fixed by this batch (needs a code change, not data): Daily Jute Stock, Qty Wise,
--   Inventory Snapshot, Jute with Value, Month/Day Wise recompute from base tables with
--   different filters and will read roughly -52,720 qtl, driven by 31,688 qtl of issues
--   with a dangling jute_mr_li_id and 65,283 qtl of issues against never-approved MRs.
--
-- UOM: weights in sls are QUINTAL, not kg (jute_mr.total_amount reproduces exactly as
-- SUM(actual_weight * rate) with no /100; BALE average 1.451). issue_value below uses
-- bal_weight * rate directly.
--
-- ROLLBACK (reverses pilot AND balance together)
--   DELETE FROM jute_issue
--   WHERE updated_by = 'STOCKADJ-20260331' AND branch_id = 29 AND issue_date = '2026-03-31';
--
-- Rerunnable: the NOT EXISTS guard prevents double-posting against any key that already
-- carries a tagged adjustment row.
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
  AND (ROUND(v.bal_qty, 3) <> 0 OR ROUND(v.bal_weight, 3) <> 0)
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
--   AND (ROUND(bal_qty, 3) <> 0 OR ROUND(bal_weight, 3) <> 0);
