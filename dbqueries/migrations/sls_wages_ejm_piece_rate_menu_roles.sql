-- Grant sidebar access to sls.menu_mst 1054 "Ejm Picee Occu Rate Entry"
-- (menu_path wages/ejm/pieceRateOccuEntry), which was seeded with zero
-- role_menu_map rows. Copies whatever its siblings under 1051 "Wages" already
-- carry, so it stays in step when those are widened.
--
-- Target DB: sls
--
-- ROLLBACK: DELETE FROM role_menu_map WHERE menu_id = 1054;

-- Both sides are derived tables so they materialise before the insert -- a bare
-- subquery on role_menu_map inside an INSERT INTO role_menu_map is refused.
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT sib.role_id, 1054, sib.access_type_id, 4, NOW()
FROM (
  SELECT r.role_id, MAX(r.access_type_id) AS access_type_id
  FROM role_menu_map r
  JOIN menu_mst m ON m.menu_id = r.menu_id AND m.menu_parent_id = 1051
  WHERE r.menu_id <> 1054
  GROUP BY r.role_id
) sib
LEFT JOIN (
  SELECT role_id FROM role_menu_map WHERE menu_id = 1054
) have ON have.role_id = sib.role_id
WHERE have.role_id IS NULL;
