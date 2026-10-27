-- ============================================================================
-- sls branch 87 — set legacy jute MR stock AS ON 2026-03-29 to the user's
-- stock sheet (18 lines) and zero every other legacy line
-- ============================================================================
-- Target DB   : sls ONLY (Limelight Commercial Pvt. Ltd., co_id 106, branch 87 FACTORY)
-- Date stamped: 2026-03-29   (user: "currently there are no other entries on 29th March")
-- Tag         : jute_issue.updated_by = 'STOCKADJ-20260329'
-- Supersedes  : zero_jute_stock_br87_20260331_SUPERSEDED.sql (never run)
--
-- MUST RUN AFTER
--   1. add_sales_dtl_jute_mr_li_link_and_view_sold.sql   (applied sls 2026-08-23)
--   2. sls_br87_backfill_sales_dtl_jute_mr_li.sql        (applied sls 2026-08-23)
--   3. sls_br87_date_invoice_15702.sql                    (the INSERT refuses to run while
--      an undated approved raw-jute invoice still has linked lines on branch 87)
--   4. sls_br87_repoint_30mar_issues.sql                  (moves the four 30-Mar issues off the
--      non-sheet lines 43897/44331/44501/44975 onto sheet lines 44563/45014, so those four
--      lines zero cleanly instead of going negative — user ruling 2026-08-23)
--
-- USER RULINGS (2026-08-23, in answer to 7 questions)
--   * The sheet IS the closing stock of branch 87 on 29-Mar-2026 (18 rows, 933.64 qtl,
--     keyed on MR id + old quality id). Where it differs from both systems' own 29-Mar
--     balance (MR 27597, 27697, 27950) the sheet wins.
--   * MR 27951 stays split 35 (quality 1703) / 15 (quality 1706) as written, although
--     only 1 bale of 1706 was received on it; the later sls issues then take 1703 to
--     about -6 bales. Accepted.
--   * MR 27938: sls line actual_qty is 1 (old = 74): post the -73 qty adjustment, do not
--     edit the line.
--   * MR 27952 (MR date 31-Mar, sls out_date 28-Mar): LEAVE UNTOUCHED, excluded below.
--   * MR 27953 (31-Mar receipt) and every movement dated after 29-Mar stay as they are
--     (9 legacy issue rows 30/31-Mar + 12 April rows keep consuming the sheet stock).
--   * Everything else legacy (received <= 29-Mar) -> 0, including the 2020 opening MR
--     5320 (110.1 qtl) and the quality-697 lines whose 18 old sale lines were never
--     imported. Consequently the 27 old-system issues dated 31-Mar (issue_no
--     81785..81916, the old clearing batch) must NEVER be imported afterwards.
--
-- MECHANISM
-- One signed jute_issue row per line: quantity / weight = (sls balance as on
-- 29-Mar) - (sheet value, 0 if not on the sheet), where the sls balance as on 29-Mar =
--   actual_qty / actual_weight
--   - issues with status_id <> 4 dated <= 2026-03-29
--   - approved raw-jute sales line-linked to the line and dated <= 2026-03-29
--     (qty pro-rated by weight, exactly as the view does).
-- Computed from the base tables, NOT from vw_jute_stock_outstanding, because the view
-- nets every movement ever booked and this must be an as-on-date figure. A negative
-- row adds stock back (27951/1706 +14, 27938 +73, 27950 +21). issue_value =
-- weight * jute_mr_li.rate (legacy rows are quintal, rate per quintal). status 3.
-- Eligibility = the view's (MR status NOT IN 4,6,21; line active; line status not
-- 4/6), so only lines the reports can see are adjusted.
--
-- EXPECTED (dry-run 2026-08-23, read-only): 730 rows — 17 on sheet lines (net +78.17
-- qtl, mostly mill-vs-accepted dust; the 5 notable ones: 27597 +78 drums / +25.24,
-- 27697 +248 / +62.41, 27950 -21 / -27.72, 27951 +14 / +19.30 on 1703 and -14 / -19.05
-- on 1706, 27938 -73 qty / +0.55) and 713 on unlisted legacy lines (6,664.49 qtl);
-- total 6,742.66 qtl / 21,080 units / ~Rs 5.4 cr at jute_mr_li.rate. Exactly 4 rows
-- carry a negative quantity (27951/1706, 27938, 27950, and line 43289 which was
-- over-issued by 24 drums) and 2 a negative weight. Nothing for MR 27952 / 27953.
-- Afterwards, as on 29-Mar-2026: sheet lines read exactly the sheet (933.64 qtl),
-- every other legacy line 0, MR 27952 keeps 105.8 (out_date 28-Mar), the branch stock
-- report reads ~1,039.4 qtl; today the sheet lines read sheet minus the 30-Mar..3-Apr
-- issues (27937 -> 0, 27949 -> 92 drums, 27950 -> 37, 27951/1703 -> -6, 27932 -> 74).
--
-- ALSO KNOWN (reviewed 2026-08-23, accepted)
--   * Live view after B: 27951/1703 reads about -6 bales / -9 qtl and 27937/1831 reads
--     0 qty / -0.70 qtl (sheet weight 58.45 vs 59.15 issued later). The four sibling-
--     quality lines issued on 30-Mar (43897/44331/44501/44975) zero cleanly because
--     prerequisite 4 moved those issues to 44563 (-> 12 bales left) and 45014 (-> 39).
--   * 8 rows land on MR lines whose header is status 24 TRANSFER PENDING (incl. sheet
--     line 27697 = 46 bales / 11.55 qtl): inside the view/report eligibility, outside the
--     app's issuable statuses (1,3,13) — the stock report shows them, the picker cannot
--     issue them until the MR leaves status 24. Deliberate: B follows report eligibility.
--   * 76 rows carry a fractional quantity (pro-rata sales term) — leave, rounding would
--     push the unlisted lines off exact zero.
--   * One-day spike on 29-Mar-2026 in every issue-bearing report (DPR/_issue_mt, batch
--     cost, day/month-wise, issue register): 730 issues / 6,742.66 qtl / Rs 5.37 cr at
--     jute_mr_li.rate. Not a bug.
--   * If the invoice-date prerequisite is missing B is a SILENT no-op (0 rows, no error):
--     after running, SELECT COUNT(*) FROM jute_issue WHERE updated_by='STOCKADJ-20260329'
--     must return 730, not 0.
--
-- ROLLBACK (this batch only) — roll back B BEFORE A (sls_br87_date_invoice_15702.sql);
-- reverting A while these rows stand re-creates the 113.10 qtl phantom.
--   DELETE FROM jute_issue
--   WHERE updated_by = 'STOCKADJ-20260329' AND branch_id = 87 AND issue_date = '2026-03-29';
-- (if a row was edited in the UI its updated_by is overwritten: fall back to
--  branch_id = 87 AND issue_date = '2026-03-29' AND jute_mr_li_id IS NOT NULL)
-- Rerunnable: NOT EXISTS guard per line; per-line rows can be deleted individually to
-- reopen a single MR line.
-- ============================================================================

