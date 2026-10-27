-- Migration: attendance_open_close.sql
-- Backs the HRMS Attendance Open/Close page (hrms/AttendanceOpenClose), which
-- flips a whole (branch, date, spell, department) slice of daily_attendance
-- between Open (status_id 1) and Closed (status_id 3).
--
-- 1. attendance_open_close_password - the shared secret the page prompts for
--    before it rewrites a day's records. Single row per tenant, stored as
--    plain text by design (an operational gate, NOT a user credential - it is
--    never used to authenticate anybody, the caller is already authenticated
--    by the portal JWT).
-- 2. menu row + Super Admin role mapping so the page is reachable and passes
--    the portal per-route permission check in src/middleware.ts.
--
-- Target: dev3 (run against sls separately once QA signs off)
--
-- EXECUTOR CONSTRAINT: no semicolon in comment prose, header terminated by the
-- lone semicolon below.
--
-- ROLLBACK (as comments):
-- DELETE FROM role_menu_map WHERE menu_id = (SELECT menu_id FROM menu_mst WHERE menu_path = 'hrms/AttendanceOpenClose')
-- DELETE FROM menu_mst WHERE menu_path = 'hrms/AttendanceOpenClose'
-- DROP TABLE attendance_open_close_password
;

CREATE TABLE IF NOT EXISTS attendance_open_close_password (
    id                INT NOT NULL AUTO_INCREMENT,
    password          VARCHAR(100) NOT NULL
                      COMMENT 'plain text by design - operational gate for the bulk open/close, not a login credential',
    updated_by        INT NULL,
    updated_date_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO attendance_open_close_password (password, updated_by)
SELECT 'vow@123', 1 FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM attendance_open_close_password);

-- Sits under the HRMS parent menu (741), same shape as Attendance Calendar (790).
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, module_mst_id, order_by)
SELECT 'Attendance Open Close', 'hrms/AttendanceOpenClose', 1, 741, 2, 1, 2 FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM menu_mst WHERE menu_path = 'hrms/AttendanceOpenClose');

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT 1, m.menu_id, 4, 1, NOW()
FROM menu_mst m
WHERE m.menu_path = 'hrms/AttendanceOpenClose'
  AND NOT EXISTS (
      SELECT 1 FROM role_menu_map rm WHERE rm.menu_id = m.menu_id AND rm.role_id = 1
  )
