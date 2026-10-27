-- =====================================================================
-- sls_fix_jmr00015_repair.sql
-- Tenant: sls  (LIVE, shared)      Author: data investigation, 2026-09-22
-- NOT RUN. Review, then apply with the `run-migration` skill against sls.
-- Run sls_fix_jmr00015_verify.sql before and after.
--
-- WHAT IT FIXES
--   Jute MR EJM/F/JMR/26-27/00015 (jute_mr_id 28076, co 2 EJM / branch 29)
--   and its transfer-chain parent at Jagrati Trade Services Pvt. Ltd.
--   (jute_mr_id 28165, co 74 / branch 20) carry a bale COUNT of 133 on their
--   active line. The delivery was 70 bales / 10,160 kg. 133 is the bale
--   quantity of the purchase order line (jute_po_li 31266, PO 13314:
--   133 bales / 20,000 kg) and was copied onto the MR line by mistake.
--
--   All weights (actual_weight 10,160, accepted_weight 9,957, challan_weight
--   10,500, rate, total_price, invoice money) are CORRECT and are NOT touched.
--
--   Because the per-bale weight is derived as actual_weight / actual_qty,
--   the 14 jute_issue rows drawn against the EJM line were written at
--   10,160 / 133 = 76.39 kg/bale instead of 10,160 / 70 = 145.142857 kg/bale,
--   so 70 bales were booked out as 5,348 kg instead of 10,160 kg, leaving a
--   phantom 63 bales / 4,812 kg outstanding.
--
-- SECOND DEFECT FIXED IN THE SAME PASS (owner approved 2026-09-22)
--   MR jute_mr_id 28172 (gate entry 22, branch 29, status 13 Pending, no
--   branch_mr_no) is the cause of the two negative rows on the Jute Stock
--   (Production) report for branch 29: item 65 "J TD-6" -61.0 and item 66
--   "J TD-7" -49.0. Its 5 issues were keyed on 2026-09-12 at the then-current
--   line weights (767 kg over 5 bales and 613 kg over 4 bales, i.e. the header
--   NET of 1,380 kg); on 2026-09-18 the lines were reduced to 706 and 564 kg
--   (header actual_weight 1,270 = net 1,380 - variable_shortage 110) and the
--   issues were never re-derived. Statements 7 + 8 re-derive them, which makes
--   both items close at exactly 0.
--
-- STATEMENT SUMMARY (8 statements; 44 row updates over 23 distinct rows)
--   1  jute_mr_li            2 rows   actual_qty      133 -> 70
--   2  jute_mr_li            2 rows   challan_quantity 133 -> 70
--   3  jute_issue           14 rows   weight re-derived at 145.142857 kg/bale
--   4  jute_issue           14 rows   issue_value re-derived
--   5  sales_invoice_jute_dtl 1 row   qty_untit_conversion 133 -> 70
--   6  sales_invoice_dtl      1 row   remarks 'Raw Jute - 133.0' -> '- 70.0'
--   7  jute_issue            5 rows   MR 28172 weight re-derived (141.2 / 141.0)
--   8  jute_issue            5 rows   MR 28172 issue_value re-derived
--   (statements 3+4 and 7+8 hit the same rows twice, hence 44 updates / 23 rows)
--
--   Statements 1 + 3 produce exactly the same result as fixing actual_qty on
--   the MR screen and then pressing "Sync" on the Jute MR Wise Issue Detail
--   page (POST /juteIssue/sync_issue_weights, issue.py:1389); statements 7 + 8
--   are byte-for-byte what that same Sync button writes for MR 28172, which
--   needs no MR-screen edit first. That route is the safer option if you prefer
--   not to run SQL; statements 5 and 6 have no UI equivalent and must be run
--   either way.
--
-- =====================================================================
-- ROLLBACK  (restores every value exactly as it stood on 2026-09-22)
-- =====================================================================
--   UPDATE jute_mr_li SET actual_qty = 133, challan_quantity = 133,
--          updated_date_time = '2026-09-02 21:02:54'
--    WHERE jute_mr_li_id = 45417;
--   UPDATE jute_mr_li SET actual_qty = 133, challan_quantity = 133,
--          updated_date_time = '2026-09-02 21:02:04'
--    WHERE jute_mr_li_id = 45548;
--
--   UPDATE jute_issue SET weight = CASE jute_issue_id
--            WHEN 87590 THEN 153 WHEN 87591 THEN 458 WHEN 87592 THEN 535
--            WHEN 87593 THEN  76 WHEN 87594 THEN 306 WHEN 87595 THEN 382
--            WHEN 87596 THEN 611 WHEN 87597 THEN 917 WHEN 87604 THEN 382
--            WHEN 87605 THEN 764 WHEN 87672 THEN  76 WHEN 87673 THEN  76
--            WHEN 87674 THEN 306 WHEN 87675 THEN 306 END,
--          issue_value = CASE jute_issue_id
--            WHEN 87672 THEN 9766.12 WHEN 87673 THEN 9766.12
--            WHEN 87674 THEN 39321.50 WHEN 87675 THEN 39321.50
--            ELSE 0.00 END,
--          updated_by = 'KGROUND-20260905',
--          update_date_time = CASE jute_issue_id
--            WHEN 87590 THEN '2026-08-31 15:12:30' WHEN 87591 THEN '2026-08-31 15:12:54'
--            WHEN 87592 THEN '2026-08-31 15:13:13' WHEN 87593 THEN '2026-08-31 15:13:55'
--            WHEN 87594 THEN '2026-08-31 15:14:17' WHEN 87595 THEN '2026-08-31 15:14:37'
--            WHEN 87596 THEN '2026-08-31 15:15:14' WHEN 87597 THEN '2026-08-31 15:15:34'
--            WHEN 87604 THEN '2026-09-01 15:59:33' WHEN 87605 THEN '2026-09-01 16:01:48'
--            WHEN 87672 THEN '2026-09-03 15:26:59' WHEN 87673 THEN '2026-09-03 15:27:24'
--            WHEN 87674 THEN '2026-09-03 15:27:50' WHEN 87675 THEN '2026-09-03 15:28:08' END
--    WHERE jute_mr_li_id = 45417;                              -- 14 rows
--
--   UPDATE sales_invoice_jute_dtl SET qty_untit_conversion = 133
--    WHERE sales_invoice_jute_dtl_id = 28907;                  -- 1 row
--   UPDATE sales_invoice_dtl SET remarks = 'Raw Jute - 133.0'
--    WHERE invoice_line_item_id = 28931;                       -- 1 row
--
--   -- statements 7 + 8 (MR 28172)
--   UPDATE jute_issue SET weight = CASE jute_issue_id
--            WHEN 87735 THEN 153 WHEN 87736 THEN 153 WHEN 87737 THEN 153
--            WHEN 87738 THEN 308 WHEN 87739 THEN 613 END,
--          issue_value = CASE jute_issue_id
--            WHEN 87735 THEN 17901.00 WHEN 87736 THEN 17901.00
--            WHEN 87737 THEN 17901.00 WHEN 87738 THEN 36036.00
--            WHEN 87739 THEN 69269.00 END,
--          updated_by = 'system',
--          update_date_time = CASE jute_issue_id
--            WHEN 87735 THEN '2026-09-12 08:09:29' WHEN 87736 THEN '2026-09-12 08:09:57'
--            WHEN 87737 THEN '2026-09-12 08:10:16' WHEN 87738 THEN '2026-09-12 08:10:32'
--            WHEN 87739 THEN '2026-09-12 08:11:12' END
--    WHERE jute_mr_li_id IN (45571, 45572);                    -- 5 rows
-- =====================================================================
--
-- NOT FIXED HERE, ON PURPOSE (see the investigation report):
--   * Jagrati MR 28165 will still show 70 bales / 10,160 kg outstanding in
--     vw_jute_stock_outstanding after this repair, because the forward-sale
--     invoice line (sales_invoice_dtl 28931) has jute_mr_li_id = NULL, so the
--     view's `sold` leg never matches it. That affects ALL 43 Jagrati FY26-27
--     MR lines, not just this one (only branch 87 was ever backfilled). Needs
--     its own decision + migration.
--   * The 15 remaining negative rows in vw_jute_stock_outstanding are all
--     branch 4 (Nellimarla), 2022-23, under +/-10 kg. Untouched.
--   * Jagrati branch 20 keeps 3 negative report rows (items 586 / 630 / 631,
--     legacy quintal-era sales booked against items never received there).
--     Unrelated to this MR; untouched.
-- =====================================================================