INSERT INTO jute_issue (
    branch_id, issue_date, status_id, jute_mr_li_id, waste_entry_id, yarn_type_id,
    item_id, quantity, weight, issue_value, unit_conversion, updated_by, update_date_time
)
SELECT
    87,
    '2026-03-29',
    3,
    x.jute_mr_li_id,
    NULL,
    NULL,
    x.actual_item_id,
    x.adj_q,                                   -- SIGNED
    x.adj_w,                                   -- SIGNED
    ROUND(x.adj_w * COALESCE(x.rate, 0), 2),
    x.unit_conversion,
    'STOCKADJ-20260329',
    NOW()
FROM (
    SELECT
        li.jute_mr_li_id,
        li.actual_item_id,
        li.rate,
        jm.unit_conversion,
        -- balance as on 29-Mar minus the sheet target
        (li.actual_qty
           - IFNULL(i.q, 0)
           - CASE WHEN li.actual_weight > 0
                  THEN li.actual_qty * IFNULL(s.w, 0) / li.actual_weight ELSE 0 END
           - IFNULL(t.tq, 0))                                        AS adj_q,
        ROUND(li.actual_weight - IFNULL(i.w, 0) - IFNULL(s.w, 0) - IFNULL(t.tw, 0), 3) AS adj_w
    FROM jute_mr jm
    JOIN jute_mr_li li ON li.jute_mr_id = jm.jute_mr_id
    -- issues booked on or before the cut
    LEFT JOIN (
        SELECT jute_mr_li_id, SUM(quantity) q, SUM(weight) w
        FROM jute_issue
        WHERE status_id <> 4 AND issue_date <= '2026-03-29'
        GROUP BY jute_mr_li_id
    ) i ON i.jute_mr_li_id = li.jute_mr_li_id
    -- approved raw-jute sales line-linked to the line, dated on or before the cut
    LEFT JOIN (
        SELECT sid.jute_mr_li_id, SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)) w
        FROM sales_invoice_dtl sid
        JOIN sales_invoice sinv ON sinv.invoice_id = sid.invoice_id
        WHERE sid.jute_mr_li_id IS NOT NULL
          AND sinv.invoice_type = 5 AND sinv.status_id = 3 AND COALESCE(sinv.active, 1) = 1
          AND sinv.invoice_date <= '2026-03-29'
        GROUP BY sid.jute_mr_li_id
    ) s ON s.jute_mr_li_id = li.jute_mr_li_id
    -- the user's sheet: MR id, old quality id, bales+drums, quintal
    LEFT JOIN (
              SELECT 27951 mr, 1703 q, 35 tq, 47.62 tw
        UNION ALL SELECT 27951, 1706, 15, 20.30
        UNION ALL SELECT 27950,  744,  92, 54.82
        UNION ALL SELECT 27949, 1931, 247, 90.86
        UNION ALL SELECT 27937, 1831,  41, 58.45
        UNION ALL SELECT 27938, 1535,  74, 105.95   -- 73 bales + 1 drum
        UNION ALL SELECT 27932, 1599,  86, 118.00
        UNION ALL SELECT 27923, 1744,  73, 90.44
        UNION ALL SELECT 27815, 1745,  30, 37.83
        UNION ALL SELECT 27820, 1597,   1, 1.48
        UNION ALL SELECT 27758,  740,   1, 1.49
        UNION ALL SELECT 27697, 1535,  46, 11.55
        UNION ALL SELECT 27685, 1745,  74, 106.52
        UNION ALL SELECT 27637,  737,  33, 43.59
        UNION ALL SELECT 27637, 1745,  13, 14.44
        UNION ALL SELECT 27597, 1535, 323, 101.54
        UNION ALL SELECT 27504,  737,  20, 28.53
        UNION ALL SELECT 24002,  739,   1, 0.23
    ) t ON t.mr = jm.jute_mr_id AND t.q = li.actual_quality
    WHERE jm.branch_id = 87
      AND jm.status_id NOT IN (4, 6, 21)
      AND li.active = 1
      AND (li.status IS NULL OR li.status NOT IN ('4', '6'))
      -- legacy = received on or before the cut (same date rule as the reports)
      AND COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date) <= '2026-03-29'
      -- user ruling: MR 27952 untouched
      AND jm.jute_mr_id <> 27952
) x
WHERE (ROUND(x.adj_q, 3) <> 0 OR ROUND(x.adj_w, 3) <> 0)
  -- PREREQUISITE 1: the branch-87 sales backfill has run (else every sold line would
  -- be over-adjusted by its whole sales volume, silently)
  AND EXISTS (
      SELECT 1 FROM sales_invoice_dtl sid
      JOIN sales_invoice sinv ON sinv.invoice_id = sid.invoice_id
      WHERE sinv.branch_id = 87 AND sinv.invoice_type = 5 AND sid.jute_mr_li_id IS NOT NULL
  )
  -- PREREQUISITE 2: no undated approved raw-jute invoice with linked lines on this
  -- branch (sls_br87_date_invoice_15702.sql) — else the view and the report diverge
  AND NOT EXISTS (
      SELECT 1 FROM sales_invoice_dtl sid
      JOIN sales_invoice sinv ON sinv.invoice_id = sid.invoice_id
      WHERE sinv.branch_id = 87 AND sinv.invoice_type = 5 AND sinv.status_id = 3
        AND sinv.invoice_date IS NULL AND sid.jute_mr_li_id IS NOT NULL
  )
  AND NOT EXISTS (
      SELECT 1 FROM jute_issue z
      WHERE z.jute_mr_li_id = x.jute_mr_li_id
        AND z.updated_by = 'STOCKADJ-20260329'
        AND z.issue_date = '2026-03-29'
  );

