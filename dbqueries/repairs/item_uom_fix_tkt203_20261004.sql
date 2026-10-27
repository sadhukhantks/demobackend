-- TKT-203 (TKT-000203): item 147-0441 UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana, page /dashboardportal/procurement/indent/createIndent.
-- Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code (vw_item_with_group_path.full_item_code).
--   item_id 298693 (co 2, group 2186 '147' SPARES & ACCESSORIES FOR IST DRG): "DRAFT INTERMEDIATE STUD 85J139"
--   uom_id 132 (KGS) -> 143 (PCS)  -- a drafting stud is counted, not weighed
-- Co 1's twin (item_id 328518, same code/name) is ALREADY uom 143 PCS, so only co 2 needs the change.
-- Data issue, not a code bug: the indent screen shows item_mst.uom_id; there are no uom_item_map_mst rows for the item.
-- Follows the owner's TKT-197 / TKT-199 ruling: change the master AND the existing transaction lines
-- (transaction lines carry their own uom_id and do not follow the master).
-- Verified via SELECT against sls 2026-10-04: lines referencing item 298693 in all 14 item_id+uom_id tables:
--   proc_indent_dtl 33370 (indent 4139 no 117, 2026-10-01, APPROVED, qty 2, KGS)
--   proc_po_dtl     32021 (PO 12943 no 210, 2026-10-03, APPROVED, qty 2 @ 550.00, KGS)
--   no inward / issue / transfer / enquiry / sales lines.
-- Quantities and rates are NOT touched: PO line 32021 stays 2 @ 550.00, now per PCS (550/kg for a stud is implausible,
-- so per piece is almost certainly what was meant -- confirm with the reporter if in doubt).
-- sls is SELECT-only for the triage agent -- the owner runs the REPAIR block manually.

-- === VERIFY (run first) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id IN (298693, 328518);
-- expect: 298693 co 2 uom 132 KGS; 328518 co 1 uom 143 PCS (reference only, not changed)
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id IN (132, 143);   -- expect 132 KGS, 143 PCS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id = 298693
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id = 298693
UNION ALL
SELECT 'proc_inward_dtl', inward_dtl_id, item_id, NULL, uom_id FROM proc_inward_dtl WHERE item_id = 298693;
-- expect: proc_indent_dtl 33370 (qty 2), proc_po_dtl 32021 (qty 2), no inward rows; all uom_id 132

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 298693 AND uom_id = 132;          -- expect 1 row
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id = 33370 AND item_id = 298693 AND uom_id = 132;  -- expect 1 row
UPDATE proc_po_dtl SET uom_id = 143 WHERE po_dtl_id = 32021 AND item_id = 298693 AND uom_id = 132;          -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 298693;
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id = 33370;
-- UPDATE proc_po_dtl SET uom_id = 132 WHERE po_dtl_id = 32021;