START TRANSACTION;

-- ---------------------------------------------------------------------
-- 1. Bale count on the two active MR lines. 133 -> 70.   [2 rows]
--    45417 = EJM   branch 29 MR 28076 line (item 57  "D TD-6")
--    45548 = JTSPL branch 20 MR 28165 line (item 612 "D TD-6")
--    The superseded active=0 lines 45310 / 45547 already hold 70 and are
--    deliberately excluded by the id list.
-- ---------------------------------------------------------------------
UPDATE jute_mr_li
   SET actual_qty        = 70,
       updated_date_time = NOW()
 WHERE jute_mr_li_id IN (45417, 45548)
   AND actual_qty = 133;

-- ---------------------------------------------------------------------
-- 2. Challan bale count on the same two lines. 133 -> 70.   [2 rows]
--    APPROVED by the owner 2026-09-22.
--    Evidence: challan_weight 10,500 / 70 = 150.0 kg per bale, matching the
--    PO's 20,000 / 133 = 150.4; and the superseded QC lines 45310 / 45547
--    recorded challan_quantity = 70.
-- ---------------------------------------------------------------------
UPDATE jute_mr_li
   SET challan_quantity  = 70,
       updated_date_time = NOW()
 WHERE jute_mr_li_id IN (45417, 45548)
   AND challan_quantity = 133;

