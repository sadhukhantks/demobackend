-- =============================================================================
-- Jute SQC Standard Moisture Master (jute_sqc_std_mr_mst)
--
-- Replaces the hardcoded std MR% constants in the four yarn-group-keyed SQC
-- pages with a per-(company, yarn item group, process) master.
--
--   process token   SQC page (FE)                 was hardcoded
--   -------------   ---------------------------   -------------
--   BREAKER         juteSQC/breakerCard           16.0
--   CARDING         juteSQC/interCard             20.0
--   DRAWHEAD        juteSQC/drawhead              16.0
--   FIN_DRAW        juteSQC/finDraw               16.0
--
-- Co-scoped only (item_grp_mst carries co_id, not branch_id). Yarn groups are
-- item_grp_mst.item_type_id = 4; item_grp_mst.active is a messy varchar
-- ('1' / 'Y' / NULL all mean live).
--
-- Save on the four SQC pages now BLOCKS (400) when the (co_id, item_grp_id,
-- process) row is missing, so section 2 seeds every currently-active yarn group
-- at the value the code used to hardcode. Re-runnable: the seed is guarded by
-- the unique key via INSERT ... ON DUPLICATE KEY UPDATE (no-op on rerun).
--
-- Target: tenant DBs (dev3, sls, and every other tenant BEFORE the code deploys)
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS jute_sqc_std_mr_mst;
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jute_sqc_std_mr_mst (
    std_mr_id           INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    item_grp_id         INT           NOT NULL,
    process             VARCHAR(30)   NOT NULL,
    std_mr_pct          DECIMAL(5, 2) NOT NULL,
    active              INT           NOT NULL DEFAULT 1,
    updated_by          INT               NULL,
    updated_date_time   DATETIME          NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (std_mr_id),
    UNIQUE KEY uk_jute_sqc_std_mr (co_id, item_grp_id, process),
    KEY idx_jute_sqc_std_mr_lookup (co_id, item_grp_id, process, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. Seed — every active jute-yarn item group x the 4 processes, at the value
--    the backend used to hardcode. Idempotent.
-- ---------------------------------------------------------------------------
INSERT INTO jute_sqc_std_mr_mst (co_id, item_grp_id, process, std_mr_pct, active)
SELECT igm.co_id, igm.item_grp_id, p.process, p.std_mr_pct, 1
FROM item_grp_mst igm
CROSS JOIN (
    SELECT 'BREAKER'  AS process, 16.00 AS std_mr_pct
    UNION ALL SELECT 'CARDING',  20.00
    UNION ALL SELECT 'DRAWHEAD', 16.00
    UNION ALL SELECT 'FIN_DRAW', 16.00
) p
WHERE igm.item_type_id = 4
  AND (igm.active IN ('1', 'Y') OR igm.active IS NULL)
  AND igm.co_id IS NOT NULL
ON DUPLICATE KEY UPDATE std_mr_id = jute_sqc_std_mr_mst.std_mr_id;
