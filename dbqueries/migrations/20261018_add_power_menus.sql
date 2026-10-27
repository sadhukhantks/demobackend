-- =============================================================================
-- Migration: ITM-004 Power readings -- portal menu rows + grants (TENANT DBs)
-- =============================================================================
-- Date: 2026-10-18
-- TARGET: tenant DBs -- dev3 FIRST. sls only on the owner's go.
-- Pair:   20261018_add_power_menus_console.sql (vowconsole3 template rows, run once)
-- Tables: 20261018_jute_power_reading.sql must already be applied on the tenant
--         so the pages have something to read.
-- Spec:   Empire Jute Mill AI GM and team/itmis/prompts/ITM-004-power-readings.md
--
-- ADDS TWO menu_mst ROWS under the existing Jute Mechanical & Maintenance tree
-- (seeded by seed_jute_mech_menus_tenant.sql, whose conventions this follows):
--
--   level 2  'Power Readings'    juteMechanical/powerReading          order 245
--            sidebar entry page, between Machine History (240) and
--            Maintenance Masters (250)
--   level 3  'Power & DG Report' juteMechanical/reports/powerReport   order 273
--            report = 1 so it appears in the Maintenance Reports hub dropdown
--            (next free slot after Open Breakdown Board, 272). Level-3 rows
--            never reach the sidebar -- that is by design, see the seed file.
--
-- Both menu_path values must match the Next.js route folders exactly:
--   src/app/dashboardportal/juteMechanical/powerReading/page.tsx
--   src/app/dashboardportal/juteMechanical/reports/powerReport/page.tsx
--
-- GRANTS (role_menu_map, one row per (role_id, menu_id), access_type_id is a
-- cumulative level: 1 read, 2 print, 3 write, 4 edit). The LEVEL IS THE
-- CAPABILITY the backend reads (src/juteMechanical/constants.py):
--   3 write = may key readings inside the 7-day window
--   4 edit  = may also change / delete readings older than 7 days (the lock
--             override, resolved server-side through the user's menu access
--             level -- there is no role-id list in code any more)
--
--   (b) the spec roles, matched by roles_mst.role_name (case-insensitive) --
--       NEVER by role_id, which is per tenant DB (sls 13 / 14 / 55 are
--       'Super Admin' 1 / 'MIS ADMIN' 4 / nothing on dev3):
--         level 4  'slssuperadmin' (sls 13), 'super admin' (dev3 1), 'mis admin'
--                  (sls 14, dev3 4)
--         level 3  'mechanical' (sls 55, the electricians -- key, no override)
--       Only where the name exists, so a tenant without a name is a no-op for
--       it. Runs BEFORE the copy so these roles land at their level whatever
--       the sibling holds.
--   (a) every other role holding the sibling page gets VIEW (1) on the entry
--       page -- production roles see the readings, they do not key them (the
--       spec: "entry by 55 / 14 / 13, view for all production roles"). The
--       report row copies the sibling's level as-is (a report has no write).
--         entry page  <- 'juteMechanical/breakdown'                 (Breakdown Entry)
--         report row  <- 'juteMechanical/reports/breakdownRegister' (Breakdown Register)
--       On dev3: Super Admin (1) and MIS ADMIN (4) at level 4 on the entry
--       page by name, roles 3 / 12 at level 1 by the copy.
--
-- DEFENSIVE BY CONSTRUCTION: no menu_id, module_mst_id or role_id is
-- hardcoded anywhere. Parents are resolved by menu_name (UNIQUE table-wide),
-- the spec roles by role_name. If an anchor fails to resolve the derived
-- table is empty and the INSERT is a silent no-op -- ALWAYS run the
-- VERIFICATION queries at the end.
--
-- IDEMPOTENT: every INSERT is guarded by NOT EXISTS (menu_name for menu_mst,
-- (role_id, menu_id) for role_menu_map). Safe to re-run.
--
-- POST-APPLY: every portal user MUST LOG OUT AND BACK IN. The sidebar caches
-- the whole menu tree in localStorage (SidebarContext), so nothing new
-- appears until the cached tree is refetched.
--
-- PREREQUISITE: menu_mst must carry the `report` column (add_report_to_menu_mst.sql,
-- present on dev3 and sls). Check with: SHOW COLUMNS FROM menu_mst LIKE 'report'
--
-- RUNNER NOTE: the pymysql runner (run-migration skill) splits this file on
-- the literal semicolon character and skips any chunk whose trimmed text
-- starts with '--'. Every comment block is therefore terminated by a lone
-- semicolon on its own line, and no comment prose contains a mid-line
-- semicolon. There are no DELIMITER blocks.
;

-- -----------------------------------------------------------------------------
-- COLLISION PRE-CHECK -- run BEFORE applying. menu_name is UNIQUE, so a name
-- already taken by an unrelated menu silently turns its INSERT into a no-op.
-- Expect ZERO rows back (checked read-only on dev3 on 2026-10-18: none).
-- -----------------------------------------------------------------------------
--   SELECT menu_id, menu_name, menu_path FROM menu_mst
--    WHERE menu_name IN ('Power Readings', 'Power & DG Report')
;

-- =============================================================================
-- STEP 1 -- menu_mst : level-2 entry page under 'Jute Mechanical & Maintenance'
-- =============================================================================
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Power Readings', CONCAT(p.menu_path, '/powerReading'), 1, p.menu_id, NULL, 'bolt',
       p.module_mst_id, 245, 0
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Jute Mechanical & Maintenance' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Power Readings');

