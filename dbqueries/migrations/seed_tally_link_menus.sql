-- =============================================================================
-- Migration: Tally Link Master menu (under portal Masters)
-- =============================================================================
-- Date: 2026-10-06
-- TARGET: tenant DB only (dev3 first; other tenants once BE+FE deploy there).
-- Run AFTER create_tally_link_mst.sql.
--
--   'Tally Link Master'  -> masters/tallyLink   (ordered after the last Masters sibling)
--
-- Role grants mirror each role's access on 'masters/partyMaster'.
-- Runner note: each comment block is terminated by a lone ';'.
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Tally Link Master', 'masters/tallyLink', 1, p.menu_id, p.menu_type_id, NULL, p.module_mst_id, p.next_order
FROM (SELECT h.menu_id, h.menu_type_id, h.module_mst_id,
             (SELECT COALESCE(MAX(c.order_by), 0) + 10 FROM menu_mst c WHERE c.menu_parent_id = h.menu_id) AS next_order
      FROM menu_mst h
      WHERE h.menu_path = 'masters' AND h.menu_parent_id IS NULL LIMIT 1) p
WHERE NOT EXISTS (SELECT 1 FROM (SELECT menu_path FROM menu_mst) m WHERE m.menu_path = 'masters/tallyLink');

-- role grants cloned from Party Master
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst pm ON pm.menu_id = rmm.menu_id AND pm.menu_path = 'masters/partyMaster'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst WHERE menu_path = 'masters/tallyLink') m
WHERE NOT EXISTS (SELECT 1 FROM (SELECT role_id, menu_id FROM role_menu_map) x
                  WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT m.menu_id, m.menu_name, m.menu_path, m.menu_parent_id, m.order_by,
       GROUP_CONCAT(CONCAT(r.role_id, ':', r.access_type_id) ORDER BY r.role_id) AS role_grants
FROM menu_mst m
LEFT JOIN role_menu_map r ON r.menu_id = m.menu_id
WHERE m.menu_path = 'masters/tallyLink'
GROUP BY m.menu_id, m.menu_name, m.menu_path, m.menu_parent_id, m.order_by;

-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN (SELECT menu_id FROM (SELECT menu_id FROM menu_mst
--   WHERE menu_path = 'masters/tallyLink') x);
-- DELETE FROM menu_mst WHERE menu_path = 'masters/tallyLink';
-- =============================================================================
;
