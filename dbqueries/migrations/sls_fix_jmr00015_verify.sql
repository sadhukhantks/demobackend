-- =====================================================================
-- sls_fix_jmr00015_verify.sql
-- Tenant: sls          Author: data investigation, 2026-09-22
-- READ-ONLY. Run BEFORE and AFTER sls_fix_jmr00015_repair.sql.
--
-- Subject: jute MR  EJM/F/JMR/26-27/00015  = jute_mr.jute_mr_id 28076
--          (co 2 THE EMPIRE JUTE COMPANY LTD., branch 29 FACTORY,
--           branch_mr_no 15, jute_mr_date 2026-08-27, status 3 Approved)
--          and its transfer-chain parent
--          jute_mr.jute_mr_id 28165 (co 74 Jagrati Trade Services Pvt. Ltd.,
--           branch 20 FACTORY, branch_mr_no 15, src_jute_mr_id = 28076)
--          and the forward-sale invoice 17892 (JTSPL/SI/26-27/15,
--           branch 20, invoice_type 5, Jagrati -> EJM).
--
-- Defect: the bale COUNT on the active MR line is 133; the true count is 70.
--         133 is the bale quantity of jute_po_li 31266 (PO 13314: 133 bales /
--         20,000 kg); this delivery was 70 bales / 10,160 kg. The weights are
--         correct and are NOT touched.
--
-- Second subject (blocks 9b-9e): jute_mr.jute_mr_id 28172 (branch 29, gate
--         entry 22, status 13), whose 5 stale issues are the sole cause of the
--         branch-29 negative stock on items 65 "J TD-6" (-61) and 66 "J TD-7"
--         (-49). Its MR lines are correct; only jute_issue is repaired.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. MR headers in the chain
-- ---------------------------------------------------------------------
SELECT jm.jute_mr_id, jm.branch_id, b.co_id, c.co_prefix, b.branch_prefix,
       jm.branch_mr_no, jm.jute_mr_date, jm.out_date, jm.status_id,
       jm.transfer_mode, jm.src_jute_mr_id, jm.po_id,
       jm.challan_no, jm.challan_weight, jm.gross_weight, jm.tare_weight,
       jm.net_weight, jm.variable_shortage, jm.actual_weight, jm.mr_weight,
       jm.total_amount, jm.invoice_no
FROM jute_mr jm
JOIN branch_mst b ON b.branch_id = jm.branch_id
JOIN co_mst     c ON c.co_id     = b.co_id
WHERE jm.jute_mr_id IN (28076, 28165);

-- ---------------------------------------------------------------------
-- 2. Every line of both MRs.  Expect BEFORE: 45417 / 45548 actual_qty =
--    challan_quantity = 133.  AFTER: both = 70.
--    (45310 / 45547 are the superseded active=0 lines and already say 70 --
--     they are NOT touched.)
-- ---------------------------------------------------------------------
SELECT jml.jute_mr_li_id, jml.jute_mr_id, jml.active, jml.jute_po_li_id,
       jml.actual_item_id, jml.warehouse_id,
       jml.actual_qty, jml.challan_quantity,
       jml.actual_weight, jml.challan_weight,
       jml.allowable_moisture, jml.actual_moisture, jml.shortage_kgs,
       jml.accepted_weight, jml.rate, jml.actual_rate, jml.total_price,
       ROUND(jml.actual_weight / NULLIF(jml.actual_qty, 0), 4) AS kg_per_bale,
       jml.updated_date_time
FROM jute_mr_li jml
WHERE jml.jute_mr_id IN (28076, 28165)
ORDER BY jml.jute_mr_id, jml.jute_mr_li_id;

-- ---------------------------------------------------------------------
-- 3. The PO the 133 was copied from (context only, never modified)
-- ---------------------------------------------------------------------
SELECT jp.jute_po_id, jp.po_no, jp.po_date, jp.branch_id, jp.jute_uom,
       jp.weight AS po_weight_kg, jp.jute_po_value,
       jpl.jute_po_li_id, jpl.item_id, jpl.quantity AS po_bales, jpl.rate,
       ROUND(jp.weight / NULLIF(jpl.quantity, 0), 2) AS po_kg_per_bale