-- ---------------------------------------------------------------------
-- 3. Issue weight per bale on the 14 issues drawn against EJM line 45417.
--    [14 rows]
--    New weight = ROUND_HALF_UP(quantity * 10160 / 70); the LAST row
--    (87675) absorbs the -1 kg whole-kg residue so the 70 issued bales sum to
--    exactly 10,160 kg and the line closes at zero. These are the identical
--    values POST /juteIssue/sync_issue_weights would write.
--    Bale quantities are already correct and are NOT changed.
--        before: 5,348 kg over 70 bales (76.39 kg/bale)
--        after : 10,160 kg over 70 bales (145.142857 kg/bale)
-- ---------------------------------------------------------------------
UPDATE jute_issue
   SET weight = CASE jute_issue_id
                  WHEN 87590 THEN 290    -- qty  2
                  WHEN 87591 THEN 871    -- qty  6
                  WHEN 87592 THEN 1016   -- qty  7
                  WHEN 87593 THEN 145    -- qty  1
                  WHEN 87594 THEN 581    -- qty  4
                  WHEN 87595 THEN 726    -- qty  5
                  WHEN 87596 THEN 1161   -- qty  8
                  WHEN 87597 THEN 1742   -- qty 12
                  WHEN 87604 THEN 726    -- qty  5
                  WHEN 87605 THEN 1451   -- qty 10
                  WHEN 87672 THEN 145    -- qty  1
                  WHEN 87673 THEN 145    -- qty  1
                  WHEN 87674 THEN 581    -- qty  4
                  WHEN 87675 THEN 580    -- qty  4, closing row (581 - 1 kg residue)
                END,
       updated_by       = 'AI-FIX-JMR00015',
       update_date_time = NOW()
 WHERE jute_mr_li_id = 45417
   AND jute_issue_id IN (87590, 87591, 87592, 87593, 87594, 87595, 87596,
                         87597, 87604, 87605, 87672, 87673, 87674, 87675);

-- ---------------------------------------------------------------------
-- 4. Issue value on the same 14 rows.   [14 rows]
--    APPROVED by the owner 2026-09-22.
--    issue_value = weight / 100 * jute_mr_li.actual_rate (12,850.00), which is
--    what sync_issue_weights does. NOTE: 10 of these rows currently hold
--    issue_value 0.00 (they were entered before the rate reached the line), so
--    this statement raises EJM's booked issue value for MR 15 from 98,175.24
--    to 1,305,560.00 and will move the Batch Cost / "with value" reports.
--    MUST run after statement 3 -- it reads the weight this statement set.
-- ---------------------------------------------------------------------
UPDATE jute_issue
   SET issue_value      = ROUND(weight / 100 * 12850.00, 2),
       updated_by       = 'AI-FIX-JMR00015',
       update_date_time = NOW()
 WHERE jute_mr_li_id = 45417
   AND jute_issue_id IN (87590, 87591, 87592, 87593, 87594, 87595, 87596,
                         87597, 87604, 87605, 87672, 87673, 87674, 87675);

-- ---------------------------------------------------------------------
-- 5. Forward sale Jagrati -> EJM, invoice 17892 (JTSPL/SI/26-27/15):
--    bale count on the priced line. 133 -> 70.   [1 row]
--    The sibling row 28906 (invoice_line_item_id 28930) is the zero-value
--    mirror of the superseded MR line and already holds 70.
--    Money on the invoice is weight-based (9,957 kg @ 129.14 = 1,285,846.98)
--    and is NOT touched.
-- ---------------------------------------------------------------------
UPDATE sales_invoice_jute_dtl
   SET qty_untit_conversion = 70
 WHERE sales_invoice_jute_dtl_id = 28907
   AND invoice_line_item_id = 28931
   AND qty_untit_conversion = 133;

