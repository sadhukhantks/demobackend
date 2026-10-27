-- TKT-159 / TKT-161: item_mst.uom_id wrong for 2 items on sls (co_id=2, reported from branch 29)
-- Reporter: Babula Mohrana. Displayed item code = item_grp_mst.item_grp_code + '-' + item_mst.item_code.
--   TKT-159: item_id 299795  (120-3700E, "PNEUMATIC CONNECTOR ONE SIDE PUSH 8 MM ...")
--            uom_id 132 (KGS) -> 143 (PCS)  -- a connector is counted, not weighed
--   TKT-161: item_id 284957  (147-0291, "RETAINING ROLLER OIL BUSH 1 1/2\" BORE PNO 1006 ...")
--            uom_id 147 (PAR) -> 143 (PCS)
-- Verified via SELECT against sls item_mst/item_grp_mst/uom_mst 2026-09-16 (see [AI-TRIAGE] comments
-- on the tickets). Neither item has uom_item_map_mst or item_minmax_mst rows, and transaction detail
-- tables (proc_po_dtl, proc_indent_dtl, etc.) carry their own uom_id per line captured at transaction
-- time -- so this master-only change does not touch historical records.
-- sls is SELECT-only for this agent -- user must run the REPAIR block manually.

-- === VERIFY (run first, confirm current state before applying) ===
SELECT im.item_id, ig.item_grp_code, im.item_code, im.item_name, im.uom_id, u.uom_name
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
JOIN uom_mst u ON u.uom_id = im.uom_id
WHERE im.item_id IN (299795, 284957);
-- expect: 299795 -> uom_name KGS, 284957 -> uom_name PAR

-- === REPAIR ===
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 299795 AND uom_id = 132; -- TKT-159: KGS -> PCS
UPDATE item_mst SET uom_id = 143, updated_date_time = NOW() WHERE item_id = 284957 AND uom_id = 147; -- TKT-161: PAR -> PCS

-- === ROLLBACK (uncomment if needed) ===
-- UPDATE item_mst SET uom_id = 132, updated_date_time = NOW() WHERE item_id = 299795;
-- UPDATE item_mst SET uom_id = 147, updated_date_time = NOW() WHERE item_id = 284957;
