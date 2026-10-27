-- Seed: Bale series counters for dev3 from the mill sheet's "Last Bale No" tab
--       (docs/bale-production-spec-sheet.md §4.1, values as of Feb-2026).
-- Date: 2026-09-16
-- Applies to: tenant database dev3 (QA). OPTIONAL — confirm the counter values with the
--             owner before applying (§4.4 item 3). Run with the run-migration skill.
-- Requires: create_jute_prod_bale_tables.sql applied first.
--
-- Idempotent: every INSERT is guarded by NOT EXISTS on (co_id, branch_id, series_name,
-- active = 1), so re-running the script is a no-op. The hessian series are created
-- UNBOUND (item_id NULL) because the sheet's short names ('40"-6.50', '39"- 5.85',
-- '38" - 7.14') are hand-typed aliases; bind each to its item_mst row afterwards in the
-- Bale Series Master page (or via UPDATE ... SET item_id = ...). Spellings are the
-- sheet's (CHHATISGARH, ANDHRAPARDESH, TELENGANA) — link each to the properly spelled
-- state_mst row through state_id in the master page rather than renaming here.
--
-- ============================== OPERATOR FILLS IN ==================================
-- Resolve the target company / branch (the mill whose counters these are) and set the
-- two session variables below. Do NOT hardcode ids in this file. Example lookups:
--   SELECT co_id, co_name FROM co_mst;
--   SELECT branch_id, branch_name FROM branch_mst WHERE co_id = <co_id>;
SET @co_id     := NULL;   -- <-- e.g. SET @co_id := 1;
SET @branch_id := NULL;   -- <-- e.g. SET @branch_id := 4;
-- ====================================================================================

-- Safety: every INSERT below also requires both variables to be set, so a script run
-- with the header left NULL inserts NOTHING (instead of tripping the NOT NULL columns).

-- ---------------------------------------------------------------------------------
-- Sacking series (quality_type = 2) — one counter per consignee / order series,
-- shared across every sacking bag quality (item_id NULL).
-- ---------------------------------------------------------------------------------
INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'CHHATISGARH', 2, NULL, 21600, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'CHHATISGARH' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'ANDHRAPARDESH', 2, NULL, 24241, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'ANDHRAPARDESH' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'PUNJAB', 2, NULL, 24746, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'PUNJAB' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'BIHAR', 2, NULL, 22277, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'BIHAR' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'TELENGANA', 2, NULL, 21359, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'TELENGANA' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'FCI', 2, NULL, 23003, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'FCI' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, 'JHARKHAND', 2, NULL, 2009, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = 'JHARKHAND' AND active = 1);

-- ---------------------------------------------------------------------------------
-- Hessian series (quality_type = 1) — one counter per quality. Created unbound; bind
-- item_id to the matching 'Jute Cloth' item (item_grp_mst.item_type_id = 5) afterwards.
-- ---------------------------------------------------------------------------------
INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, '40"-6.50', 1, NULL, 205, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = '40"-6.50' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, '39"- 5.85', 1, NULL, 0, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = '39"- 5.85' AND active = 1);

INSERT INTO jute_prod_bale_series (co_id, branch_id, series_name, quality_type, item_id, last_bale_no, active)
SELECT @co_id, @branch_id, '38" - 7.14', 1, NULL, 0, 1
WHERE @co_id IS NOT NULL AND @branch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jute_prod_bale_series
                   WHERE co_id = @co_id AND branch_id = @branch_id AND series_name = '38" - 7.14' AND active = 1);

-- =============================================================================
-- ROLLBACK (only the rows this seed created, for the same co/branch):
-- DELETE FROM jute_prod_bale_series
--  WHERE co_id = @co_id AND branch_id = @branch_id
--    AND series_name IN ('CHHATISGARH','ANDHRAPARDESH','PUNJAB','BIHAR','TELENGANA','FCI',
--                        'JHARKHAND','40"-6.50','39"- 5.85','38" - 7.14');
-- =============================================================================
