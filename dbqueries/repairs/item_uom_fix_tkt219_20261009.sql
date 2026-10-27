-- TKT-219 (TKT-000219): item 207-1211 UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana, page /dashboardportal/procurement/indent/createIndent.
-- Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code (vw_item_with_group_path.full_item_code).
--   item_id 301494 (co 2, group 2254 '207' SPARE OF ROTARY FALLER GILL DRAW HEAD M/C):
--   "DISC PIN PART NO:-2C01130796 (LAGAN MAKE)", uom_id 132 (KGS) -> 143 (PCS) -- a pin is counted, not weighed.
-- Co 1 twin: item_id 330066 (group 2390 '207', code 1211, same name), also KGS, with NO transaction lines anywhere.
--   Left commented out below -- the owner decides whether to align it (TKT-197 changed both companies, TKT-199 left co 1 alone).
-- Data issue, not a code bug: the indent screen shows item_mst.uom_id; there are no uom_item_map_mst or item_minmax_mst rows.
-- Follows the owner's TKT-197 / TKT-199 ruling: change the master AND the existing transaction lines
-- (transaction lines carry their own uom_id and do not follow the master).
-- Verified via SELECT against sls 2026-10-09: lines referencing item 301494 in all item_id+uom_id tables
-- (issue_li, jute_mech_*_spare, proc_enquiry_dtl, proc_indent_dtl, proc_inward_dtl, proc_po_dtl,
--  proc_price_enquiry_response_dtl, proc_transfer_dtl, sales_*_dtl):
--   proc_indent_dtl 33456 (indent_id 4154 no 130, branch 29, status 1 OPEN, qty 288, remarks '(02 SET)', KGS)
--   nothing else.
-- Quantity is NOT touched.
-- sls is SELECT-only for the triage agent -- the owner runs the REPAIR block manually.

-- === VERIFY (run first) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id IN (301494, 330066);
-- expect: 301494 co 2 uom 132 KGS ; 330066 co 1 uom 132 KGS
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id IN (132, 143);   -- expect 132 KGS, 143 PCS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id IN (301494, 330066)
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id IN (301494, 330066)
UNION ALL
SELECT 'proc_inward_dtl', inward_dtl_id, item_id, NULL, uom_id FROM proc_inward_dtl WHERE item_id IN (301494, 330066);
-- expect: proc_indent_dtl 33456 (qty 288, uom 132) only
-- (if a PO line has appeared since 2026-10-09, add it to the REPAIR block before running)

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 301494 AND uom_id = 132;          -- expect 1 row
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id = 33456 AND item_id = 301494 AND uom_id = 132;    -- expect 1 row

-- Optional (owner decision): align the Co 1 twin as well. No transaction lines exist for it.
-- UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 330066 AND uom_id = 132;        -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 301494;
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id = 33456;
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 330066;   -- only if the twin was changed
