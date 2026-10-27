-- Rollback for ISSUEREOPEN-20260913 (10 rows). Run against sls.
START TRANSACTION;
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86351 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86352 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86353 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86354 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'KGROUND-20260905', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86355 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86356 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:24:50' WHERE jute_issue_id = 86357 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:28:00' WHERE jute_issue_id = 86358 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:28:00' WHERE jute_issue_id = 86359 AND updated_by = 'ISSUEREOPEN-20260913';
UPDATE jute_issue SET status_id = 3, updated_by = 'system', update_date_time = '2026-08-21 15:28:00' WHERE jute_issue_id = 86360 AND updated_by = 'ISSUEREOPEN-20260913';
COMMIT;
