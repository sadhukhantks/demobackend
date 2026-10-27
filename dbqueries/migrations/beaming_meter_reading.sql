-- Migration: Beaming production — opening/closing meter reading (like Drawing).
--   The operator reads the beaming machine's yard counter at the start and end of the
--   entry instead of working out cuts by hand. diff_meter (close - open, counter wrap
--   handled) is the length beamed in the row and becomes act_prod_yards; act_cuts per
--   beam is derived from it (diff / laid_length / no_of_beam). All three columns are
--   NULL on rows entered without a meter reading (legacy cuts-based rows keep working).
-- Date: 2026-10-24
-- Applies to: tenant databases dev3, sls.
-- Run AFTER beaming_composite_and_beam_no.sql.

ALTER TABLE jute_prod_beaming_daily
    ADD COLUMN open_meter  DECIMAL(14,2) NULL AFTER no_of_beam,
    ADD COLUMN close_meter DECIMAL(14,2) NULL AFTER open_meter,
    ADD COLUMN diff_meter  DECIMAL(14,2) NULL AFTER close_meter;

-- Rollback:
-- ALTER TABLE jute_prod_beaming_daily DROP COLUMN diff_meter, DROP COLUMN close_meter, DROP COLUMN open_meter;
