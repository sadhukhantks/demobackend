-- =============================================================================
-- Migration: move 'Outstanding PO Position' from the Production Reports hub to
--            the Jute Report hub (jutePurchase/reports)
-- =============================================================================
-- Date: 2026-09-25
-- TARGET: tenants where the first version of seed_jute_po_outstanding_position_menu.sql
-- was run under juteProduction/report (dev3, sls). Fresh tenants only need the seed.
--
-- Re-parents the existing row (keeps its menu_id), then replaces its role grants
-- with the Jute Report hub's (MAX level held per role on the hub or any child).
;
UPDATE menu_mst m
JOIN (SELECT menu_id, menu_path, module_mst_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports' LIMIT 1) p
SET m.menu_path = CONCAT(p.menu_path, '/poOutstandingPosition'),
    m.menu_parent_id = p.menu_id,
    m.module_mst_id = p.module_mst_id,
    m.order_by = (SELECT COALESCE(MAX(c.order_by), 0) + 1 FROM (SELECT order_by FROM menu_mst WHERE menu_parent_id = p.menu_id) c)
WHERE m.menu_path = 'juteProduction/report/poOutstandingPosition';

;
DELETE FROM role_menu_map
WHERE menu_id IN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition');

;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst existing ON existing.menu_id = rmm.menu_id
    JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports' LIMIT 1) hub
      ON existing.menu_id = hub.menu_id OR existing.menu_parent_id = hub.menu_id
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'jutePurchase/reports/poOutstandingPosition') m;

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, module_mst_id, report, order_by
FROM menu_mst
WHERE menu_name = 'Outstanding PO Position';

-- ROLLBACK: rerun with the two paths swapped and hub 'juteProduction/report'.
;
