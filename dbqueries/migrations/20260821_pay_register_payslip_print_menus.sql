-- Seed the Pay Register Print and Payslip Print report menus in the sls tenant
-- DB (children of HRMS Reports 955), mirroring the role access rows of the
-- existing Full Attendance report (menu_id 956) — the sls role convention.
-- Idempotent: skips menu paths already present.
-- Target DB: sls (run via the run-migration flow).

INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by)
SELECT t.menu_name, t.menu_path, 955, 'assessment', 1, 1, t.order_by
FROM (
    SELECT 'Pay Register Print' AS menu_name, 'hrms/hrmsreports/payRegisterPrint' AS menu_path, 210 AS order_by
    UNION ALL SELECT 'Payslip Print', 'hrms/hrmsreports/payslipPrint', 220
) AS t
WHERE NOT EXISTS (SELECT 1 FROM menu_mst m WHERE m.menu_path = t.menu_path);

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT src.role_id, m.menu_id, src.access_type_id, 4
FROM menu_mst m
JOIN (
    SELECT DISTINCT role_id, access_type_id
    FROM role_menu_map WHERE menu_id = 956
) AS src
WHERE m.menu_parent_id = 955
    AND m.menu_path IN (
        'hrms/hrmsreports/payRegisterPrint',
        'hrms/hrmsreports/payslipPrint'
    )
    AND NOT EXISTS (
        SELECT 1 FROM role_menu_map r
        WHERE r.menu_id = m.menu_id AND r.role_id = src.role_id
            AND r.access_type_id = src.access_type_id
    );
