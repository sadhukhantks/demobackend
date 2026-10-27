-- ============================================================================
-- sls branch 87 — MR 27938 / line 45014: actual_qty 1 -> 73 (BALE)
-- ============================================================================
-- Target DB : sls ONLY (Limelight co 106, branch 87 FACTORY)
-- Date      : 2026-09-05   User: "actual quantity mentioned is 1 but it should be
--             73 as per the old db"
-- Tag       : jute_mr_li.updated_date_time / jute_issue.update_date_time = NOW();
--             nothing else identifies the rows (ids are fixed below).
--
-- ROOT CAUSE
--   vowsls.scm_mr_line_item 45014 carries actual_bale = 73 AND actual_loose = 1
--   (a stray drum); the importer keyed jute_mr_li.actual_qty off the loose count.
--   Tenant-wide scan of every migrated MR line (ids == vowsls ids, received
--   <= 2026-03-31, active, MR not 4/6/21) finds 45014 as the ONLY BALE line whose
--   sls qty equals old loose while old bales > 0 — a one-off, no sibling repair.
--   Weight was never wrong: actual_weight 10,650 kg = old mill 106.5 qtl;
--   old accepted 105.95 qtl; bill pass 15,68,060 = 10,650 x 148/qtl (unaffected).
--
-- WHAT ELSE SITS ON THIS LINE (all stay; only one quantity is re-based)
--   87092  2026-03-29  STOCKADJ-20260329   qty -73  wt   +55   <- written to lift the
--          bale count 1 -> 74 (sheet: 73 bales + 1 drum) as on 29-Mar. With
--          actual_qty = 73 that offset is no longer needed: qty -73 -> 0. Weight +55
--          (mill 10,650 -> accepted 10,595) and value 8,140 unchanged.
--   87599  2026-03-29  STOCKADJ2-20260329  qty -35  wt -4,945  <- offsets the two
--          30-Mar issues so the 31-Mar closing matches the old portal. Unchanged.
--   81777  2026-03-30  23 bales / 3,225 kg  (re-pointed here 08-23)  Unchanged.
--   81778  2026-03-30  12 bales / 1,720 kg  (re-pointed here 08-23)  Unchanged.
--   No transfers: jute_mr.src_jute_mr_id children = 0, jute_lot_src = 0,
--   sales_invoice_dtl.jute_mr_li_id links = 0.
--
-- RESULT (vw_jute_stock_outstanding, line 45014)
--   before: actual_qty 1,  bal_qty 74, bal_weight 10,595
--   after : actual_qty 73, bal_qty 73, bal_weight 10,595   (weight ledger untouched;
--           31-Mar closing stays 103,410 kg; old system shows the same 73 bales /
--           105.95 qtl consumed by its Apr-May issues 82030/81934/81938/82102)
--   The "74" of the 29-Mar sheet counted the stray drum; user ruling 2026-09-05 = 73.
--
-- WHY IT MATTERED (actual_qty is the per-bale divisor)
--   * POST /juteIssue/sync_issue_weights: weight = qty x actual_weight/actual_qty
--     -> with 1 it would rewrite 81777 to 23 x 10,650 = 244,950 kg.
--   * juteIssue edit page: weight auto-fill per bale = 10,650 kg.
--   * MR-wise issue detail: unit_weight 10,650 / expected_issued_weight 372,750.
--   * juteTransfer: take fraction scales actual_qty (lot_ops / warehouse_stock_ops),
--     invoice remark "Raw Jute - 1 BALE" (transfer.py:1062/1098).
--   * Stock / batch-cost / issue-planner reports: received 1 bale, balance 74.
--
-- ROLLBACK
--   UPDATE jute_mr_li SET actual_qty = 1  WHERE jute_mr_li_id = 45014 AND actual_qty = 73;
--   UPDATE jute_issue SET quantity = -73  WHERE jute_issue_id = 87092 AND quantity = 0
--     AND jute_mr_li_id = 45014;
-- ============================================================================

-- 1. the MR line (guarded on the wrong value; rerunnable)
UPDATE jute_mr_li
SET actual_qty = 73, updated_date_time = NOW()
WHERE jute_mr_li_id = 45014 AND jute_mr_id = 27938
  AND actual_qty = 1 AND actual_weight = 10650 AND active = 1;

-- 2. re-base the 29-Mar bale-count adjustment (keep its STOCKADJ tag = its batch
--    rollback handle; only the quantity changes)
UPDATE jute_issue
SET quantity = 0, update_date_time = NOW()
WHERE jute_issue_id = 87092 AND jute_mr_li_id = 45014
  AND updated_by = 'STOCKADJ-20260329' AND branch_id = 87
  AND quantity = -73 AND weight = 55;
