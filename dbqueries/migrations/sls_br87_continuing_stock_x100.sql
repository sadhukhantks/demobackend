-- ============================================================================
-- sls branch 87 — convert the CONTINUING legacy jute stock from quintal to kg (x100)
-- ============================================================================
-- Target DB   : sls ONLY (Limelight Commercial Pvt. Ltd., co_id 106, branch 87)
-- Date        : 2026-08-23   User ruling: "multiply by 100 ... for the values that are
--               to continue"
-- Run order   : after sls_br87_stock_asof_20260329_adjust.sql (applied 2026-08-23)
--
-- WHY
-- The new system stores weights in KG (MR page rounds to whole kg, issue value =
-- weight/100 x rate, every label says kg). Rows migrated from the old system are in
-- QUINTAL as-is. The branch-87 lines that still hold stock after the 29-Mar reset are
-- all migrated (quintal) lines, so every future kg issue against them would net wrongly
-- and the picker/reports show them 100x too small. This converts exactly those MRs —
-- the 16 sheet MRs plus MR 27952 / 27953 (31-Mar receipts, still open) — so the
-- continuing stock is in one unit with what the tenant enters from now on. Zeroed
-- legacy history elsewhere stays quintal (it nets to 0 and is history only).
--
-- SCOPE (measured before running)
--   MRs: 24002, 27504, 27597, 27637, 27685, 27697, 27758, 27815, 27820, 27923, 27932,
--        27937, 27938, 27949, 27950, 27951, 27952, 27953
--   jute_mr_li  : 23 lines (all active) — actual_weight, challan_weight  (accepted NULL)
--   jute_mr     : 18 headers — net_weight, gross_weight, tare_weight, actual_weight,
--                 mr_weight, challan_weight  (variable_shortage 0, total_amount UNCHANGED)
--   jute_issue  : 90 rows on those lines — weight only: 49 legacy <= 29-Mar (952.22 qtl),
--                 9 dated 30/31-Mar incl. the 4 re-pointed (183.29), 12 April-2026 rows
--                 keyed in quintal by the app (192.61 — after 31-Mar but they live on
--                 these lines and MUST scale with them), 20 STOCKADJ-20260329 rows (139.49)
--   Not touched : issue_value, rate, total_amount, quantity/bales/drums, any sales
--                 (none linked to these lines), any PO (none linked), other MRs.
--   Result      : every balance on these lines x100: sheet line 27932 reads 11,800 kg as
--                 on 29-Mar and 10,120 kg today; 27952 7,070 kg; 27953 7,956 kg; etc.
--
-- ONE-SHOT — NOT RERUNNABLE. Sentinel: jute_mr_li 45007 (MR 27932) actual_weight is
-- 120.4 before and 12,040 after; if it is already > 1,000 the PREPARE below fails
-- (syntax error) and tools/run_sql_migration.py rolls the whole transaction back.
--
-- ROLLBACK (exact inverse, same sets; only if the sentinel reads > 1,000):
--   UPDATE jute_mr_li SET actual_weight = actual_weight/100, challan_weight = challan_weight/100 WHERE jute_mr_id IN (<MR list>);
--   UPDATE jute_mr SET net_weight=net_weight/100, gross_weight=gross_weight/100, tare_weight=tare_weight/100,
--          actual_weight=actual_weight/100, mr_weight=mr_weight/100, challan_weight=challan_weight/100 WHERE jute_mr_id IN (<MR list>);
--   UPDATE jute_issue ji JOIN jute_mr_li li ON li.jute_mr_li_id=ji.jute_mr_li_id SET ji.weight=ji.weight/100 WHERE li.jute_mr_id IN (<MR list>);
-- ============================================================================

SET @already := (SELECT actual_weight > 1000 FROM jute_mr_li WHERE jute_mr_li_id = 45007);
SET @guard := IF(@already, 'ABORT: continuing-stock x100 already applied on branch 87', 'SELECT 1');
PREPARE guard_stmt FROM @guard;
EXECUTE guard_stmt;
DEALLOCATE PREPARE guard_stmt;

UPDATE jute_mr_li
SET actual_weight  = actual_weight  * 100,
    challan_weight = challan_weight * 100
WHERE jute_mr_id IN (24002, 27504, 27597, 27637, 27685, 27697, 27758, 27815, 27820,
                     27923, 27932, 27937, 27938, 27949, 27950, 27951, 27952, 27953);

UPDATE jute_mr
SET net_weight     = net_weight     * 100,
    gross_weight   = gross_weight   * 100,
    tare_weight    = tare_weight    * 100,
    actual_weight  = actual_weight  * 100,
    mr_weight      = mr_weight      * 100,
    challan_weight = challan_weight * 100
WHERE branch_id = 87
  AND jute_mr_id IN (24002, 27504, 27597, 27637, 27685, 27697, 27758, 27815, 27820,
                     27923, 27932, 27937, 27938, 27949, 27950, 27951, 27952, 27953);

UPDATE jute_issue ji
JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
SET ji.weight = ji.weight * 100
WHERE li.jute_mr_id IN (24002, 27504, 27597, 27637, 27685, 27697, 27758, 27815, 27820,
                        27923, 27932, 27937, 27938, 27949, 27950, 27951, 27952, 27953);

-- ---------------------------------------------------------------------------
-- Verification (expect 23 / 18 / 90 rows updated; then:)
-- SELECT jm.jute_mr_id, li.actual_quality, ROUND(v.bal_qty,2), ROUND(v.bal_weight,2) kg
-- FROM vw_jute_stock_outstanding v JOIN jute_mr_li li ON li.jute_mr_li_id=v.jute_mr_li_id
-- JOIN jute_mr jm ON jm.jute_mr_id=li.jute_mr_id WHERE v.branch_id=87
--   AND COALESCE(jm.out_date,jm.jute_mr_date,jm.jute_gate_entry_date)<='2026-03-31'
--   AND (ROUND(v.bal_qty,2)<>0 OR ROUND(v.bal_weight,2)<>0) ORDER BY jm.jute_mr_id;
-- expect the same rows as before x100 in weight (e.g. 27932 74 / 10,120.00; 27952 139 / 7,070.00)