FROM jute_po jp
JOIN jute_po_li jpl ON jpl.jute_po_id = jp.jute_po_id
WHERE jp.jute_po_id = 13314;

-- ---------------------------------------------------------------------
-- 4. The 14 jute_issue rows drawn against EJM line 45417.
--    BEFORE: SUM(quantity) = 70 bales, SUM(weight) = 5,348 kg (76.4 kg/bale,
--            i.e. 10,160 / 133).
--    AFTER : SUM(quantity) = 70 bales, SUM(weight) = 10,160 kg
--            (145.142857 kg/bale = 10,160 / 70, last row absorbs the residue).
-- ---------------------------------------------------------------------
SELECT ji.jute_issue_id, ji.jute_mr_li_id, ji.issue_date, ji.status_id,
       ji.yarn_type_id, ji.quantity, ji.weight,
       ROUND(ji.weight / NULLIF(ji.quantity, 0), 4) AS kg_per_bale,
       ji.issue_value, ji.updated_by, ji.update_date_time
FROM jute_issue ji
WHERE ji.jute_mr_li_id = 45417
ORDER BY ji.jute_issue_id;

SELECT COUNT(*)              AS issue_rows,
       SUM(ji.quantity)      AS total_bales,
       ROUND(SUM(ji.weight), 3) AS total_weight_kg,
       ROUND(SUM(ji.issue_value), 2) AS total_issue_value
FROM jute_issue ji
WHERE ji.jute_mr_li_id = 45417
  AND ji.status_id <> 4;

-- ---------------------------------------------------------------------
-- 5. The forward sale: Jagrati -> EJM, invoice 17892.
--    The bale count lives in sales_invoice_jute_dtl.qty_untit_conversion
--    and (as text) in sales_invoice_dtl.remarks.
--    BEFORE: 133 / 'Raw Jute - 133.0'.  AFTER: 70 / 'Raw Jute - 70.0'.
--    Money columns (quantity = 9,957 kg, rate 129.14, amount 1,285,846.98)
--    are weight-based and must NOT change.
-- ---------------------------------------------------------------------
SELECT si.invoice_id, si.invoice_no, si.invoice_date, si.branch_id,
       si.invoice_type, si.status_id, si.active, si.invoice_amount,
       sij.mr_id AS jute_mr_id_on_invoice, sij.unit_conversion,
       sid.invoice_line_item_id, sid.item_id, sid.quantity AS qty_kg,
       sid.sales_weight, sid.rate, sid.total_amount, sid.remarks,
       sid.jute_mr_li_id,
       sijd.sales_invoice_jute_dtl_id, sijd.qty_untit_conversion AS bales
FROM sales_invoice si
JOIN sales_invoice_dtl sid  ON sid.invoice_id = si.invoice_id
LEFT JOIN sales_invoice_jute sij      ON sij.invoice_id = si.invoice_id
LEFT JOIN sales_invoice_jute_dtl sijd ON sijd.invoice_line_item_id = sid.invoice_line_item_id
WHERE si.invoice_id = 17892
ORDER BY sid.invoice_line_item_id;

-- ---------------------------------------------------------------------
-- 6. The outstanding figure the owner is looking at.
--    Feeds: Jute MR In Stock, Jute MR Wise, Godown Wise Stock,
--           Jute MR Wise Issue Detail, MR Movement Detail, Jute Issue picker.
--    BEFORE  45417 (EJM) : actual_qty 133, bal_qty 63, bal_weight 4,812,
--                          bal_accepted_weight 4,716.474
--            45548 (JTSPL): actual_qty 133, bal_qty 133, bal_weight 10,160
--    AFTER   45417 (EJM) : actual_qty 70,  bal_qty 0,   bal_weight 0,
--                          bal_accepted_weight 0
--            45548 (JTSPL): actual_qty 70,  bal_qty 70,  bal_weight 10,160
--                          -- still outstanding, see NOTE in the repair file
-- ---------------------------------------------------------------------
SELECT v.jute_mr_li_id, v.branch_id, v.branch_mr_no, v.warehouse_name,
       v.actual_item_id, v.actual_qty, v.actual_weight, v.accepted_weight,
       v.bal_qty, v.bal_weight, v.bal_accepted_weight,
       v.sold_qty, v.sold_weight, v.mr_status_id
