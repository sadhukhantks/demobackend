-- TKT-000155 — split the Jute Stock report into two report menus.
--
--   Jute Stock (Production)  jutePurchase/reports/juteStock          (existing row, renamed)
--   Jute Stock (Accounts)    jutePurchase/reports/juteStockAccounts  (new row)
--
-- Target: a TENANT DB. Run on dev3 first, then sls (and any other tenant that
-- has the jute reports hub). NOT executed by the triage agent — awaiting owner
-- approval; apply with the run-migration skill, which asks for the target DB.
--
-- menu_path MUST match the Next.js route folder:
--   src/app/dashboardportal/jutePurchase/reports/juteStockAccounts/page.tsx
-- Backend endpoint (unchanged): GET /api/juteReports/stock?basis=accounts
--
-- The hub dropdown lists menu_mst rows with report = 1 under the hub's own
-- menu row, so parent/module are resolved from the existing Jute Stock row
-- rather than hardcoded ids (same pattern as
-- add_jute_issue_planner_report_menu.sql). Idempotent: safe to re-run.
--
-- Until this runs the new page is still reachable by URL (the permission check
-- falls back to the parent path jutePurchase/reports); it just is not listed
-- in the Reports dropdown.

-- 1. Rename the existing report.
UPDATE menu_mst
SET menu_name = 'Jute Stock (Production)'
WHERE menu_path = 'jutePurchase/reports/juteStock'
  AND menu_name = 'Jute Stock Report';

-- 2. Add the Accounts report directly after it (juteStock = 20, batchCost = 30).
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Stock (Accounts)', 'jutePurchase/reports/juteStockAccounts', 1, s.menu_parent_id, NULL, 'assessment', s.module_mst_id,
       COALESCE(s.order_by, 20) + 5,
       1
FROM (
    SELECT menu_parent_id, module_mst_id, order_by
    FROM menu_mst
    WHERE menu_path = 'jutePurchase/reports/juteStock'
    LIMIT 1
) s
WHERE NOT EXISTS (
    SELECT 1 FROM menu_mst WHERE menu_path = 'jutePurchase/reports/juteStockAccounts'
);

SET @m = (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/juteStockAccounts' LIMIT 1);

-- 3. Role grants. DEFAULT BELOW = same roles/access the Jute Stock report has
--    today (22 roles on sls, 3 on dev3), so nobody loses the accounts figures
--    they could already see through the old Actual/Accounts toggle.
--    OWNER DECISION: if the Accounts report should be limited to accounts /
--    commercial roles, replace this statement with an explicit role list
--    before running on sls.
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT rmm.role_id, @m, rmm.access_type_id, 1, NOW()
FROM role_menu_map rmm
INNER JOIN menu_mst sib
        ON sib.menu_id = rmm.menu_id
       AND sib.menu_path = 'jutePurchase/reports/juteStock'
WHERE @m IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map x
      WHERE x.menu_id = @m AND x.role_id = rmm.role_id
  );

-- Users must log out and in again (the permission token and the hub's report
-- list are built at login / first load).

-- Rollback:
-- DELETE rmm FROM role_menu_map rmm
--   JOIN menu_mst m ON m.menu_id = rmm.menu_id
--   WHERE m.menu_path = 'jutePurchase/reports/juteStockAccounts';
-- DELETE FROM menu_mst WHERE menu_path = 'jutePurchase/reports/juteStockAccounts';
-- UPDATE menu_mst SET menu_name = 'Jute Stock Report'
--   WHERE menu_path = 'jutePurchase/reports/juteStock';
