-- =============================================================================
-- Migration: Jute Production Planning -- menu wiring (SQL only)
-- =============================================================================
-- Date: 2026-08-08
-- Spec: vowerp3ui/docs/claude/modules/jute-production/production-planning/SPEC.md
--   section 5 (Menu wiring). Table DDL and backend/frontend code for this
--   feature are separate migrations/PRs -- this file ONLY adds sidebar menu
--   rows and their role grants.
--
-- Adds two Portal sidebar entries under the existing 'Jute Production' parent
-- module for the new Jute Production Planning feature:
--   'Production Planning' -> /dashboardportal/juteProduction/productionPlanning
--   'Planning Norms'      -> /dashboardportal/juteProduction/masters/planningNorms
--
-- THIS FILE TOUCHES TWO DATABASES -- run each section against its own
-- target, manually, via the pymysql runner described in CLAUDE.md
-- ('Running migrations' / the run-migration skill). There is no mysql CLI on
-- dev machines and the dev3/vowconsole3 hosts are NOT reachable from the
-- sandbox this file was authored in, so nothing below has been executed --
-- run it by hand and verify with the queries at the bottom.
--
--   SECTION A (below) -> vowconsole3 : portal_menu_mst template rows only.
--                        This is the master template consulted when a brand
--                        new tenant gets provisioned. It has no per-role
--                        grant table of its own (role_menu_map lives in each
--                        tenant DB, not vowconsole3), so Section A never
--                        touches role grants.
--   SECTION B (below) -> dev3        : menu_mst rows + role_menu_map grants,
--                        the actual Portal sidebar dev3 users see today.
--
-- DEFENSIVE-BY-CONSTRUCTION: nothing below hardcodes a menu_id, module_id,
-- or role_id -- every id is resolved at run time from the existing
-- 'Jute Production' parent row (its own menu_id / module id / menu_type_id)
-- or from whichever roles already hold a grant on a Jute Production menu, so
-- the same script is safe to promote to other tenants later without editing.
-- If the 'Jute Production' sibling anchor does not resolve on a given target
-- DB, the derived table is empty and the INSERT becomes a silent no-op --
-- always run the VERIFICATION queries at the bottom after applying.
--
-- Idempotent: every INSERT is guarded by NOT EXISTS. menu_name is UNIQUE in
-- both menu_mst and portal_menu_mst, so the guard is a simple name check.
-- role_menu_map is guarded on the (role_id, menu_id, access_type_id) triple.
-- Safe to re-run -- a partially-applied prior run just fills in the rest.
--
-- RUNNER NOTE: the pymysql runner (CLAUDE.md 'Running migrations') splits
-- this file on the literal semicolon character and skips any chunk whose
-- trimmed text starts with '--'. Every comment block in this file, no matter
-- how short, is therefore terminated by a lone semicolon on its own line so
-- it becomes its own (skipped) chunk and the following real statement
-- starts a clean chunk of its own. Comment prose in this file never
-- contains a mid-line semicolon for the same reason -- an accidental one
-- would split a comment block early and leave a stray non-comment-looking
-- fragment for the runner to execute.
--
-- POST-APPLY: Portal sidebars cache their menu tree in localStorage
-- (SidebarContext) -- users, including QA, MUST log out/re-login (or clear
-- the cached menu) before the two new entries appear.
--
-- Run this AFTER the jute_prod_planning table migration and the backend
-- router registration (SPEC sections 2-3) are live, so the routes these
-- menus point to actually resolve by the time anyone clicks them.
;

-- =============================================================================
-- SECTION A -- vowconsole3 (portal_menu_mst master template)
-- =============================================================================
-- portal_menu_mst columns: menu_name (UNIQUE), menu_path, active,
-- menu_parent_id, menu_type_id (NOT NULL here, unlike tenant menu_mst where
-- it is nullable, so it is inherited from the parent row rather than set to
-- NULL), menu_icon, module_id (NOT NULL, FK -> con_module_masters), order_by.
-- Parent id / menu_type_id / module_id are all inherited from the existing
-- 'Jute Production' template row. order_by is placed after the current max
-- sibling under that same parent.
;

-- A1. Production Planning
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Production Planning', CONCAT(p.menu_path, '/productionPlanning'), 1, p.menu_id, p.menu_type_id, 'event_note',
       p.module_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM portal_menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Production Planning');

-- A2. Planning Norms
;
INSERT INTO portal_menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_id, order_by)
SELECT 'Planning Norms', CONCAT(p.menu_path, '/masters/planningNorms'), 1, p.menu_id, p.menu_type_id, 'tune',
       p.module_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM portal_menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path, menu_type_id, module_id FROM portal_menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM portal_menu_mst WHERE menu_name = 'Planning Norms');