FROM vw_jute_stock_outstanding v
WHERE v.jute_mr_li_id IN (45417, 45548);

-- ---------------------------------------------------------------------
-- 7. Per-bale weight as the "Jute MR Wise Issue Detail" report computes it
--    (reportQueries.py:1594 unit_weight, :1626 expected_issued_weight).
--    BEFORE: unit_weight 76.3910, expected 5,347.37, issued 5,348, diff  0.63
--    AFTER : unit_weight 145.1429, expected 10,160.00, issued 10,160, diff 0.00
-- ---------------------------------------------------------------------
SELECT l.jute_mr_li_id,
       l.actual_qty, l.actual_weight,
       ROUND(l.actual_weight / NULLIF(l.actual_qty, 0), 4) AS unit_weight,
       ROUND(SUM(ji.weight), 3)                            AS total_issued_weight,
       ROUND(SUM(ji.quantity * l.actual_weight / NULLIF(l.actual_qty, 0)), 3)
                                                           AS expected_issued_weight,
       ROUND(SUM(ji.weight)
             - SUM(ji.quantity * l.actual_weight / NULLIF(l.actual_qty, 0)), 3)
                                                           AS weight_diff
FROM jute_mr_li l
JOIN jute_issue ji ON ji.jute_mr_li_id = l.jute_mr_li_id AND ji.status_id <> 4
WHERE l.jute_mr_li_id IN (45417, 45548)
GROUP BY l.jute_mr_li_id, l.actual_qty, l.actual_weight;

-- ---------------------------------------------------------------------
-- 8. Jute Stock report impact, EJM branch 29, item 57 "D TD-6".
--    ACTUAL basis  : receipts 173,961  issues 164,949  closing  9,012
--                 -> after repair      issues 169,761  closing  4,200
--    ACCOUNTS basis: closing 4,715.855 -> after repair  0.000 exactly, so the
--                    item-57 row then vanishes from the Jute Stock (Accounts)
--                    page (all-zero rows are suppressed): 10 rows -> 9 rows.
--    (The stock report has no bale column, so 133 itself never appears on it;
--     it moves only because the issue WEIGHTS move.)
-- ---------------------------------------------------------------------
SELECT 'receipt' AS side,
       ROUND(SUM(jml.actual_weight), 3)                                 AS wt_actual_basis,
       ROUND(SUM(COALESCE(jml.accepted_weight, jml.actual_weight, 0)), 3) AS wt_accounts_basis,
       SUM(jml.actual_qty)                                              AS qty
FROM jute_mr jm
JOIN jute_mr_li jml ON jml.jute_mr_id = jm.jute_mr_id
WHERE jm.branch_id = 29
  AND jml.actual_item_id = 57
  AND jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'))
  AND COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date) >= '2026-04-01'
UNION ALL
SELECT 'issue',
       ROUND(SUM(ji.weight), 3),
       ROUND(SUM(ji.weight
                 * COALESCE(COALESCE(mrli.accepted_weight, mrli.actual_weight, 0)
                            / NULLIF(mrli.actual_weight, 0), 1)), 3),
       SUM(ji.quantity)
FROM jute_issue ji
JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
JOIN jute_mr jm      ON jm.jute_mr_id      = mrli.jute_mr_id
WHERE ji.branch_id = 29
  AND mrli.actual_item_id = 57
  AND ji.status_id <> 4
  AND jm.status_id NOT IN (4, 6, 21, 48)
  AND mrli.active = 1
  AND (mrli.status IS NULL OR mrli.status NOT IN ('4', '6'))
  AND ji.issue_date >= '2026-04-01';