-- ---------------------------------------------------------------------
-- 6. Same invoice line, the printed bale count in the description. [1 row]
-- ---------------------------------------------------------------------
UPDATE sales_invoice_dtl
   SET remarks = 'Raw Jute - 70.0'
 WHERE invoice_line_item_id = 28931
   AND invoice_id = 17892
   AND remarks = 'Raw Jute - 133.0';

-- =====================================================================
-- SECOND DEFECT: MR 28172 (branch 29, gate entry 22, status 13 Pending).
-- Nothing on the MR itself is wrong -- lines 45571 (item 65 "J TD-6",
-- 5 bales / 706 kg) and 45572 (item 66 "J TD-7", 4 bales / 564 kg) are
-- correct. Only the 5 issues are stale, at the pre-2026-09-18 line weights.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 7. Re-derive issue weight for the 5 issues of MR 28172.   [5 rows]
--    Line 45571: unit = 706 / 5 = 141.2 kg/bale
--        1 bale -> ROUND_HALF_UP(141.2) = 141   (x3 rows)
--        2 bales -> ROUND_HALF_UP(282.4) = 282, then 87735..87738 are all 5
--        bales of the line (fully issued), so the LAST row closes it:
--        282 + (706 - 705) = 283.  Line total 706 -> bal_weight 0.
--    Line 45572: unit = 564 / 4 = 141.0 kg/bale
--        4 bales -> 564, single row, already exact. Line total 564 -> bal 0.
--    Bale quantities are correct and are NOT changed (sum_q == actual_qty on
--    both lines, so sync's over-issue trim does not fire).
--        before: 767 + 613 = 1,380 kg (the stale header NET)
--        after : 706 + 564 = 1,270 kg (the header actual_weight)
-- ---------------------------------------------------------------------
UPDATE jute_issue
   SET weight = CASE jute_issue_id
                  WHEN 87735 THEN 141    -- line 45571, qty 1
                  WHEN 87736 THEN 141    -- line 45571, qty 1
                  WHEN 87737 THEN 141    -- line 45571, qty 1
                  WHEN 87738 THEN 283    -- line 45571, qty 2, closing row (282 + 1 kg residue)
                  WHEN 87739 THEN 564    -- line 45572, qty 4, closing row (exact)
                END,
       updated_by       = 'AI-FIX-JMR00015',
       update_date_time = NOW()
 WHERE jute_mr_li_id IN (45571, 45572)
   AND jute_issue_id IN (87735, 87736, 87737, 87738, 87739);

-- ---------------------------------------------------------------------
-- 8. Re-derive issue value on the same 5 rows.   [5 rows]
--    issue_value = weight / 100 * jute_mr_li.actual_rate, per line:
--      45571 -> 11,700.00   (141 -> 16,497.00 ; 283 -> 33,111.00)
--      45572 -> 11,300.00   (564 -> 63,732.00)
--    Unlike statement 4 these rows already carried the right formula at the
--    old weights (153/100*11700 = 17,901.00), so this is a pure restatement:
--    line total 89,739.00 -> 82,602.00 and 69,269.00 -> 63,732.00.
--    MUST run after statement 7 -- it reads the weight that statement set.
-- ---------------------------------------------------------------------
UPDATE jute_issue
   SET issue_value      = ROUND(weight / 100 * CASE jute_mr_li_id
                                                 WHEN 45571 THEN 11700.00
                                                 WHEN 45572 THEN 11300.00
                                               END, 2),
       updated_by       = 'AI-FIX-JMR00015',
       update_date_time = NOW()
 WHERE jute_mr_li_id IN (45571, 45572)
   AND jute_issue_id IN (87735, 87736, 87737, 87738, 87739);

-- =====================================================================
-- Expected rows matched, in order: 2, 2, 14, 14, 1, 1, 5, 5  (= 44).
-- ANY other number on ANY statement -> ROLLBACK, do not COMMIT, re-run the
-- verify file and re-investigate: the guarded WHEREs (= 133, = 'Raw Jute -
-- 133.0') silently match 0 rows if someone already changed the data.
-- Re-run sls_fix_jmr00015_verify.sql, then:
-- =====================================================================
COMMIT;
