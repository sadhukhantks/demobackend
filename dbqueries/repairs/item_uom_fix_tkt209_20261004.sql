-- TKT-209 (TKT-000209): item 165-0026 UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana, page /dashboardportal/procurement/purchaseOrder/createPO.
-- Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code (vw_item_with_group_path.full_item_code).
--   item_id 270749 (co 2, group 2211 '165' DRAFTS CHANGE PINIONS):
--     "DRAFT CHANGE PINION 34T X 8DPX 1-1/2"B X 1-1/2"FX 5/16 K.W."
--   uom_id 132 (KGS) -> 143 (PCS)  -- a change pinion is counted, not weighed
-- Co 1's twin (item_id 305507, same code/name) is ALREADY uom 143 PCS, so only co 2 needs the change.
-- Data issue, not a code bug: the indent/PO screens show item_mst.uom_id; there are no uom_item_map_mst rows for the item.
-- Follows the owner's TKT-197 / TKT-199 ruling: change the master AND the existing transaction lines
-- (transaction lines carry their own uom_id and do not follow the master).
-- Verified via SELECT against sls 2026-10-04: lines referencing item 270749 in all 14 item_id+uom_id tables:
--   proc_indent_dtl 33214 (indent 4111 no 100, 2026-09-22, APPROVED, qty 3, KGS)
--   proc_po_dtl     31964 (PO 12923 no 200, 2026-09-30, APPROVED, qty 3 @ 340.00, KGS)
--   no inward / issue / transfer / enquiry / sales lines.
-- Quantities and rates are NOT touched: PO line 31964 stays 3 @ 340.00, now per PCS.
-- sls is SELECT-only for the triage agent -- the owner runs the REPAIR block manually.

-- === VERIFY (run first) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id IN (270749, 305507);
-- expect: 270749 co 2 uom 132 KGS; 305507 co 1 uom 143 PCS (reference only, not changed)
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id IN (132, 143);   -- expect 132 KGS, 143 PCS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id = 270749
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id = 270749
UNION ALL
SELECT 'proc_inward_dtl', inward_dtl_id, item_id, NULL, uom_id FROM proc_inward_dtl WHERE item_id = 270749;
-- expect: proc_indent_dtl 33214 (qty 3), proc_po_dtl 31964 (qty 3), no inward rows; all uom_id 132
-- (if an inward line has appeared since 2026-10-04, add it to the REPAIR block before running)

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 270749 AND uom_id = 132;          -- expect 1 row
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id = 33214 AND item_id = 270749 AND uom_id = 132;  -- expect 1 row
UPDATE proc_po_dtl SET uom_id = 143 WHERE po_dtl_id = 31964 AND item_id = 270749 AND uom_id = 132;          -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 270749;
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id = 33214;
-- UPDATE proc_po_dtl SET uom_id = 132 WHERE po_dtl_id = 31964;
