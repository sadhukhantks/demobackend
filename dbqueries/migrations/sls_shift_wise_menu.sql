-- Seed the Shift Wise Attendance report menu in the sls tenant DB, mirroring
-- the role access rows of the existing Spell Wise Summary report (menu_id 977).
-- Idempotent: skips if the menu path is already present.
-- Target DB: sls (run via the run-migration flow).

INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by)
SELECT 'Shift Wise Attendance', 'hrms/hrmsreports/shiftWise', 955, 'assessment', 1, 1, 195
FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM menu_mst m WHERE m.menu_path = 'hrms/hrmsreports/shiftWise'
);

-- Give every role that can see Spell Wise Summary (977) the same access.
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT src.role_id, m.menu_id, src.access_type_id, 4
FROM menu_mst m
JOIN role_menu_map src ON src.menu_id = 977
WHERE m.menu_path = 'hrms/hrmsreports/shiftWise'
    AND NOT EXISTS (
        SELECT 1 FROM role_menu_map r
        WHERE r.menu_id = m.menu_id AND r.role_id = src.role_id
            AND r.access_type_id = src.access_type_id
    );
