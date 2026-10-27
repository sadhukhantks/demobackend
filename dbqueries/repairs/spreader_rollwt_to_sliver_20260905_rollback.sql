-- Rollback for ROLLWT2SLIVER-20260905 on sls.
-- Deletes the sliver rows this repair inserted and reactivates the roll rows.
START TRANSACTION;
DELETE FROM jute_sqc_spreader_sliver_wt WHERE spreader_sliver_wt_id IN (7, 8, 9, 10);
UPDATE jute_sqc_spreader_roll_wt SET active = 1 WHERE spreader_roll_wt_id IN (3, 4, 5, 19);
COMMIT;
