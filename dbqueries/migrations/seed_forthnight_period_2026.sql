-- Seed forthnight_period_mst for calendar 2026 (sls).
-- 1st-15th => type_of_period 1, 16th-month end => type_of_period 2.
-- Skips periods already present; also normalises the type of any existing 2026 row.

INSERT INTO forthnight_period_mst (start_date, end_date, type_of_period)
SELECT d.start_date, d.end_date, d.type_of_period
FROM (
    SELECT DATE('2026-01-01') AS start_date, DATE('2026-01-15') AS end_date, 1 AS type_of_period UNION ALL
    SELECT '2026-01-16', '2026-01-31', 2 UNION ALL
    SELECT '2026-02-01', '2026-02-15', 1 UNION ALL
    SELECT '2026-02-16', '2026-02-28', 2 UNION ALL
    SELECT '2026-03-01', '2026-03-15', 1 UNION ALL
    SELECT '2026-03-16', '2026-03-31', 2 UNION ALL
    SELECT '2026-04-01', '2026-04-15', 1 UNION ALL
    SELECT '2026-04-16', '2026-04-30', 2 UNION ALL
    SELECT '2026-05-01', '2026-05-15', 1 UNION ALL
    SELECT '2026-05-16', '2026-05-31', 2 UNION ALL
    SELECT '2026-06-01', '2026-06-15', 1 UNION ALL
    SELECT '2026-06-16', '2026-06-30', 2 UNION ALL
    SELECT '2026-07-01', '2026-07-15', 1 UNION ALL
    SELECT '2026-07-16', '2026-07-31', 2 UNION ALL
    SELECT '2026-08-01', '2026-08-15', 1 UNION ALL
    SELECT '2026-08-16', '2026-08-31', 2 UNION ALL
    SELECT '2026-09-01', '2026-09-15', 1 UNION ALL
    SELECT '2026-09-16', '2026-09-30', 2 UNION ALL
    SELECT '2026-10-01', '2026-10-15', 1 UNION ALL
    SELECT '2026-10-16', '2026-10-31', 2 UNION ALL
    SELECT '2026-11-01', '2026-11-15', 1 UNION ALL
    SELECT '2026-11-16', '2026-11-30', 2 UNION ALL
    SELECT '2026-12-01', '2026-12-15', 1 UNION ALL
    SELECT '2026-12-16', '2026-12-31', 2
) AS d
LEFT JOIN forthnight_period_mst f
       ON f.start_date = d.start_date AND f.end_date = d.end_date
WHERE f.forthnight_id IS NULL;

-- existing rows (Aug 2026) carried type_of_period = 1 for both halves
UPDATE forthnight_period_mst
   SET type_of_period = IF(DAY(start_date) = 1, 1, 2)
 WHERE YEAR(start_date) = 2026;
