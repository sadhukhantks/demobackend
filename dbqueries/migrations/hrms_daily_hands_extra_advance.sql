-- Daily Hands Complement + Daily Extra Advance Payment reports
-- (legacy Data-Portal "Other Entry/Reports" options 2 and 8).
--
-- Creates the one table the new schema was missing -- the extra-advance rate
-- slabs -- and seeds the two report menus under HRMS Reports.
-- Daily Hands Complement needs no table: the legacy EMPMILL12.dailyhandcomp
-- view is just daily_attendance pivoted, so the report queries it directly.
--
-- Idempotent. Target DB: dev3 / sls (run via the run-migration flow).


-- ---------------------------------------------------------------------------
-- 1. Extra advance rate slabs
-- ---------------------------------------------------------------------------
-- The legacy report hardcoded the payout in a CASE: 150 for 3-5 hours, 200 for
-- 5-7.5, 300 above that, and a pro-rata 450/day (300 on Sundays) for the two
-- S4 weaving occupations. Those are per-branch commercial terms, so they live
-- in a table instead of in the SQL.
--
-- Matching (see get_extra_advance_payment_query): rows whose window contains
-- the worked hours and whose eff_date has passed; most specific wins --
-- designation-specific over the catch-all (desig_id NULL), day-specific over
-- any-day (is_sunday NULL), then the newest eff_date.
CREATE TABLE IF NOT EXISTS extra_advance_rate (
    extra_adv_rate_id INT NOT NULL AUTO_INCREMENT,
    branch_id         INT NOT NULL,
    desig_id          BIGINT       DEFAULT NULL COMMENT 'NULL = any designation',
    min_hours         DECIMAL(5,2) NOT NULL DEFAULT 0.00,
    max_hours         DECIMAL(5,2) NOT NULL DEFAULT 24.00,
    is_sunday         TINYINT      DEFAULT NULL COMMENT 'NULL = any day, 1 = Sundays, 0 = other days',
    rate              DECIMAL(10,2) NOT NULL,
    pro_rata          TINYINT NOT NULL DEFAULT 0 COMMENT '1 = ROUND(rate / 8 * hours), 0 = flat rate',
    eff_date          DATE NOT NULL COMMENT 'effective from',
    is_active         TINYINT NOT NULL DEFAULT 1,
    updated_by        INT DEFAULT NULL,
    update_date_time  DATETIME DEFAULT NULL,
    PRIMARY KEY (extra_adv_rate_id),
    KEY idx_extra_adv_rate_lookup (branch_id, is_active, eff_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Legacy slabs for branch 29 (sls co_id 2 -- the mill the legacy report ran
-- for). Designations 78 / 79 are WEAVER(S4) / HELPER(S4); the migration kept
-- the legacy ids on this branch, so the legacy CASE maps across unchanged.
-- Other branches: add their own rows, the report shows nothing without them.
INSERT INTO extra_advance_rate
    (branch_id, desig_id, min_hours, max_hours, is_sunday, rate, pro_rata, eff_date)
SELECT * FROM (
    SELECT 29 b, NULL d, 3.00 mn, 4.99 mx, NULL s, 150.00 r, 0 p, '2020-01-01' e
    UNION ALL SELECT 29, NULL, 5.00,  7.49,  NULL, 200.00, 0, '2020-01-01'
    UNION ALL SELECT 29, NULL, 7.50, 24.00,  NULL, 300.00, 0, '2020-01-01'
    UNION ALL SELECT 29, 78,   3.00, 24.00,  0,    450.00, 1, '2020-01-01'
    UNION ALL SELECT 29, 78,   3.00, 24.00,  1,    300.00, 1, '2020-01-01'
    UNION ALL SELECT 29, 79,   3.00, 24.00,  0,    450.00, 1, '2020-01-01'
    UNION ALL SELECT 29, 79,   3.00, 24.00,  1,    300.00, 1, '2020-01-01'
) seed
WHERE NOT EXISTS (SELECT 1 FROM extra_advance_rate WHERE branch_id = 29)
  AND EXISTS (SELECT 1 FROM branch_mst WHERE branch_id = 29 AND co_id = 2);


-- ---------------------------------------------------------------------------
-- 2. Report menus under HRMS Reports
-- ---------------------------------------------------------------------------
-- Parent is resolved by path (menu ids differ per tenant: 955 on sls, 952 on
-- dev3), and role access is copied from the sibling report menus rather than
-- from the parent -- on sls the parent carries only 7 roles while every report
-- under it carries the same 20, and a new report should match its siblings.
INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by)
SELECT 'Daily Hands Complement', 'hrms/hrmsreports/dailyHandsComplement',
       p.menu_id, 'assessment', 1, 1, 230
FROM menu_mst p
WHERE p.menu_path = 'hrms/hrmsreports'
  AND NOT EXISTS (
      SELECT 1 FROM (SELECT menu_path FROM menu_mst) m
      WHERE m.menu_path = 'hrms/hrmsreports/dailyHandsComplement'
  );

INSERT INTO menu_mst (menu_name, menu_path, menu_parent_id, menu_icon, report, active, order_by)
SELECT 'Daily Extra Advance Payment', 'hrms/hrmsreports/extraAdvance',
       p.menu_id, 'assessment', 1, 1, 240
FROM menu_mst p
WHERE p.menu_path = 'hrms/hrmsreports'
  AND NOT EXISTS (
      SELECT 1 FROM (SELECT menu_path FROM menu_mst) m
      WHERE m.menu_path = 'hrms/hrmsreports/extraAdvance'
  );

-- Every (role, access type) any existing sibling report grants, deduped. The
-- siblings are uniform on both tenants, so this is their exact role set.
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT DISTINCT src.role_id, m.menu_id, src.access_type_id, 4
FROM menu_mst m
JOIN menu_mst p ON p.menu_path = 'hrms/hrmsreports'
JOIN (SELECT menu_id, menu_parent_id FROM menu_mst
      WHERE report = 1 AND active = 1) sib ON sib.menu_parent_id = p.menu_id
JOIN role_menu_map src ON src.menu_id = sib.menu_id
WHERE m.menu_path IN ('hrms/hrmsreports/dailyHandsComplement',
                      'hrms/hrmsreports/extraAdvance')
  AND sib.menu_id <> m.menu_id
  AND NOT EXISTS (
      SELECT 1 FROM (SELECT menu_id, role_id, access_type_id FROM role_menu_map) r
      WHERE r.menu_id = m.menu_id AND r.role_id = src.role_id
        AND r.access_type_id = src.access_type_id
  );
