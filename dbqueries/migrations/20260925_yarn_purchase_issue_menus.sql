-- =============================================================================
-- Migration: sidebar menus for Yarn Purchase and Yarn Issue (Jute Procurement)
-- =============================================================================
-- Date: 2026-09-25   Phase 1 of
-- docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md
-- TARGET: tenant DB only (dev3, then sls with the pages). vowconsole3
-- portal_menu_mst untouched, as for every other jutePurchase sibling here.
--
-- Nothing hardcodes an id: the parent is the 'Jute Procurement' root
-- (menu_path 'jutePurchase'), the module resolves by name, and role access
-- mirrors whoever can already reach 'Jute Issue'. Idempotent via NOT EXISTS.
-- The report menu (Purchased Yarn Ledger) is added in phase 5 with its page.
--
-- POST-APPLY: sidebars cache in localStorage — users must log out and in.
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Yarn Purchase', CONCAT(p.menu_path, '/yarnPurchase'), 1, p.menu_id, NULL, 'local_shipping',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Procurement' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       60
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_path = 'jutePurchase' AND menu_parent_id IS NULL LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = CONCAT(p.menu_path, '/yarnPurchase'));

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Yarn Issue', CONCAT(p.menu_path, '/yarnIssue'), 1, p.menu_id, NULL, 'outbox',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Procurement' AND active = 1 ORDER BY module_mst_id LIMIT 1),
       61
FROM (SELECT menu_id, menu_path FROM menu_mst WHERE menu_path = 'jutePurchase' AND menu_parent_id IS NULL LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = CONCAT(p.menu_path, '/yarnIssue'));

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT rmm.role_id, m.menu_id, rmm.access_type_id, 1
FROM role_menu_map rmm
JOIN menu_mst ji ON ji.menu_id = rmm.menu_id AND ji.menu_path = 'jutePurchase/juteIssue'
JOIN menu_mst m ON m.menu_path IN ('jutePurchase/yarnPurchase', 'jutePurchase/yarnIssue')
WHERE NOT EXISTS (
    SELECT 1 FROM role_menu_map x
    WHERE x.menu_id = m.menu_id AND x.role_id = rmm.role_id AND x.access_type_id = rmm.access_type_id
);

-- =============================================================================
-- VERIFICATION
--   SELECT menu_id, menu_name, menu_path, menu_parent_id, order_by, module_mst_id
--     FROM menu_mst WHERE menu_path IN ('jutePurchase/yarnPurchase', 'jutePurchase/yarnIssue');
--   SELECT r.role_id, m.menu_name, r.access_type_id
--     FROM role_menu_map r JOIN menu_mst m ON m.menu_id = r.menu_id
--    WHERE m.menu_path IN ('jutePurchase/yarnPurchase', 'jutePurchase/yarnIssue')
--    ORDER BY m.menu_name, r.role_id;
-- ROLLBACK
--   DELETE rmm FROM role_menu_map rmm JOIN menu_mst m ON m.menu_id = rmm.menu_id
--    WHERE m.menu_path IN ('jutePurchase/yarnPurchase', 'jutePurchase/yarnIssue');
--   DELETE FROM menu_mst WHERE menu_path IN ('jutePurchase/yarnPurchase', 'jutePurchase/yarnIssue');
-- =============================================================================