-- =============================================================================
-- SECTION B -- dev3 (tenant DB: menu_mst + role_menu_map)
-- =============================================================================
-- menu_mst columns match every other Jute Production child seed
-- (seed_beaming_menu.sql, seed_drawing_menu.sql, seed_winding_menu.sql,
-- seed_stoppage_menu.sql): menu_type_id NULL (matches the sibling rows'
-- convention -- nullable here, unlike portal_menu_mst), module_mst_id
-- inherited from the 'Jute Production' parent row itself, menu_path derived
-- via CONCAT off the parent's own path so this also works unmodified on a
-- tenant that stores an absolute '/dashboardportal/...' path (e.g. sls),
-- unlike dev3's relative 'juteProduction' style. order_by is placed after
-- the current max sibling under the same parent.
;

-- B1. menu_mst -- Production Planning
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Production Planning', CONCAT(p.menu_path, '/productionPlanning'), 1, p.menu_id, NULL, 'event_note',
       p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Production Planning');

-- B2. menu_mst -- Planning Norms
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Planning Norms', CONCAT(p.menu_path, '/masters/planningNorms'), 1, p.menu_id, NULL, 'tune',
       p.module_mst_id,
       (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM menu_mst c WHERE c.menu_parent_id = p.menu_id)
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Production' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Planning Norms');

-- role_menu_map -- grant every role that currently holds ANY access on an
-- existing Jute Production menu (the 'Jute Production' parent row itself, or
-- one of its direct children -- Spreader Production, Spinning Production,
-- Weaving Production, and so on) the FULL action level on both new menus.
--
-- access_type is a CUMULATIVE LEVEL, not a set of independent permissions:
--   1 = read, 2 = print, 3 = write, 4 = edit
-- and role_menu_map carries exactly ONE row per (role_id, menu_id) -- the
-- tenant-admin role-menu mapping UI (expandableMenuDropdown.tsx) renders one
-- <select> per menu bound to that single value, and the menu-tree query joins
-- role_menu_map without DISTINCT. Inserting one row per level (the original
-- version of this file did) returns the same menu_id four times and breaks
-- that UI with a duplicate-React-key error. 'Full action level' therefore
-- means access_type_id = 4, one row -- matching every sibling Jute Production
-- menu and the convention in seed_price_enquiry_menu.sql /
-- add_jute_sqc_bag_check_menu.sql.
--
-- updated_by = 1 (system/admin seed user, same convention as
-- seed_amcl_enquiry_menus.sql). Idempotent per the
-- (role_id, menu_id, access_type_id) triple.
;

-- B3. role_menu_map -- Production Planning, access level 4 (edit)
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, 4, 1, NOW()
FROM (
    SELECT DISTINCT rmm.role_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN menu_mst jp ON jp.menu_name = 'Jute Production'
    WHERE existing.menu_id = jp.menu_id OR existing.menu_parent_id = jp.menu_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Production Planning') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- B4. role_menu_map -- Planning Norms, access level 4 (edit)
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, 4, 1, NOW()
FROM (
    SELECT DISTINCT rmm.role_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN menu_mst jp ON jp.menu_name = 'Jute Production'
    WHERE existing.menu_id = jp.menu_id OR existing.menu_parent_id = jp.menu_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_name = 'Planning Norms') m
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id
);

-- =============================================================================
-- VERIFICATION -- run manually against each target DB after applying
-- =============================================================================
--
--   -- dev3
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, module_mst_id, order_by
--     FROM menu_mst WHERE menu_name IN ('Production Planning', 'Planning Norms')
--
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_name IN ('Production Planning', 'Planning Norms')
--    ORDER BY r.role_id, m.menu_name, r.access_type_id
--
--   -- vowconsole3
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, module_id, order_by
--     FROM portal_menu_mst WHERE menu_name IN ('Production Planning', 'Planning Norms')
--
-- Then log in to dev3 as a user holding one of the granted roles and confirm
-- 'Production Planning' and 'Planning Norms' render under 'Jute Production'
-- in the Portal sidebar and route correctly (re-login first, see POST-APPLY
-- above). A user WITHOUT a granted role must NOT see either entry.
-- =============================================================================

-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- dev3:
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_name IN ('Production Planning', 'Planning Norms')
--   DELETE FROM menu_mst WHERE menu_name IN ('Production Planning', 'Planning Norms')
--
-- vowconsole3:
--   DELETE FROM portal_menu_mst WHERE menu_name IN ('Production Planning', 'Planning Norms')
-- =============================================================================
