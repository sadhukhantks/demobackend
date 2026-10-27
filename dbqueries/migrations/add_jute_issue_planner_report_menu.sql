-- Add the "Jute Issue Planner" report menu under the Jute Reports hub in a
-- tenant DB (default target: dev3).
--
-- menu_path MUST match the Next.js route folder:
--   src/app/dashboardportal/jutePurchase/reports/issuePlanner/page.tsx
-- Backend endpoint: GET /api/juteReports/issue-planner (vowerp3be).
--
-- The hub dropdown (jutePurchase/reports/page.tsx) lists menu_mst rows with
-- report = 1 whose menu_parent_id is the hub's own menu row, so parent,
-- module and role grants are resolved from the existing Jute Stock report
-- sibling instead of hardcoded ids — the script works on any tenant that
-- already has the jute reports hub seeded.

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Jute Issue Planner', 'jutePurchase/reports/issuePlanner', 1, s.menu_parent_id, NULL, 'assessment', s.module_mst_id,
       (SELECT COALESCE(MAX(m2.order_by), 0) + 10 FROM menu_mst m2 WHERE m2.menu_parent_id = s.menu_parent_id),
       1
FROM (
    SELECT menu_parent_id, module_mst_id
    FROM menu_mst
    WHERE menu_path = 'jutePurchase/reports/juteStock'
    LIMIT 1
) s
WHERE NOT EXISTS (
    SELECT 1 FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issuePlanner'
);

SET @m = (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issuePlanner' LIMIT 1);

-- Grant the same roles/access the Jute Stock report already has.
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

-- NOTE: per the menu-system convention a matching template row belongs in
-- vowconsole3.portal_menu_mst as well — run the equivalent insert there (via
-- the add-menu skill) when templating this report for new tenants.

-- Rollback:
-- DELETE rmm FROM role_menu_map rmm
--   JOIN menu_mst m ON m.menu_id = rmm.menu_id
--   WHERE m.menu_path = 'jutePurchase/reports/issuePlanner';
-- DELETE FROM menu_mst WHERE menu_path = 'jutePurchase/reports/issuePlanner';
