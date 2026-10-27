-- TKT-197 (TKT-000197): item 164-3224 UOM should be PCS instead of KGS on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana. Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code.
--   item_id 291671 (co 2) and item_id 323636 (co 1): "C I M/C CUT PINION 90T X 6DP ..." (group 164 C.I.M/C CUT PINIONS)
--   uom_id 132 (KGS) -> 143 (PCS)  -- a cut pinion is counted, not weighed
-- Owner decisions 2026-09-25: change both companies' items, AND change the existing transaction lines to PCS
-- (transaction lines carry their own uom_id, so they do not follow the master automatically).
-- Verified via SELECT against sls 2026-09-25: neither item has uom_item_map_mst rows; item 323636 has no
-- transaction lines; item 291671 has 2 proc_indent_dtl lines and 1 proc_po_dtl line (all KGS).
-- Quantities and rates are NOT touched: proc_po_dtl 7844 stays qty 4 @ 1125.00, now per PCS instead of per KG --
-- check with the reporter if the old PO's rate was really per KG.
-- sls is SELECT-only for this agent -- user must run the REPAIR block manually.

-- === VERIFY (run first, confirm current state before applying) ===
SELECT im.item_id, ig.co_id, ig.item_grp_code, im.item_code, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id IN (291671, 323636);
-- expect: both rows uom_id 132 / KGS
SELECT 'proc_indent_dtl' AS tbl, indent_dtl_id AS id, item_id, qty, uom_id FROM proc_indent_dtl WHERE item_id IN (291671, 323636)
UNION ALL
SELECT 'proc_po_dtl', po_dtl_id, item_id, qty, uom_id FROM proc_po_dtl WHERE item_id IN (291671, 323636);
-- expect: proc_indent_dtl 6307 (qty 4), 33242 (qty 3); proc_po_dtl 7844 (qty 4); all uom_id 132

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id IN (291671, 323636) AND uom_id = 132;   -- expect 2 rows
UPDATE proc_indent_dtl SET uom_id = 143 WHERE indent_dtl_id IN (6307, 33242) AND item_id = 291671 AND uom_id = 132; -- expect 2 rows
UPDATE proc_po_dtl SET uom_id = 143 WHERE po_dtl_id = 7844 AND item_id = 291671 AND uom_id = 132;                  -- expect 1 row

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id IN (291671, 323636);
-- UPDATE proc_indent_dtl SET uom_id = 132 WHERE indent_dtl_id IN (6307, 33242);
-- UPDATE proc_po_dtl SET uom_id = 132 WHERE po_dtl_id = 7844;
