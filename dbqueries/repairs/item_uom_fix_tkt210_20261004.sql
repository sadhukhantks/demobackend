-- TKT-210 (TKT-000210): item 133-1159E UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana, page /dashboardportal/procurement/indent/createIndent.
-- Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code (vw_item_with_group_path.full_item_code).
--   item_id 300204 (co 2, group 2221 '133' TUBELIGHT/LAMPS & ELECT FITING):
--     "LED STREET LIGHT - 50 WATT (HELOGEN TYPE)"
--   uom_id 132 (KGS) -> 143 (PCS)  -- a light fitting is counted, not weighed
-- There is no co 1 twin for this item code.
-- Data issue, not a code bug: the indent screen shows item_mst.uom_id; there are no uom_item_map_mst rows for the item.
-- Follows the owner's TKT-197 / TKT-199 ruling: change the master AND the existing transaction lines
-- (transaction lines carry their own uom_id and do not follow the master).
-- Verified via SELECT against sls 2026-10-04: lines referencing item 300204 in all 14 item_id+uom_id tables:
--   CURRENT  proc_indent_dtl 33393 (indent 4141 no 120, 2026-10-03, OPEN, qty 6, KGS)  <- the indent being raised now
--   HISTORIC proc_indent_dtl  2237 (indent  136 no 143, 2019-08-19, CLOSED,    qty 2, KGS)
--   HISTORIC proc_indent_dtl  2599 (indent  188 no 254, 2019-12-05, CLOSED,    qty 3, KGS)
--   HISTORIC proc_indent_dtl 14782 (indent 1740 no  21, 2023-04-03, CANCELLED, qty 2, KGS)
--   HISTORIC proc_po_dtl      3569 (PO 1079 no 672, 2019-10-14, APPROVED, qty 2 @ 1830.00, KGS, from indent line 2237)
--   no inward / issue / transfer / enquiry / sales lines.
-- Block A is the minimum (master + the open 2026 indent line). Block B relabels the migrated 2019/2023 history too;
-- PO 672's rate of 1830 only makes sense per piece, so B is recommended for consistency (owner decides).
-- Quantities and rates are NOT touched.
-- sls is SELECT-only for the triage agent -- the owner runs the REPAIR blocks manually.

-- === VERIFY (run first) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id = 300204;
-- expect: 300204 co 2 uom 132 KGS
SELECT uom_id, uom_name FROM uom_mst WHERE uom_id IN (132, 143);   -- expect 132 KGS, 143 PCS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id = 300204
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id = 300204
UNION ALL
SELECT 'proc_inward_dtl', inward_dtl_id, item_id, NULL, uom_id FROM proc_inward_dtl WHERE item_id = 300204;
-- expect: indent lines 2237, 2599, 14782, 33393 and PO line 3569, no inward rows; all uom_id 132

-- === REPAIR block A (master + open 2026 indent line) ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 300204 AND uom_id = 132;          -- expect 1 row
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id = 33393 AND item_id = 300204 AND uom_id = 132;  -- expect 1 row

-- === REPAIR block B (historic 2019/2023 lines; optional, recommended) ===
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id IN (2237, 2599, 14782) AND item_id = 300204 AND uom_id = 132;  -- expect 3 rows
UPDATE proc_po_dtl SET uom_id = 143 WHERE po_dtl_id = 3569 AND item_id = 300204 AND uom_id = 132;                          -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 300204;
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id IN (33393, 2237, 2599, 14782);
-- UPDATE proc_po_dtl SET uom_id = 132 WHERE po_dtl_id = 3569;
