-- rollback for jute_po_rollback_to_open_lcpl_00071 on sls, generated 20260926_0922
START TRANSACTION;
UPDATE jute_po SET status_id = 3, approval_level = NULL, updated_by = 21, updated_date_time = '2026-08-10 15:09:16' WHERE jute_po_id = 13295;
DELETE FROM jute_po_status_log WHERE jute_po_id = 13295 AND source = 'MANUAL' AND trigger_ref = 'rollback_to_open LCPL/JPO/26-27/00071' AND new_status_id = 1;
COMMIT;