-- =============================================================================
-- STEP 2 -- menu_mst : level-3 report row under 'Maintenance Reports'
-- =============================================================================
;
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Power & DG Report', CONCAT(p.menu_path, '/powerReport'), 1, p.menu_id, NULL, 'assessment',
       p.module_mst_id, 273, 1
FROM (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_name = 'Maintenance Reports' LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_name = 'Power & DG Report');

-- =============================================================================
-- STEP 3 -- role_menu_map : entry page
-- =============================================================================
-- 3(b) the spec roles BY NAME: admins at 4 (key + lock override), the
--      electricians' role at 3 (key only), where the name exists.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT rl.role_id, me.menu_id,
       CASE WHEN LOWER(TRIM(rl.role_name)) IN ('slssuperadmin', 'super admin', 'mis admin') THEN 4 ELSE 3 END,
       1, NOW()
FROM roles_mst rl
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteMechanical/powerReading' LIMIT 1) me
WHERE LOWER(TRIM(rl.role_name)) IN ('slssuperadmin', 'super admin', 'mis admin', 'mechanical')
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map x
      WHERE x.role_id = rl.role_id AND x.menu_id = me.menu_id
  );

-- 3(a) every other role holding the sibling Breakdown Entry gets VIEW (1):
--      production roles read the readings, only 3(b)'s roles key them. The
--      sibling's level is never copied here -- Breakdown Entry at 4 must not
--      become a lock override on power readings.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT sib.role_id, me.menu_id, 1, 1, NOW()
FROM (
    SELECT r.role_id
    FROM role_menu_map r
    JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteMechanical/breakdown'
    GROUP BY r.role_id
) sib
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteMechanical/powerReading' LIMIT 1) me
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = sib.role_id AND x.menu_id = me.menu_id
);

-- =============================================================================
-- STEP 4 -- role_menu_map : report row
-- =============================================================================
-- 4(b) the spec roles BY NAME at level 4 on the report row (a report has no
--      write semantics, 4 just keeps print / export open), where the name exists.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT rl.role_id, me.menu_id, 4, 1, NOW()
FROM roles_mst rl
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteMechanical/reports/powerReport' LIMIT 1) me
WHERE LOWER(TRIM(rl.role_name)) IN ('slssuperadmin', 'super admin', 'mis admin', 'mechanical')
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map x
      WHERE x.role_id = rl.role_id AND x.menu_id = me.menu_id
  );

-- 4(a) every role holding the sibling Breakdown Register, at its MAX level there.
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT sib.role_id, me.menu_id, sib.access_type_id, 1, NOW()
FROM (
    SELECT r.role_id, MAX(r.access_type_id) AS access_type_id
    FROM role_menu_map r
    JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_path = 'juteMechanical/reports/breakdownRegister'
    GROUP BY r.role_id
) sib
JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteMechanical/reports/powerReport' LIMIT 1) me
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.role_id = sib.role_id AND x.menu_id = me.menu_id
);

-- =============================================================================
-- VERIFICATION -- run manually against each target tenant after applying
-- =============================================================================
--   -- 1. the two rows: EXPECT (juteMechanical/powerReading, 245, 0) under
--   --    'Jute Mechanical & Maintenance' and (juteMechanical/reports/powerReport,
--   --    273, 1) under 'Maintenance Reports'
--   SELECT m.menu_id, m.menu_name, m.menu_path, p.menu_name AS parent_name,
--          m.order_by, m.report, m.module_mst_id, m.active
--     FROM menu_mst m
--     LEFT JOIN menu_mst p ON p.menu_id = m.menu_parent_id
--    WHERE m.menu_path LIKE 'juteMechanical/%power%'
--    ORDER BY m.order_by
--
--   -- 2. grants per role. EXPECT on the ENTRY page:
--   --      dev3  role 1 'Super Admin' 4, role 4 'MIS ADMIN' 4 (by name),
--   --            roles 3 / 12 at 1 (copied from Breakdown Entry as view)
--   --      sls   13 'slssuperadmin' 4, 14 'MIS ADMIN' 4, 55 'Mechanical' 3,
--   --            every other Breakdown Entry role at 1
--   --    and on the REPORT row the named roles at 4 plus the Breakdown
--   --    Register roles at their own level.
--   SELECT m.menu_path, r.role_id, rl.role_name, r.access_type_id
--     FROM role_menu_map r
--     JOIN menu_mst m ON m.menu_id = r.menu_id
--     JOIN roles_mst rl ON rl.role_id = r.role_id
--    WHERE m.menu_path IN ('juteMechanical/powerReading', 'juteMechanical/reports/powerReport')
--    ORDER BY m.menu_path, r.role_id
--
--   -- 3. duplicate guard: must return ZERO rows
--   SELECT r.role_id, r.menu_id, COUNT(*) AS c
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path LIKE 'juteMechanical/%power%'
--    GROUP BY r.role_id, r.menu_id HAVING c > 1
--
-- Then re-login as a user holding one of the granted roles and confirm:
--   * 'Power Readings' shows under Jute Mechanical & Maintenance in the sidebar
--   * the Maintenance Reports hub dropdown lists 'Power & DG Report'
--   * a level-1 role sees the grid read-only, a level-3 role can save a reading
--     for yesterday but gets 403 on a date older than 7 days, a level-4 role
--     can save that older date too
--   * a user WITHOUT a granted role is redirected away from both routes
;

-- =============================================================================
-- ROLLBACK -- run against each tenant this was applied to
-- =============================================================================
--   DELETE r FROM role_menu_map r
--     JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path IN ('juteMechanical/powerReading', 'juteMechanical/reports/powerReport')
--
--   DELETE FROM menu_mst
--    WHERE menu_path IN ('juteMechanical/powerReading', 'juteMechanical/reports/powerReport')
-- =============================================================================
