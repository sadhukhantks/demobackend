-- TKT-000184 (priority 4): "INCORRECT UNIT" — Rajiv Mimani, tenant sls, co 2 / branch 29
-- "IN PO NO 123 DATED 14.09.2026 the first item unit of that PO was incorrect.
--  Please correct the unit in Pcs."
--
-- Target: sls ONLY. DO NOT run automatically — sls is production (SELECT-only for agents).
-- One-off DATA repair, not a schema migration (no DDL).
--
-- ======================== WHICH ROW ========================
-- Document: proc_po.po_id = 12839 (po_no 123, po_date 2026-09-14, branch_id 29,
-- supplier_id 1968, status_id 3 = approved). Only po_no 123 on that date.
--
-- First line: proc_po_dtl.po_dtl_id = 31750
--     item_id 299795  '3700E'  PNEUMATIC CONNECTOR ONE SIDE PUSH 8 MM ONE SIDE THREAD 1/2"
--     qty 4, rate 128, uom_id 132 (KGS)      <-- to be corrected
--     item_mst.uom_id today = 143 (PCS)
-- Second line (po_dtl_id 31751, item 304213, NOS) matches its item master — leave alone.
--
-- ======================== ROOT CAUSE: NOT A BUG, AND NOT A MIS-KEYED ENTRY ========================
-- sls.trigger_audit_logs settles it. item_mst.uom_id for item 299795 was 132 (KGS) from
-- creation until it was changed to 143 (PCS):
--     audit_id 7799120  UPDATE item_mst pk 299795  by Tarun@106.219.132.45
--                       at 2026-09-17 00:02:02   uom_id: 132 -> 143
--     audit_id 7799970  UPDATE item_mst pk 299795  at 2026-09-19 13:27:32 (updated_by only)
--
-- Timeline:
--     2026-09-07 10:23  indent_no 76 (indent_id 4086) line 33105 created, uom KGS
--     2026-09-14 11:17  indent line last touched (uom unchanged)
--     2026-09-14 12:01  PO 123 raised from that indent, line 31750 copies uom KGS
--     2026-09-17 00:02  item master corrected KGS -> PCS   <-- AFTER the PO
--     2026-09-19 13:27  item master touched again; uom_item_map_mst row 72 (PCS -> NOS) dated here
--     2026-09-19 13:32  reporter's screenshot
--     2026-09-19 13:36  ticket raised
--
-- So when the indent and the PO were entered, KGS *was* the item's correct and only offered
-- UOM — the PO screen's dropdown is restricted per item to item_mst.uom_id plus the item's
-- uom_item_map_mst rows (backend get_item_uom_by_group_id, src/procurement/query.py:91;
-- frontend bucketing in vowerp3ui .../createPO/utils/poMappers.ts:260-295), and the PO line
-- prefers the item's own default (.../createPO/hooks/usePOLineItems.ts:160-176). The
-- application recorded exactly what the master said at the time. Someone then fixed the
-- master, which retroactively made the historical PO line look wrong.
--
-- => No code defect. Nothing to fix in the application for this ticket. This is a
--    historical-document alignment request, and only the mill can authorise it.
--
-- Supporting context:
--   * 37 of the 38 PO lines raised on 2026-09-14 for branch 29 match their item master UOM;
--     this is the only mismatch that day.
--   * Across all of sls, 277 / 31851 proc_po_dtl lines (0.87%) differ from item_mst.uom_id.
--     Some are legitimate alternate-UOM purchases and many are probably this same
--     master-corrected-later situation. Do NOT add a blanket "PO UOM must equal item UOM"
--     validation on the strength of this ticket; it would reject valid entries.
--   * The earlier PO for this item (po_id 1296, po_no 877, 2019-12-21, 24 KGS) was likewise
--     correct for its time. Leave it alone.
--
-- ======================== SAFE TO REPAIR ========================
--   * Nothing received against the line: proc_inward_dtl has 0 rows for po_dtl_id 31750.
--     No receipt booked in KGS to reconcile, no stock impact.
--   * Financially neutral: qty (4), rate (128) and all amounts are untouched, so the PO value
--     (total_amount 1295.88, net_amount 1098.20) and the po_gst row (po_gst_id 33414,
--     tax_amount 78.34) do not move. Rate 128 is plainly per piece, consistent with PCS.
--   * proc_po_dtl and proc_indent_dtl carry AFTER UPDATE audit triggers
--     (trg_proc_po_dtl_update, after_proc_indent_dtl_update) so the change is logged.
--   * (jute_mr rows matching po_id 12839 are a FALSE POSITIVE: jute_mr.po_id points at
--     jute_po, a separate numbering space; those rows are 2025-08 jute gate entries.)
--
-- CAVEAT FOR THE MILL: PO 123 was approved and issued to the supplier reading "4 KGS".
-- Editing an approved document in place changes what the stored PO says versus what was
-- approved/sent. Two options — the mill chooses:
--   (a) run section 2 below: keeps PO no 123, one row, audit-trailed, fastest;
--   (b) the in-app route: Clone PO 123 (POST /clone_po creates a fresh status 21 draft),
--       set PCS on the clone, and cancel/close 123 — cleanest paper trail with the supplier.
-- There is deliberately no UI path to edit an approved PO's UOM, which is why this ticket
-- came to us rather than being self-served.
--
-- Every WHERE clause pins the row id, the current wrong value, the item and the parent
-- document, so re-running this — or running it against the wrong database — is a harmless
-- no-op affecting 0 rows.
-- ==================================================================================

