-- TKT-000217 — sls (reporter: Manoj Thapa, co 106 LCPL Limelight): "please delete these gate
-- entry permanently, it is created in last year due to wrong entering of date":
--   LCPL/JGE/25-26/00394, LCPL/JGE/25-26/00395, LCPL/JGE/25-26/00396
--
-- Data issue, not a code bug. The three rows are FY 2025-26 gate entries on branch 87
-- (FACTORY) that were keyed with a last-year date, so they took the next 25-26 sequence
-- numbers. Each one was then re-entered correctly in FY 2026-27:
--   jute_mr 28276  #394  2026-02-08  AP 39UB 7929  challan wt 12770  -> re-entered as 28275 (#100, 2026-08-29, net 12770)
--   jute_mr 28962  #395  2026-03-31  AP31TE5959    challan 3 / 11200  -> re-entered as 28974 (#155, 2026-10-03, challan 3, net 11200)
--   jute_mr 28965  #396  2026-03-31  AP31TE5959    all weights 0      -> blank duplicate of the same lorry
-- (jute_mr 8640 also carries gate no 396, but in FY 2021-22 — it is NOT touched.)
--
-- Checked 2026-10-08 (SELECT only): all three are status 1 (Gate IN), qc_check 0, no out
-- time, no PO, no MR no., no bill pass, and NOTHING references them:
--   jute_mr_li 0 rows, jute_prod_selection_trolly_wt 0, jute_mr.src_jute_mr_id 0,
--   sales_invoice_jute.mr_id 0, sales_order_jute.mr_id 0; no triggers on jute_mr.
-- So a hard DELETE of the three header rows is complete — no line or child rows to clean.
-- The app has no delete/cancel action for gate entries (src/juteProcurement/juteGateEntry.py
-- only has create + update), which is why this needs a hand repair.
--
-- DO NOT RUN BLIND. sls is production: the owner runs this after approving.
-- APPLIED on sls 2026-10-08 with owner approval: DELETE reported 3 rows, committed;
-- 28275 / 28974 / 8640 untouched.
--
-- ---------------------------------------------------------------------------
-- VERIFY (SELECT only) — expect exactly 3 rows, all status_id 1, line_count 0
-- ---------------------------------------------------------------------------
SELECT jm.jute_mr_id, jm.branch_id, jm.jute_gate_entry_no, jm.jute_gate_entry_date,
       jm.vehicle_no, jm.challan_no, jm.net_weight, jm.status_id, jm.qc_check, jm.po_id,
       jm.branch_mr_no, jm.bill_pass_no,
       (SELECT COUNT(*) FROM jute_mr_li li WHERE li.jute_mr_id = jm.jute_mr_id) AS line_count,
       (SELECT COUNT(*) FROM jute_prod_selection_trolly_wt t WHERE t.jute_mr_id = jm.jute_mr_id) AS trolley_refs,
       (SELECT COUNT(*) FROM jute_mr c WHERE c.src_jute_mr_id = jm.jute_mr_id) AS child_mrs
FROM jute_mr jm
WHERE jm.jute_mr_id IN (28276, 28962, 28965);

-- Row count the DELETE below would touch (run this instead of DELETE+ROLLBACK):
SELECT COUNT(*)
FROM jute_mr
WHERE jute_mr_id IN (28276, 28962, 28965)
  AND branch_id = 87
  AND jute_gate_entry_no IN (394, 395, 396)
  AND jute_gate_entry_date BETWEEN '2025-04-01' AND '2026-03-31'
  AND status_id = 1
  AND po_id IS NULL
  AND branch_mr_no IS NULL
  AND NOT EXISTS (SELECT 1 FROM jute_mr_li li WHERE li.jute_mr_id = jute_mr.jute_mr_id);
-- expect: 3   (anything else -> stop, something changed since 2026-10-08)

-- ---------------------------------------------------------------------------
-- REPAIR — run in one transaction; COMMIT only if the DELETE reports 3 rows
-- ---------------------------------------------------------------------------
-- START TRANSACTION;
-- DELETE FROM jute_mr
-- WHERE jute_mr_id IN (28276, 28962, 28965)
--   AND branch_id = 87
--   AND jute_gate_entry_no IN (394, 395, 396)
--   AND jute_gate_entry_date BETWEEN '2025-04-01' AND '2026-03-31'
--   AND status_id = 1
--   AND po_id IS NULL
--   AND branch_mr_no IS NULL
--   AND NOT EXISTS (SELECT 1 FROM jute_mr_li li WHERE li.jute_mr_id = jute_mr.jute_mr_id);
-- -- expect: 3 rows affected
-- COMMIT;

-- Re-check afterwards: expect 0
-- SELECT COUNT(*) FROM jute_mr WHERE jute_mr_id IN (28276, 28962, 28965);

-- ---------------------------------------------------------------------------
-- ROLLBACK — exact rows as they stood on 2026-10-08 (SELECT * dump)
-- ---------------------------------------------------------------------------
-- INSERT INTO jute_mr (jute_mr_id, jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date, challan_date, challan_no, challan_weight, gross_weight, tare_weight, net_weight, variable_shortage, actual_weight, in_time, out_date, out_time, qc_check, mukam_id, unit_conversion, mr_weight, remarks, status_id, is_returned, vehicle_no, marketing_slip, transporter, driver_name, frieght_paid, updated_by, updated_date_time, po_id, branch_id, party_id, party_branch_id, jute_supplier_id, src_com_id, bill_pass_no, bill_pass_date, total_amount, claim_amount, roundoff, net_total, tds_amount, invoice_no, invoice_date, invoice_amount, payment_due_date, invoice_received_date, invoice_upload, challan_upload, bill_pass_complete, approval_level, src_jute_mr_id, transfer_mode) VALUES
--   (28276, 394, NULL, '2026-02-08', NULL, '2026-02-08', '15', 12770.0e0, 22960.0e0, 0.0e0, 22960.0e0, 0.0e0, 22960.0e0, '12:49:00', NULL, NULL, 0, NULL, NULL, NULL, NULL, 1, 0, 'AP 39UB 7929', 0, '', 'SURIVALLI', NULL, 21, '2026-09-24 12:50:29', NULL, 87, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0),
--   (28962, 395, NULL, '2026-03-31', NULL, '2026-10-01', '3', 11200.0e0, 20900.0e0, 0.0e0, 20900.0e0, 0.0e0, 20900.0e0, '12:47:00', NULL, NULL, 0, NULL, NULL, NULL, NULL, 1, 0, 'AP31TE5959', 0, '', 'RAM', NULL, 21, '2026-10-07 12:51:20', NULL, 87, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0),
--   (28965, 396, NULL, '2026-03-31', NULL, '2026-10-07', 'N/A', 0.0e0, 0.0e0, 0.0e0, 0.0e0, 0.0e0, 0.0e0, '12:49:00', NULL, NULL, 0, NULL, NULL, NULL, NULL, 1, 0, 'AP31TE5959', 0, '', 'ERRGD', NULL, 21, '2026-10-07 12:51:04', NULL, 87, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0);
