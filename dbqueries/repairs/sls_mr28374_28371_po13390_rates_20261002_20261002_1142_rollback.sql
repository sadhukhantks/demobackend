-- rollback for sls_mr28374_28371_po13390_rates on sls, generated 20261002_1142
START TRANSACTION;
UPDATE jute_mr_li SET rate = NULL, actual_rate = NULL, total_price = 0.0, updated_date_time = '2026-09-30 18:31:44' WHERE jute_mr_li_id = 46103;
UPDATE jute_mr_li SET rate = NULL, actual_rate = NULL, total_price = 0.0, updated_date_time = '2026-09-30 18:31:44' WHERE jute_mr_li_id = 46104;
UPDATE jute_mr_li SET rate = NULL, actual_rate = NULL, total_price = 0.0, updated_date_time = '2026-09-30 18:30:09' WHERE jute_mr_li_id = 46101;
UPDATE jute_mr SET total_amount = 0.0, claim_amount = 0.0, tds_amount = 0.0, roundoff = 0.0, net_total = 0.0, mr_weight = 230.0, updated_by = 24, updated_date_time = '2026-09-30 22:36:31' WHERE jute_mr_id = 28371;
UPDATE jute_mr SET total_amount = 0.0, claim_amount = 0.0, tds_amount = 0.0, roundoff = 0.0, net_total = 0.0, mr_weight = 10180.0, updated_by = 24, updated_date_time = '2026-09-30 22:36:50' WHERE jute_mr_id = 28374;
UPDATE jute_po_li SET rate = 12500.0, value = 1250000.0, updated_date_time = '2026-09-30 17:35:06' WHERE jute_po_li_id = 31414;
UPDATE jute_po_li SET rate = 12300.0, value = 1230000.0, updated_date_time = '2026-09-30 17:35:06' WHERE jute_po_li_id = 31415;
UPDATE jute_po SET jute_po_value = 2480000.0, updated_by = 19, updated_date_time = '2026-09-30 17:35:11' WHERE jute_po_id = 13390;
COMMIT;
