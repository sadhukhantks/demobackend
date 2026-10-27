-- Deactivate the retired "Width & Picks SQC" portal menu (juteSQC/widthPicks).
-- 2026-08-21: page retired — superseded by the Weaving SQC "Loom Width & Picks
-- (R-08-21)" tab (juteSQC/weaving), which writes jute_sqc_weaving_pick (the table
-- weaving efficiency reads). role_menu_map rows are left in place (an inactive
-- menu never renders); table data in jute_sqc_width_picks* stays for history.
--
-- Tenant-agnostic (matches by menu_path). Applied: dev3 2026-08-21.
-- sls: run at WHICHEVER repo deploys FIRST — the new BE alone deletes the
-- page's endpoints (menu would point at 404 APIs), the new FE alone deletes
-- the route (menu would point at a 404 page). Either way the menu must go
-- when the first half ships. NOTE deploy order for the repos themselves:
-- BE BEFORE (or with) FE — the new PickForm sends picks_dm only, which the
-- old BE rejects (picks was required).
--
-- Rollback:
--   UPDATE menu_mst SET active = 1 WHERE menu_path = 'juteSQC/widthPicks';

UPDATE menu_mst
SET active = 0
WHERE menu_path = 'juteSQC/widthPicks';
