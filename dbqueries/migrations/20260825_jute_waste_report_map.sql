-- =============================================================================
-- Migration: Wastage Report mapping master (+ best-effort seeds)
-- =============================================================================
-- Date: 2026-08-25
-- TARGET: tenant DB (dev3 first).
--
-- The daily Wastage Report derives every waste column from jute_prod_waste_entry;
-- this table says which (waste item, department) lands in which report group and
-- which production figure its percentage divides by. It is DATA on purpose:
-- waste item names are tenant-specific (no seeds exist in code) and mills
-- re-shape the report by editing rows here, not by a release.
--
-- dept_id NULL = the item maps to this group from ANY department (the report
-- still shows one component column per department that actually has entries).
-- A (item, dept) row beats the item's NULL-dept row for that department.
--
-- Denominator codes (day_denom / total_denom):
--   WEAVING      -- weaving production kg of the day / month
--   SPINNING     -- spinning production kg
--   BREAKER_FEED -- spreader rolls issued to breaker, kg (breaker production
--                   itself is not tracked; user decision 2026-08-25)
--   SELECTION    -- raw jute issued (jute_issue weight, MR-sourced legs)
--   NONE         -- no percentage
-- day_denom and total_denom are separate because the mill's format divides
-- daily rows and the monthly total row by DIFFERENT bases for some groups
-- (kept as-is by user decision 2026-08-25).
--
-- Seeds below match live item/dept names with LIKE patterns and are
-- idempotent + gap-filling; re-run after adding new waste items. Waste
-- entries with no mapping row appear in the report's OTHER bucket — nothing
-- silently disappears.
;
CREATE TABLE IF NOT EXISTS jute_waste_report_map (
  waste_report_map_id INT PRIMARY KEY AUTO_INCREMENT,
  co_id               INT NOT NULL,
  group_code          VARCHAR(40) NOT NULL,
  group_name          VARCHAR(100) NOT NULL,
  day_denom           VARCHAR(20) NOT NULL DEFAULT 'NONE',
  total_denom         VARCHAR(20) NOT NULL DEFAULT 'NONE',
  item_id             INT NOT NULL,
  dept_id             INT NULL,
  col_label           VARCHAR(100) NULL,
  display_order       INT NOT NULL DEFAULT 0,
  active              INT NOT NULL DEFAULT 1,
  updated_by          INT NULL,
  updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_waste_rpt_map_co (co_id)
);

-- Seed 1: item-name-based groups (any department). One row per matching
-- jute-wastage item (item_type_id = 3) per company.
;
INSERT INTO jute_waste_report_map
  (co_id, group_code, group_name, day_denom, total_denom, item_id, dept_id, display_order, updated_by)
SELECT ig.co_id, g.group_code, g.group_name, g.day_denom, g.total_denom, im.item_id, NULL, g.display_order, 1
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id AND ig.item_type_id = 3
JOIN (
  SELECT 'ROPES_HABIJABI' AS group_code, 'Ropes & Habijabi' AS group_name, 'SELECTION' AS day_denom, 'SELECTION' AS total_denom, '%HABIJABI%' AS pattern, 10 AS display_order
  UNION ALL SELECT 'ROPES_HABIJABI', 'Ropes & Habijabi', 'SELECTION', 'SELECTION', '%ROPE%', 10
  UNION ALL SELECT 'CONDUCTOR', 'Conductor Waste', 'BREAKER_FEED', 'BREAKER_FEED', '%CONDUCTOR%', 20
  UNION ALL SELECT 'SLIVER', 'Sliver Waste', 'SPINNING', 'SPINNING', '%SLIVER%', 30
  UNION ALL SELECT 'THREAD', 'Thread Waste', 'WEAVING', 'SPINNING', '%THREAD%', 40
  UNION ALL SELECT 'GUNNY', 'Gunny Cutting', 'WEAVING', 'SPINNING', '%GUNNY%', 50
  UNION ALL SELECT 'CADDIES_MILL', 'Mill Side Caddies & Dust', 'SPINNING', 'SPINNING', '%DUST%', 60
  UNION ALL SELECT 'SWEEP', 'Spinning Sweep', 'SPINNING', 'SPINNING', '%SWEEP%', 80
) g ON UPPER(im.item_name) LIKE g.pattern
WHERE im.active = 1
  AND NOT EXISTS (
    SELECT 1 FROM jute_waste_report_map x
    WHERE x.co_id = ig.co_id AND x.item_id = im.item_id AND x.dept_id IS NULL
  );

-- Seed 2: caddies split by department — mill side (batching/spinning/winding,
-- % of spinning production) vs factory side (beaming/weaving looms, daily % of
-- weaving production, monthly % of spinning production — mirrors the format).
;
INSERT INTO jute_waste_report_map
  (co_id, group_code, group_name, day_denom, total_denom, item_id, dept_id, display_order, updated_by)
SELECT ig.co_id, g.group_code, g.group_name, g.day_denom, g.total_denom, im.item_id, d.dept_id, g.display_order, 1
FROM item_mst im
JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id AND ig.item_type_id = 3
JOIN branch_mst b ON b.co_id = ig.co_id AND b.active = 1
JOIN dept_mst d ON d.branch_id = b.branch_id
JOIN (
  SELECT 'CADDIES_MILL' AS group_code, 'Mill Side Caddies & Dust' AS group_name, 'SPINNING' AS day_denom, 'SPINNING' AS total_denom, '%BATCH%' AS dept_pattern, 60 AS display_order
  UNION ALL SELECT 'CADDIES_MILL', 'Mill Side Caddies & Dust', 'SPINNING', 'SPINNING', '%SPIN%', 60
  UNION ALL SELECT 'CADDIES_MILL', 'Mill Side Caddies & Dust', 'SPINNING', 'SPINNING', '%WIND%', 60
  UNION ALL SELECT 'CADDIES_FACTORY', 'Factory Side Caddies', 'WEAVING', 'SPINNING', '%BEAM%', 70
  UNION ALL SELECT 'CADDIES_FACTORY', 'Factory Side Caddies', 'WEAVING', 'SPINNING', '%WEAV%', 70
) g ON UPPER(d.dept_desc) LIKE g.dept_pattern
WHERE im.active = 1
  AND UPPER(im.item_name) LIKE '%CADDI%'
  AND NOT EXISTS (
    SELECT 1 FROM jute_waste_report_map x
    WHERE x.co_id = ig.co_id AND x.item_id = im.item_id AND x.dept_id = d.dept_id
  );

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT m.group_code, m.day_denom, m.total_denom, im.item_name, d.dept_desc
--     FROM jute_waste_report_map m
--     JOIN item_mst im ON im.item_id = m.item_id
--     LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
--    WHERE m.active = 1 ORDER BY m.co_id, m.display_order, im.item_name;
-- If a waste item shows in the report's OTHER bucket, it has no row here —
-- add one (or re-run this script after fixing the item name).
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DROP TABLE jute_waste_report_map;
-- =============================================================================