-- ---------------------------------------------------------------------
-- 9. Negative stock rows, all branches, from the balance view.
--    BEFORE: 17 rows -- 15 at branch 4 (Nellimarla, 2022-23, +/-10 kg legacy)
--            plus 45571 and 45572 at branch 29.
--    AFTER : 15 rows, ALL branch 4. No branch 29 row, no branch 20 row.
-- ---------------------------------------------------------------------
SELECT v.jute_mr_li_id, v.branch_id, v.branch_mr_no, v.inward_date,
       v.actual_item_id, v.actual_qty, v.actual_weight, v.accepted_weight,
       v.bal_qty, v.bal_weight, v.bal_accepted_weight, v.mr_status_id
FROM vw_jute_stock_outstanding v
WHERE v.bal_qty < -0.001
   OR v.bal_weight < -0.001
   OR v.bal_accepted_weight < -0.001
ORDER BY v.branch_id, v.jute_mr_li_id;

-- ---------------------------------------------------------------------
-- 9b. THE ACCEPTANCE CHECK for branch 29.
--     BEFORE: 2 rows (45571 item 65 bal_weight -61, 45572 item 66 bal -49).
--     AFTER : 0 rows.  <-- this query must return NOTHING.
-- ---------------------------------------------------------------------
SELECT v.jute_mr_li_id, v.branch_id, v.actual_item_id,
       v.actual_qty, v.actual_weight,
       v.bal_qty, v.bal_weight, v.bal_accepted_weight
FROM vw_jute_stock_outstanding v
WHERE v.branch_id = 29
  AND (v.bal_qty < -0.001
    OR v.bal_weight < -0.001
    OR v.bal_accepted_weight < -0.001)
ORDER BY v.jute_mr_li_id;

-- =====================================================================
-- MR 28172 -- the second defect (branch 29, gate entry 22, status 13).
-- Lines 45571 (item 65 "J TD-6") and 45572 (item 66 "J TD-7") are correct;
-- their 5 issues are stale at the pre-2026-09-18 line weights.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 9c. MR 28172 header + lines. Nothing here changes -- this block exists to
--     prove the line weights are the ones the issues must be re-derived from.
--     Expect (both before and after):
--       header  net_weight 1,380  variable_shortage 110  actual_weight 1,270
--       45571   item 65  actual_qty 5  actual_weight 706  actual_rate 11,700
--               -> 141.2 kg/bale
--       45572   item 66  actual_qty 4  actual_weight 564  actual_rate 11,300
--               -> 141.0 kg/bale
-- ---------------------------------------------------------------------
SELECT jm.jute_mr_id, jm.branch_id, jm.jute_gate_entry_no, jm.branch_mr_no,
       jm.jute_mr_date, jm.out_date, jm.status_id,
       jm.net_weight, jm.variable_shortage, jm.actual_weight, jm.challan_weight
FROM jute_mr jm
WHERE jm.jute_mr_id = 28172;

SELECT jml.jute_mr_li_id, jml.active, jml.actual_item_id,
       jml.actual_qty, jml.actual_weight, jml.accepted_weight,
       jml.rate, jml.actual_rate,
       ROUND(jml.actual_weight / NULLIF(jml.actual_qty, 0), 4) AS kg_per_bale,
       jml.updated_date_time
FROM jute_mr_li jml
WHERE jml.jute_mr_id = 28172
ORDER BY jml.jute_mr_li_id;

-- ---------------------------------------------------------------------
-- 9d. The 5 issues of MR 28172, before/after.
--     BEFORE            AFTER
--       87735  1 bale  153  17,901.00     141  16,497.00
--       87736  1 bale  153  17,901.00     141  16,497.00
--       87737  1 bale  153  17,901.00     141  16,497.00
--       87738  2 bales 308  36,036.00     283  33,111.00   <- closing row
--       87739  4 bales 613  69,269.00     564  63,732.00   <- closing row
--     line 45571 total  767 kg / 89,739.00  ->  706 kg / 82,602.00
--     line 45572 total  613 kg / 69,269.00  ->  564 kg / 63,732.00
--     Bale quantities (1,1,1,2 and 4) must NOT change.
-- ---------------------------------------------------------------------
SELECT ji.jute_issue_id, ji.jute_mr_li_id, ji.item_id, ji.issue_date,
       ji.status_id, ji.yarn_type_id, ji.quantity, ji.weight,
       ROUND(ji.weight / NULLIF(ji.quantity, 0), 4) AS kg_per_bale,
       ji.issue_value, ji.updated_by, ji.update_date_time
