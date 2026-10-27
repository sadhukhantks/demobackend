-- Migration: fix Jute Production Planning menu grants — one row at access level 4.
-- Date: 2026-08-09
-- Applies to: dev3, sls (any tenant where 20260808_jute_production_planning_menu.sql ran).
--
-- BUG: 20260808_jute_production_planning_menu.sql sections B3/B4 CROSS JOINed four
-- access_type_id values (1,2,3,4) per (role, menu), reading the SPEC's "full action
-- level" as a SET of permissions. It is not. access_type is a cumulative LEVEL —
--   1 = read, 2 = print, 3 = write, 4 = edit
-- and every other row in role_menu_map is exactly one row per (role, menu); the
-- tenant-admin role-menu mapping UI (expandableMenuDropdown.tsx) renders a single
-- <select> per menu bound to that one value. Four rows made the menu list return the
-- same menu_id four times, which surfaced as React "Encountered two children with
-- the same key, 983".
--
-- Fix: keep access_type_id = 4 (edit — the full level, implying the ones below it),
-- delete the redundant 1/2/3 rows. Idempotent, and a no-op on a tenant that never
-- received the buggy grants.
--
-- ROLLBACK: none needed — the deleted rows were never valid. To restore the buggy
-- shape, re-run sections B3/B4 of 20260808_jute_production_planning_menu.sql.

DELETE r FROM role_menu_map r
JOIN menu_mst m ON m.menu_id = r.menu_id
WHERE m.menu_name IN ('Production Planning', 'Planning Norms')
  AND r.access_type_id <> 4;

-- VERIFICATION (expect one row per role per menu, access_type_id = 4):
--   SELECT m.menu_name, COUNT(*) grants, COUNT(DISTINCT r.role_id) roles,
--          GROUP_CONCAT(DISTINCT r.access_type_id) levels
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Production Planning', 'Planning Norms')
--    GROUP BY m.menu_name
