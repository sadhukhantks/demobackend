-- =============================================================================
-- TKT-000187 — Winding & Weaving Difference: purchase yarn reads 0 (sls, co 2 / FACTORY 29)
-- =============================================================================
-- Date: 2026-09-23   TARGET: sls   SELECT-ONLY — nothing here writes.
--
-- The report's purchase_mt comes from get_yarn_purchase_qty_query
-- (src/juteProduction/winding_stock_query.py): approved store receipts
-- (proc_inward.sr_status = 3) of items in an item group with item_type_id = 4
-- (yarn), scoped ig.co_id = :co_id AND pi.branch_id = :branch_id, converted
-- by uom (MT/MTS -> MT, KG -> /1000, anything else refused).
--
-- FINDINGS (2026-09-23): purchased yarn on sls satisfies NONE of those:
--   * it is booked as item 334018 'spools/yarn', item group 2455
--     'MISC EXPNS (STORES TRANS/SERV/LAB/SUNDRIES/TSTG)', item_type_id = 1,
--     belonging to company 106 — not a yarn group, not company 2;
--   * every receipt is at branch 87 (co 106 'FACTORY'), never branch 29;
--   * 27 of the 31 receipts since May 2026 carry uom 'MTR' (metre — refused
--     as a length), 4 carry 'MT'. The quantities (8-26 per receipt, 60.3 on
--     19-Sep, 26.7 on 23-Sep) are truckloads of tonnes, so 'MTR' is a wrong
--     uom pick, not metres.
--   The physical count (jute_prod_winding_stock bucket PURCHASE, branch 29)
--   shows purchased yarn '13 LBS SKWP' (item 335556) arriving 20-Sep (9.0 MT)
--   after the 19-Sep receipts, so the mill does count it in winding stock —
--   the book side never sees the inflow.
--
-- 1. Receipts of the purchase item, with the three mismatches visible
SELECT pi.inward_id, pi.inward_date, pi.branch_id, pi.sr_status,
       pid.item_id, im.item_name, ig.item_grp_name, ig.item_type_id, ig.co_id AS grp_co,
       um.uom_name, pid.approved_qty
FROM proc_inward pi
JOIN proc_inward_dtl pid ON pid.inward_id = pi.inward_id
JOIN item_mst im        ON im.item_id = pid.item_id
JOIN item_grp_mst ig    ON ig.item_grp_id = im.item_grp_id
LEFT JOIN uom_mst um    ON um.uom_id = pid.uom_id
WHERE pid.item_id = 334018 AND pi.inward_date >= '2026-08-01'
ORDER BY pi.inward_date;

-- 2. What the report's own filter finds for co 2 / branch 29 (expected: 0 / NULL)
SELECT COALESCE(SUM(pid.approved_qty), 0) AS purchase_qty,
       GROUP_CONCAT(DISTINCT um.uom_name) AS uoms
FROM proc_inward_dtl pid
JOIN proc_inward pi  ON pi.inward_id = pid.inward_id
JOIN item_mst im     ON im.item_id = pid.item_id
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
LEFT JOIN uom_mst um ON um.uom_id = pid.uom_id
WHERE pid.active = 1 AND pi.sr_status = 3 AND ig.item_type_id = 4
  AND ig.co_id = 2 AND pi.branch_id = 29
  AND pi.inward_date BETWEEN '2026-09-01' AND '2026-09-30';

-- 3. Purchased yarn as physically counted in winding stock (branch 29)
SELECT ws.tran_date, ws.item_id, im.item_name,
       ROUND(SUM(ws.no_of_samples * ws.wt_per_sample) / 1000, 3) AS mt
FROM jute_prod_winding_stock ws
LEFT JOIN item_mst im ON im.item_id = ws.item_id
WHERE ws.co_id = 2 AND ws.branch_id = 29 AND ws.active = 1 AND ws.bucket = 'PURCHASE'
  AND ws.tran_date BETWEEN '2026-09-01' AND '2026-09-30'
GROUP BY ws.tran_date, ws.item_id, im.item_name
ORDER BY ws.tran_date;

-- NO REPAIR here: the fix needs an owner/mill ruling (see the triage report):
--   which document is "purchase yarn inwarded into winding stock", how a
--   company-106 / branch-87 receipt is linked to the company-2 / branch-29
--   winding report, and whether 'MTR' on those lines is to be corrected to 'MT'.
-- =============================================================================