-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
-- (1) sheet lines as on 29-Mar == sheet; (2) every other legacy line == 0:
-- SELECT jm.jute_mr_id, li.actual_quality,
--        li.actual_qty - IFNULL(i.q,0) - CASE WHEN li.actual_weight>0 THEN li.actual_qty*IFNULL(s.w,0)/li.actual_weight ELSE 0 END AS bal_q_29,
--        ROUND(li.actual_weight - IFNULL(i.w,0) - IFNULL(s.w,0),3) AS bal_w_29
-- FROM jute_mr jm JOIN jute_mr_li li ON li.jute_mr_id=jm.jute_mr_id
-- LEFT JOIN (SELECT jute_mr_li_id, SUM(quantity) q, SUM(weight) w FROM jute_issue WHERE status_id<>4 AND issue_date<='2026-03-29' GROUP BY jute_mr_li_id) i ON i.jute_mr_li_id=li.jute_mr_li_id
-- LEFT JOIN (SELECT sid.jute_mr_li_id, SUM(COALESCE(NULLIF(sid.sales_weight,0),sid.quantity,0)) w FROM sales_invoice_dtl sid JOIN sales_invoice sinv ON sinv.invoice_id=sid.invoice_id WHERE sid.jute_mr_li_id IS NOT NULL AND sinv.invoice_type=5 AND sinv.status_id=3 AND COALESCE(sinv.active,1)=1 AND sinv.invoice_date<='2026-03-29' GROUP BY sid.jute_mr_li_id) s ON s.jute_mr_li_id=li.jute_mr_li_id
-- WHERE jm.branch_id=87 AND jm.status_id NOT IN (4,6,21) AND li.active=1 AND (li.status IS NULL OR li.status NOT IN ('4','6'))
--   AND COALESCE(jm.out_date,jm.jute_mr_date,jm.jute_gate_entry_date)<='2026-03-29'
-- HAVING ROUND(bal_q_29,3)<>0 OR ROUND(bal_w_29,3)<>0
-- ORDER BY jm.jute_mr_id;
-- expect: exactly the 18 sheet lines with the sheet values + MR 27952's line (208 / 105.8)