FROM jute_issue ji
WHERE ji.jute_mr_li_id IN (45571, 45572)
ORDER BY ji.jute_mr_li_id, ji.jute_issue_id;

SELECT ji.jute_mr_li_id,
       COUNT(*)                      AS issue_rows,
       SUM(ji.quantity)              AS total_bales,
       ROUND(SUM(ji.weight), 3)      AS total_weight_kg,
       ROUND(SUM(ji.issue_value), 2) AS total_issue_value
FROM jute_issue ji
WHERE ji.jute_mr_li_id IN (45571, 45572)
  AND ji.status_id <> 4
GROUP BY ji.jute_mr_li_id;

-- ---------------------------------------------------------------------
-- 9e. Jute Stock report arithmetic for branch 29 items 65 and 66.
--     BEFORE: item 65 receipts 39,359.25 issues 39,420.25 closing -61.00
--             item 66 receipts 38,661.06 issues 38,710.06 closing -49.00
--     AFTER : both receipts unchanged, issues fall by 61 / 49, closing 0.000
--             -> both rows then vanish from the report entirely, because the
--                report suppresses rows whose opening/receipt/issue/sold/
--                closing are all zero (reportQueries.py:326-332). Branch 29
--                actual basis goes 13 rows -> 11 rows.
-- ---------------------------------------------------------------------
SELECT jml.actual_item_id AS item_id,
       'receipt'                        AS side,
       ROUND(SUM(jml.actual_weight), 3) AS weight,
       SUM(jml.actual_qty)              AS qty
FROM jute_mr jm
JOIN jute_mr_li jml ON jml.jute_mr_id = jm.jute_mr_id
WHERE jm.branch_id = 29
  AND jml.actual_item_id IN (65, 66)
  AND jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'))
GROUP BY jml.actual_item_id
UNION ALL
SELECT mrli.actual_item_id,
       'issue',
       ROUND(SUM(ji.weight), 3),
       SUM(ji.quantity)
FROM jute_issue ji
JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
JOIN jute_mr jm      ON jm.jute_mr_id      = mrli.jute_mr_id
WHERE ji.branch_id = 29
  AND mrli.actual_item_id IN (65, 66)
  AND ji.status_id <> 4
  AND jm.status_id NOT IN (4, 6, 21, 48)
  AND mrli.active = 1
  AND (mrli.status IS NULL OR mrli.status NOT IN ('4', '6'))
GROUP BY mrli.actual_item_id
ORDER BY item_id, side;

-- ---------------------------------------------------------------------
-- 10. Sizing query for the same defect elsewhere: an active MR line whose
--     bale count was overwritten while a superseded (active=0) sibling line
--     for the same item still carries the delivered count.
--     BEFORE: 10 rows, of which 45417 + 45548 are this MR.
-- ---------------------------------------------------------------------
SELECT jm.jute_mr_id, jm.branch_id, jm.branch_mr_no, jm.jute_mr_date,
       jm.unit_conversion,
       a.jute_mr_li_id AS active_li,  a.actual_qty AS active_qty,
       a.actual_weight,
       i.jute_mr_li_id AS inactive_li, i.actual_qty AS inactive_qty,
       ROUND(a.actual_weight / NULLIF(a.actual_qty, 0), 1) AS kg_per_unit_now,
       ROUND(a.actual_weight / NULLIF(i.actual_qty, 0), 1) AS kg_per_unit_if_fixed
FROM jute_mr_li a
JOIN jute_mr_li i ON i.jute_mr_id      = a.jute_mr_id
                 AND i.active          = 0
                 AND i.actual_item_id  = a.actual_item_id
JOIN jute_mr jm   ON jm.jute_mr_id     = a.jute_mr_id
WHERE a.active = 1
  AND a.actual_qty <> i.actual_qty
  AND i.actual_qty > 0
ORDER BY jm.jute_mr_date DESC;
