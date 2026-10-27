-- jute_mr_li.mill_moisture: moisture % recorded by factory QC at material inspection.
-- Written only by material inspection saves; MR edit (supplier settlement) changes
-- actual_moisture and never touches this, so the original mill figure survives.
-- Backfill: active lines with QC readings get AVG(jute_moisture_rdg) (inactive lines skipped). Lines without readings
-- stay NULL - their actual_moisture may already be a settled value, so it is not trusted.
--
-- Rollback:
--   ALTER TABLE jute_mr_li DROP COLUMN mill_moisture;

ALTER TABLE jute_mr_li ADD COLUMN mill_moisture DECIMAL(6,2) NULL AFTER actual_moisture;

UPDATE jute_mr_li li
JOIN (
    SELECT jute_mr_li_id, ROUND(AVG(moisture_percentage), 2) AS avg_m
    FROM jute_moisture_rdg
    WHERE moisture_percentage IS NOT NULL
    GROUP BY jute_mr_li_id
) r ON r.jute_mr_li_id = li.jute_mr_li_id
SET li.mill_moisture = r.avg_m
WHERE li.mill_moisture IS NULL
  AND li.active = 1;
