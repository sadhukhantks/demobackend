-- ############################################################################
-- SUPERSEDED 2026-08-23 — NEVER RUN. Kept for the record only.
-- The user ruled that legacy branch-87 stock must NOT be zeroed; the live script is
-- sls_br87_stock_asof_20260329_adjust.sql (sets 18 lines to the user's 29-Mar sheet,
-- zeroes the rest as on 29-Mar, leaves MR 27952 / 27953 untouched).
-- ############################################################################
-- ============================================================================
-- Close legacy jute MR stock as on 2026-03-31 — sls, branch 87
-- ============================================================================
-- Target DB   : sls   (production tenant — Limelight Commercial Pvt. Ltd., co_id 106)
-- Branch      : 87 (FACTORY) — BRANCH 87 ONLY
-- Scope       : MR lines whose MR is dated ON OR BEFORE 2026-03-31 (legacy /
--               FY25-26 and earlier). FY26-27 receipts are NOT touched.
-- Date stamped: 2026-03-31
-- Tag         : jute_issue.updated_by = 'STOCKADJ-20260331'
--               (same tag family as the branch-29 exercise; rollback below is
--               scoped by branch so the two never reverse each other)
--
-- MUST RUN AFTER
--   1. add_sales_dtl_jute_mr_li_link_and_view_sold.sql   (view subtracts line-linked sales)
--   2. sls_br87_backfill_sales_dtl_jute_mr_li.sql        (branch-87 sales linked to MR lines)
-- Run it before those two and it would post ~92,600 qtl of adjustments for stock
-- that the reports already treat as sold — i.e. double-count. The INSERT below
-- therefore HARD-DEPENDS on both: it reads v.sold_weight (error 1054 if the view
-- is old) and refuses to insert anything while no branch-87 sales line carries a
-- jute_mr_li_id (0 rows if the backfill is missing).
--
-- WHAT IS BEING FIXED (measured 2026-08-23, before any change)
-- Branch 87's legacy lines showed 1,729 open keys / 92,647 qtl in the line
-- ledger (vw_jute_stock_outstanding = issue picker / mrInStock / MR Wise):
--   * 1,129 lines never issued in sls (90,885 qtl): the OLD system shows them
--     SOLD (88,889 qtl) or issued (1,801 qtl) — old balance 110 qtl. The sales
--     exist in sls (85,111 qtl linked by the backfill) but had no line link, so
--     the line ledger never saw them. The link clears 999 of 1,075 sold lines to
--     exact zero by itself.
--   *   600 lines partly issued with residue (1,762 qtl): 576 of them have
--     bal_qty already 0 and only a weight tail (0.03 – 35 qtl each). Root causes:
--     sls actual_weight is the old MILL weight while old issues were booked
--     against ACCEPTED weight (shortage_kgs stays behind), old per-bale rounding,
--     27 pre-cut old issue rows never imported, and old issues dated after
--     2026-03-31 that drew these lines down (old system is still live for this
--     company). Old balance on these 600 lines: 13 qtl.
-- After the two prerequisite scripts the residue is 730 keys / 7,535.9 qtl
-- (654 keys with no sale 7,462.3 qtl; 76 sold lines with a 73.6 qtl tail). The
-- old system carries ~270 qtl on the same lines today, i.e. everything here is
-- stale legacy residue, not physical stock. This batch zeroes it.
--
-- MECHANISM (proven on branch 29, 2,987 rows, exact zero on both ledgers)
-- One jute_issue row per open key carrying the SIGNED balance:
--     quantity = bal_qty, weight = bal_weight  (both from the view, post-link)
-- An over-issued (negative) ledger gets a NEGATIVE row that adds the stock back.
-- The view nets EVERY issue ever booked against the line, so a legacy line that
-- was also drawn in sls after the cut (12 April-2026 rows exist) is adjusted by
-- its CURRENT residue and lands on exactly 0 today; as on 2026-03-31 it shows
-- the April draw as still in stock, which is physically correct.
-- status_id 3 (approved), item_id = line's actual item, issue_value =
-- bal_weight * jute_mr_li.rate (legacy rows are quintal, rate per quintal).
--
-- WHY jute_issue AND NOT jute_mr_li
-- Editing actual_qty / actual_weight would rewrite purchase history, MR totals,
-- TDS and posted bill-pass vouchers. jute_issue reaches nothing in
-- src/accounting/; its blast radius is the stock views, /api/juteReports and
-- the winding stock query (src/juteProduction/winding_stock_query.py reuses
-- _issue_cte), where these rows show as 730 issues / 7,535.9 qtl /
-- ~Rs 6.39 cr on 2026-03-31 under yarn_type NULL — same as the br-29 precedent.
--
-- KNOWN EDGES (accepted, reviewed 2026-08-23)
--   * 2 of the 12 April-2026 sls issue rows are DRAFT (jute_issue_id 86361 /
--     86362, 37.66 qtl, updated_by 'system'). Both ledgers net status <> 4, so
--     the adjustment is sized net of them; rejecting them later resurrects
--     37.66 qtl of legacy stock (consistently on both ledgers).
--   * 4 legacy lines the old system still carried when measured (jute_mr_li_id
--     7950 = 110.1 qtl 2020 opening MR, 43289, 43340, 45014 = 1 unit / 106.5 qtl
--     vs old 74 units — sls mis-keyed qty; old system issued 73 of 74 by
--     2026-05-15) are zeroed with the rest: 227.59 qtl. Re-open with negative
--     STOCKADJ rows if a physical count says otherwise.
--   * 16 lines of 8 DRAFT 2022 invoices were linked by the backfill; their MR
--     lines are zeroed here as legacy residue (see the backfill header).
--
-- NOT COVERED (deliberate — separate decisions, see the session notes)
--   * 139 lines on FY26-27 MRs (dated after 2026-03-31; 1,017,586 in
--     actual_weight — entered in KG in the new system, never issued in sls
--     because this company still books jute issues in the old system).
--   * The unit mix (legacy quintal vs new kg) on this branch.
--
-- EXPECTED RESULT
--   ~730 rows inserted. Afterwards, for branch 87, every legacy key reads 0 in
--   the line ledger; the Jute Stock Report as on 2026-03-31 moves from 7,841.6
--   to ~305.7 qtl = 192.61 (the 12 April-2026 draws, still in stock on 31-Mar)
--   + 113.10 (approved invoice 15702 / LCP/DEC/049/24-25 has invoice_date NULL,
--   old system too — the branch report buckets sales by invoice_date so it never
--   counts it, while the line ledger does; backfilling that date, e.g. from its
--   challan_date 2024-12-16, is a separate one-row ruling). The issue picker
--   offers only FY26-27 lines.
--
-- ROLLBACK (this batch only — branch 87)
--   DELETE FROM jute_issue
--   WHERE updated_by = 'STOCKADJ-20260331' AND branch_id = 87 AND issue_date = '2026-03-31';
--
-- Rerunnable: the NOT EXISTS guard prevents double-posting against any key that
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
    87,
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
JOIN jute_mr    jm ON jm.jute_mr_id    = li.jute_mr_id
WHERE v.branch_id = 87
  AND (ROUND(v.bal_qty, 3) <> 0 OR ROUND(v.bal_weight, 3) <> 0)
  AND COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date) <= '2026-03-31'
  -- PREREQUISITE GUARDS: new view in place (column must exist) and the branch-87
  -- sales backfill done (else this would double-count sold stock)
  AND v.sold_weight IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM sales_invoice_dtl sid
      JOIN sales_invoice sinv ON sinv.invoice_id = sid.invoice_id
      WHERE sinv.branch_id = 87 AND sinv.invoice_type = 5 AND sid.jute_mr_li_id IS NOT NULL
  )
  AND NOT EXISTS (
      SELECT 1 FROM jute_issue x
      WHERE x.jute_mr_li_id = v.jute_mr_li_id
        AND x.updated_by    = 'STOCKADJ-20260331'
        AND x.issue_date    = '2026-03-31'
  );

-- ---------------------------------------------------------------------------
-- Verification (expect 0 rows)
-- ---------------------------------------------------------------------------
-- SELECT v.jute_mr_li_id, v.bal_qty, v.bal_weight
-- FROM vw_jute_stock_outstanding v
-- JOIN jute_mr_li li ON li.jute_mr_li_id = v.jute_mr_li_id
-- JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
-- WHERE v.branch_id = 87
--   AND COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date) <= '2026-03-31'
--   AND (ROUND(v.bal_qty, 3) <> 0 OR ROUND(v.bal_weight, 3) <> 0);