-- ---------- 1) VERIFY BEFORE: expect exactly 1 row, current_uom = 'KGS' ----------
SELECT d.po_dtl_id, p.po_no, p.po_date, p.branch_id, d.item_id, i.item_code,
       d.qty, d.rate, d.uom_id, u.uom_name AS current_uom,
       i.uom_id AS item_master_uom_id, iu.uom_name AS item_master_uom
FROM proc_po_dtl d
JOIN proc_po  p ON p.po_id   = d.po_id
JOIN item_mst i ON i.item_id = d.item_id
LEFT JOIN uom_mst u  ON u.uom_id = d.uom_id
LEFT JOIN uom_mst iu ON iu.uom_id = i.uom_id
WHERE d.po_dtl_id = 31750;

-- Confirm nothing has been received against the line (expect inward_lines = 0):
SELECT COUNT(*) AS inward_lines FROM proc_inward_dtl WHERE po_dtl_id = 31750;


-- ---------- 2) REPAIR (what the ticket asks for): PO line 1, KGS -> PCS ----------
UPDATE proc_po_dtl
SET uom_id = 143,                 -- PCS
    updated_date_time = NOW()
WHERE po_dtl_id = 31750
  AND po_id     = 12839
  AND item_id   = 299795
  AND uom_id    = 132;            -- KGS, the current wrong value
-- Expected: 1 row affected.


-- ---------- 3) OPTIONAL — ASK THE REPORTER FIRST: the source indent line ----------
-- Originating indent line 33105 (indent_no 76, indent_id 4086, status 3) still says KGS.
-- The ticket only mentioned the PO, and this is a separately approved document, so leave
-- commented out unless the mill confirms. Keeping the two consistent is usually desirable.
--
-- UPDATE proc_indent_dtl
-- SET uom_id = 143,               -- PCS
--     updated_date_time = NOW()
-- WHERE indent_dtl_id = 33105
--   AND indent_id     = 4086
--   AND item_id       = 299795
--   AND uom_id        = 132;      -- KGS
-- -- Expected: 1 row affected.


-- ---------- 4) VERIFY AFTER: expect uom_id 143 / 'PCS'; qty 4 and rate 128 unchanged ----------
SELECT d.po_dtl_id, p.po_no, d.qty, d.rate, d.uom_id, u.uom_name
FROM proc_po_dtl d
JOIN proc_po p ON p.po_id = d.po_id
LEFT JOIN uom_mst u ON u.uom_id = d.uom_id
WHERE d.po_dtl_id = 31750;

-- And that the PO value did not move (expect total_amount 1295.88, net_amount 1098.20):
SELECT po_id, po_no, total_amount, net_amount, status_id FROM proc_po WHERE po_id = 12839;


-- ---------- ROLLBACK (restores the pre-repair values) ----------
-- UPDATE proc_po_dtl SET uom_id = 132, updated_date_time = NOW()
-- WHERE po_dtl_id = 31750 AND po_id = 12839 AND item_id = 299795 AND uom_id = 143;
--
-- UPDATE proc_indent_dtl SET uom_id = 132, updated_date_time = NOW()
-- WHERE indent_dtl_id = 33105 AND indent_id = 4086 AND item_id = 299795 AND uom_id = 143;
