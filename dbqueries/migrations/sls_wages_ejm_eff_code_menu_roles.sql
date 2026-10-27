-- Grant sidebar access to the "Wages" tree that already exists in sls.menu_mst:
--   1051 Wages (top level, menu_path 'Wages')
--   1052 Ejm Wages Eff Code (menu_path 'wages/ejm/effCodeEntry')
-- Both were seeded with zero role_menu_map rows, so the menu is invisible to
-- everyone. Mirrors the existing EJM-wages convention on sls: menu 987
-- "Ejm Wages Process" carries exactly role 13 at access_type_id 4 (edit).
--
-- ponytail: one role, copied from the sibling. Widen with the role-management
-- UI rather than more SQL when other roles actually need it.
-- Target DB: sls
--
-- ROLLBACK: DELETE FROM role_menu_map WHERE menu_id IN (1051, 1052) AND role_id = 13;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT 13, m.menu_id, 4, 4, NOW()
FROM menu_mst m
WHERE m.menu_id IN (1051, 1052)
  AND NOT EXISTS (
    SELECT 1 FROM role_menu_map r
    WHERE r.menu_id = m.menu_id AND r.role_id = 13
  );
