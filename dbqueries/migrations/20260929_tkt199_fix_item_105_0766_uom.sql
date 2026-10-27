-- TKT-000199: item 105-0766 (co_id 2 "THE EMPIRE JUTE COMPANY LTD.", tenant sls)
-- has uom_id = 132 (KGS) but should be uom_id = 143 (PCS) per the reporter
-- (Babula Mohrana, sls, co 2/branch 29): "ITEM CODE - 105-0766 UOM WILL BE
-- PCS INSTEAD OF KGS".
--
-- sls is a production tenant — SELECT-only from this server. This script is
-- delivered for the owner to run manually after approval; nothing here has
-- been executed except the verify SELECTs below.
--
-- Target: sls.item_mst, item_id = 284216
--   (full_item_code via vw_item_with_group_path = '105-0766', item_grp_id
--    2188, item_grp_code '105', co_id 2, item_name
--    'PLUMBER BLOCK FOR 22211 K BRG,')
--
-- Related finding for the owner to decide on separately (NOT included in the
-- repair below — ticket only names co 2's item):
--   item_id 317867 is the same item_code/item_name/full_item_code '105-0766'
--   under item_grp_id 2324 (co_id 1 "Nellimarla Jute Mills Co. Ltd"), also
--   uom_id 132 (KGS). If this is a parity issue (same physical item mastered
--   once per company) rather than a coincidence, it likely needs the same
--   fix — but confirm intent before touching a different company's item.
--
-- APPLIED on sls 2026-10-02 (owner-approved): item 284216 132 -> 143 AND the
-- one open transaction line on it, proc_indent_dtl 33273 (indent 4121, no 110,
-- status 1 Open, qty 4, no PO yet) 132 -> 143, so the indent matches the master.
-- Owner ruled: leave co 1's item 317867 as KGS. Rollback for both rows below.

-- ============================================================
-- VERIFY (safe to run — SELECT only)
-- ============================================================

-- 1. Confirm the exact row and current (wrong) UOM.
SELECT im.item_id, im.item_code, im.item_name, im.uom_id, um.uom_name,
       im.item_grp_id, ig.item_grp_code, ig.co_id, vip.full_item_code
FROM item_mst im
LEFT JOIN uom_mst um ON um.uom_id = im.uom_id
LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
LEFT JOIN vw_item_with_group_path vip ON vip.item_id = im.item_id
WHERE im.item_id = 284216;
-- Expect: item_code='0766', full_item_code='105-0766', co_id=2, uom_id=132 (KGS)

-- 2. Count of rows the repair's WHERE would touch (must be exactly 1).
SELECT COUNT(*) AS rows_to_update
FROM item_mst
WHERE item_id = 284216;

-- 3. Confirm the target UOM id/name exists and is what "PCS" means here.
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id = 143;
-- Expect: uom_name = 'PCS'

-- ============================================================
-- REPAIR (DO NOT RUN from this server — sls is SELECT-only here.
-- Deliver to the owner / run manually against sls with pre-approval.)
-- ============================================================

-- UPDATE item_mst
-- SET uom_id = 143
-- WHERE item_id = 284216;
-- UPDATE proc_indent_dtl
-- SET uom_id = 143
-- WHERE indent_dtl_id = 33273 AND item_id = 284216 AND uom_id = 132;

-- ============================================================
-- ROLLBACK (if the repair is applied and needs reverting)
-- ============================================================

-- UPDATE item_mst
-- SET uom_id = 132
-- WHERE item_id = 284216;
-- UPDATE proc_indent_dtl
-- SET uom_id = 132
-- WHERE indent_dtl_id = 33273;
