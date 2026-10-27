-- =============================================================================
-- TKT-000183 — Wastage Report: "Total wastage % is left blank" (sls, co 2, FACTORY 29)
-- =============================================================================
-- Date: 2026-09-23   TARGET: sls (production — run only with the owner's approval)
--
-- DIAGNOSIS (SELECT-verified 2026-09-23, September 2026, branch 29):
--   The blank % is the last band of the printed sheet, "Other / Unmapped",
--   which by design has no denominator. It held 41,566 kg of the month's
--   50,333 kg of waste because four waste items were created AFTER the
--   2026-08-25 seed ran and have no row in jute_waste_report_map:
--     335884 Dust          BATCHING(EJM) 15          19,759 kg
--     335892 Sliver Waste  SPINNING(EJM) 17 / BATCHING 15   10,328 kg
--     335638 LINE WASTE    BATCHING(EJM) 15           9,923 kg
--     335891 Sweep         WINDING(EJM) 157 / SPINNING 17    1,556 kg
--   The mapped groups (Ropes & Habijabi, Thread, Gunny, Caddies) carried only
--   8,767 kg. Mapping the four items moves them into groups that DO have a
--   denominator, and the OTHER band disappears from the sheet.
--
-- VERIFY (before): what the report's OTHER band is made of this month
--   SELECT w.item_id, im.item_name, w.dept_id, d.dept_desc, ROUND(SUM(w.waste_qty),1) AS kg
--     FROM jute_prod_waste_entry w
--     JOIN dept_mst d  ON d.dept_id = w.dept_id
--     JOIN item_mst im ON im.item_id = w.item_id
--    WHERE w.co_id = 2 AND w.active = 1
--      AND w.tran_date BETWEEN '2026-09-01' AND LAST_DAY('2026-09-01')
--      AND COALESCE(w.branch_id, d.branch_id) = 29
--      AND NOT EXISTS (SELECT 1 FROM jute_waste_report_map m
--                       WHERE m.co_id = 2 AND m.active = 1 AND m.item_id = w.item_id
--                         AND (m.dept_id IS NULL OR m.dept_id = w.dept_id))
--    GROUP BY w.item_id, im.item_name, w.dept_id, d.dept_desc ORDER BY kg DESC;
--
-- REPAIR 1 — Seed 1 of 20260825_jute_waste_report_map.sql re-run for co 2
-- (idempotent, gap-filling). On sls today this adds exactly 3 rows:
--   Sliver Waste -> SLIVER (SPINNING / SPINNING), Dust -> CADDIES_MILL
--   (SPINNING / SPINNING), Sweep -> SWEEP (SPINNING / SPINNING).
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
  AND ig.co_id = 2
  AND NOT EXISTS (
    SELECT 1 FROM jute_waste_report_map x
    WHERE x.co_id = ig.co_id AND x.item_id = im.item_id AND x.dept_id IS NULL
  );

-- REPAIR 2 — CUTTING (141, batching, 764 kg in Sept) matches no seed pattern.
-- Owner decision 2026-10-05: its own band, % of jute issue (SELECTION) for
-- both the day rows and the monthly total. Safe with the code already in
-- production (SELECTION is a known denominator).
;
INSERT INTO jute_waste_report_map
  (co_id, group_code, group_name, day_denom, total_denom, item_id, dept_id, display_order, updated_by)
SELECT 2, 'CUTTING', 'Cutting', 'SELECTION', 'SELECTION', 141, NULL, 15, 1 FROM DUAL
WHERE NOT EXISTS (
  SELECT 1 FROM jute_waste_report_map x
  WHERE x.co_id = 2 AND x.item_id = 141 AND x.dept_id IS NULL
);

-- REPAIR 3 — LINE WASTE (335638, batching) matches no seed pattern.
-- Owner decision 2026-10-05: its own band, % of WINDING production.
-- !! DO NOT RUN until the WINDING-aware Wastage Report code (TKT-000183,
-- WASTE_REPORT_DENOM_WINDING) is DEPLOYED TO PRODUCTION. The production
-- report code before that change indexes denominators without a fallback,
-- so a 'WINDING' row makes the whole sls Wastage Report fail with a 500.
-- (Once deployed, the mill can also add this row themselves on the
-- Wastage Report Mapping master.)
-- INSERT INTO jute_waste_report_map
--   (co_id, group_code, group_name, day_denom, total_denom, item_id, dept_id, display_order, updated_by)
-- SELECT 2, 'LINE_WASTE', 'Line Waste', 'WINDING', 'WINDING', 335638, NULL, 25, 1 FROM DUAL
-- WHERE NOT EXISTS (
--   SELECT 1 FROM jute_waste_report_map x
--   WHERE x.co_id = 2 AND x.item_id = 335638 AND x.dept_id IS NULL
-- );
--
-- VERIFY (after): REPAIR 1 + 2 inserted 3 + 1 rows; the VERIFY query above
-- returns only LINE WASTE until REPAIR 3 runs, then nothing; the September
-- sheet prints a % in every band.
--
-- ROLLBACK
--   DELETE FROM jute_waste_report_map
--    WHERE co_id = 2 AND dept_id IS NULL AND item_id IN (335884, 335891, 335892, 141, 335638);
-- =============================================================================
