-- TKT-211 (TKT-000211): item 133-1137 UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana, page /dashboardportal/procurement/indent/createIndent.
-- Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code (vw_item_with_group_path.full_item_code).
--   item_id 301661 (co 2, group 2221 '133' TUBELIGHT/LAMPS & ELECT FITING): "STREET LIGHT JE WHITE 100 W"
--   uom_id 132 (KGS) -> 143 (PCS)  -- a light fitting is counted, not weighed
-- There is no co 1 twin for this item code.
-- Data issue, not a code bug: the indent screen shows item_mst.uom_id; there are no uom_item_map_mst rows for the item.
-- Follows the owner's TKT-197 / TKT-199 ruling: change the master AND the existing transaction lines
-- (transaction lines carry their own uom_id and do not follow the master).
-- Verified via SELECT against sls 2026-10-04: lines referencing item 301661 in all 14 item_id+uom_id tables:
--   proc_indent_dtl 33392 (indent 4141 no 120, 2026-10-03, OPEN, qty 6, KGS)  -- same indent as TKT-210's line 33393
--   no PO / inward / issue / transfer / enquiry / sales lines.
-- Quantity is NOT touched.
-- sls is SELECT-only for the triage agent -- the owner runs the REPAIR block manually.

-- === VERIFY (run first) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id = 301661;
-- expect: 301661 co 2 uom 132 KGS
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id IN (132, 143);   -- expect 132 KGS, 143 PCS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id = 301661
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id = 301661
UNION ALL
SELECT 'proc_inward_dtl', inward_dtl_id, item_id, NULL, uom_id FROM proc_inward_dtl WHERE item_id = 301661;
-- expect: proc_indent_dtl 33392 (qty 6) only; uom_id 132
-- (if a PO line has appeared since 2026-10-04, add it to the REPAIR block before running)

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 301661 AND uom_id = 132;          -- expect 1 row
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id = 33392 AND item_id = 301661 AND uom_id = 132;  -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 301661;
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id = 33392;
