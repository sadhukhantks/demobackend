-- =============================================================================
-- Migration: Winding Standards / Targets Map (jute_prod_winding_target_map)
-- =============================================================================
-- Date: 2026-08-19
-- TARGET: tenant DB only (dev3 first, then sls / other tenants BEFORE the code
-- deploys -- /api/windingTargetMap 500s on a tenant that lacks the table, and the
-- Winding Quality Wise report's Target columns stay blank).
--
-- Structural clone of jute_prod_weaving_target_map (create_weaving_tables.sql
-- step 2) with the PK renamed. Same conventions as that table: TINYINT active,
-- TIMESTAMP ... ON UPDATE CURRENT_TIMESTAMP, no ENGINE / CHARSET clause (server
-- defaults, as every other jute_prod_* table in this repo), no FKs.
--
-- QUALITY-ONLY, unlike weaving's two dimensions: id_type is always 'qid' and
-- ref_id is the YARN item (item_mst.item_id) -- winding has no machine standard
-- (entry is person-keyed, see docs/winding-person-keyed-entry-spec.md). One param
-- only: 'prod' = KG per winder per 8-hour shift. value_role is 'standard' or
-- 'target'. Resolution is branch-agnostic LAST-DATE: the active row with
-- MAX(effective_date) <= on_date, weaving's exact rule.
--
-- Runner note: the pymysql migration runner splits on ';' and skips chunks that
-- start with '--', so each comment block is terminated by a lone ';' below and no
-- ';' appears inside any string.
;

CREATE TABLE IF NOT EXISTS jute_prod_winding_target_map (
    winding_target_map_id INT           NOT NULL AUTO_INCREMENT,
    co_id                 INT           NOT NULL,
    branch_id             INT           NULL,                    -- stored, NOT used in resolution
    effective_date        DATE          NOT NULL,
    ref_id                INT           NOT NULL,                -- yarn item_mst.item_id (qid)
    id_type               VARCHAR(8)    NOT NULL,                -- 'qid' only (quality-only)
    value_role            VARCHAR(10)   NOT NULL,                -- 'standard' | 'target'
    param                 VARCHAR(20)   NOT NULL,                -- 'prod' (KG per winder per 8h shift)
    value                 DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
    active                TINYINT       NOT NULL DEFAULT 1,
    updated_by            INT           NULL,
    updated_date_time     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (winding_target_map_id),
    KEY idx_wintm_lookup (co_id, ref_id, id_type, value_role, param, effective_date),
    KEY idx_wintm_co (co_id)
);

-- =============================================================================
-- VERIFICATION -- run manually after applying
-- =============================================================================
--   SHOW CREATE TABLE jute_prod_winding_target_map
--   SELECT COUNT(*) FROM jute_prod_winding_target_map
--
-- Then open Jute Production > Winding Target Map, pick an effective date, type a
-- prod value against a yarn and save -- one row per (yarn, role) should appear.
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DROP TABLE IF EXISTS jute_prod_winding_target_map;
-- =============================================================================
