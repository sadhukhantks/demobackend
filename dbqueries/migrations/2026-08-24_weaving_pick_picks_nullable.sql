-- 2026-08-24: Weaving Pick-SQC (R-08-21) — width and picks each optional,
-- at least one required per reading (was: width AND picks both mandatory).
-- Width-only readings store picks NULL / picks_dm NULL. AVG/MIN/MAX in
-- vw_weaving_pick_act skip NULLs per column, so the view needs no change;
-- act_picks last-date resolution gains picks IS NOT NULL guards in code.

ALTER TABLE jute_sqc_weaving_pick
  MODIFY picks DECIMAL(10,3) NULL;

-- Rollback:
-- (only safe after deleting/zero-filling width-only rows: picks IS NULL)
-- ALTER TABLE jute_sqc_weaving_pick MODIFY picks DECIMAL(10,3) NOT NULL;
