-- Migration: weaving Sync (recompute-and-store without lock) + SQC picks/dm capture.
-- 2026-08-21. Apply to every tenant DB with the weaving tables (dev3, sls) BEFORE
-- deploying the code that reads these columns.
--
-- 1) jute_sqc_weaving_pick.picks_dm — the as-entered picks-per-decimetre reading.
--    The stored `picks` column stays PPI (= picks_dm * 0.254); picks_dm is NULL on
--    legacy PPI-entered rows (display derives picks/0.254).
-- 2) jute_prod_weaving_process_lock.synced_by / synced_date_time — the sync/lock
--    header now records the last recompute-and-store (POST /weavingProd/sync or
--    /process). A header with is_locked = 0 is a sync-only unit: its
--    jute_prod_weaving_log rows serve reads as a cache; reprocess_needed doubles
--    as the STALE flag for both locked and sync-only headers.
-- 3) Backfill: existing processed headers were stored at process time — stamp
--    their sync fields from processed_* so they read as freshly synced.
--
-- Rollback:
--   ALTER TABLE jute_sqc_weaving_pick DROP COLUMN picks_dm;
--   ALTER TABLE jute_prod_weaving_process_lock DROP COLUMN synced_by, DROP COLUMN synced_date_time;

ALTER TABLE jute_sqc_weaving_pick
  ADD COLUMN picks_dm DECIMAL(10,3) NULL AFTER width;

ALTER TABLE jute_prod_weaving_process_lock
  ADD COLUMN synced_by INT NULL AFTER processed_date_time,
  ADD COLUMN synced_date_time TIMESTAMP NULL DEFAULT NULL AFTER synced_by;

UPDATE jute_prod_weaving_process_lock
SET synced_by = processed_by,
    synced_date_time = processed_date_time
WHERE processed_date_time IS NOT NULL;
