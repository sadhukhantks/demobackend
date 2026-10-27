-- Migration: Selection Trolley Weight — per-trolley weighing log after manual jute
--            selection, plus the 'Selection' stage tag in machine_type_mst (D1).
--            Per ../vowerp3ui/docs/claude/modules/jute-production/
--            selection-trolley-weight-spec.md §2 and decisions D1–D7.
-- Date: 2026-09-22
-- Applies to: tenant database dev3 (QA). Promote to other tenants later
--             (migrate-tenant-tables skill). Run with the run-migration skill (pymysql).
--
-- Context: raw jute issued to production (jute_issue) goes through a manual selection
-- step; the sorted jute is loaded on a trolley and the loaded trolley is weighed. The
-- operator picks the jute quality (the raw-jute item_mst row — the SAME identity the
-- issue side uses, D3), the trolley (tagged to the 'Selection' stage in Trolly Master,
-- D1), optionally a spell (D5) and a gate entry (D7 — gate entries live on jute_mr),
-- and types the gross reading. The server snapshots the trolley tare and stores
-- net_wt = gross_wt − trolly_wt (D2; basket weight is NOT deducted).
--   1. jute_prod_selection_trolly_wt — one row per weighed trolley, tare snapshot     (§2)
--   2. machine_type_mst 'Selection'   — idempotent stage-tag seed for Trolly Master  (D1)
--
-- Conventions match create_jute_prod_bale_tables.sql: co_id + branch_id scoping, soft
-- delete (active TINYINT DEFAULT 1), audit cols updated_by + updated_date_time only
-- (triggers handle the rest — NO created_*), idx_/fk_ naming. Rollback block at the end.
-- Spelling follows the codebase: `trolly` (table trolly_mst).
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with '--',
-- so this header block is terminated by a lone ';'.
;

-- 1. Selection trolley weight log (§2). No approval workflow (D6); soft delete only.
--    trolly_wt is a SNAPSHOT of trolly_mst.trolly_weight taken at save (kept on edit
--    while the trolley is unchanged) so history stays reproducible after later master
--    edits. weigh_time is the business timestamp that orders the day grid, NOT audit.
;
CREATE TABLE jute_prod_selection_trolly_wt (
    selection_wt_id    INT           NOT NULL AUTO_INCREMENT,
    co_id              INT           NOT NULL,
    branch_id          INT           NOT NULL,
    tran_date          DATE          NOT NULL,                 -- selection date (never in the future)
    spell_id           INT           NULL,                     -- D5, optional
    item_id            INT           NOT NULL,                 -- jute quality = raw-jute item (item_mst)
    trolly_id          INT           NOT NULL,
    jute_mr_id         BIGINT        NULL,                     -- D7, optional gate entry (jute_mr.jute_gate_entry_no) -- BIGINT to match jute_mr.jute_mr_id
    trolly_wt          DECIMAL(10,3) NOT NULL DEFAULT 0,       -- snapshot of trolly_mst.trolly_weight (kg) at save
    gross_wt           DECIMAL(10,3) NOT NULL,                 -- scale reading (kg), > 0
    net_wt             DECIMAL(10,3) NOT NULL,                 -- gross_wt − trolly_wt (server-computed, D2), > 0
    weigh_time         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- business timestamp, orders the day grid
    remarks            VARCHAR(255)  NULL,
    active             TINYINT       NOT NULL DEFAULT 1,
    updated_by         INT           NULL,
    updated_date_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (selection_wt_id),
    KEY idx_stw_co_branch_date (co_id, branch_id, tran_date),
    KEY idx_stw_item_date      (item_id, tran_date),
    KEY idx_stw_trolly         (trolly_id),
    KEY idx_stw_spell          (spell_id),
    KEY idx_stw_mr             (jute_mr_id),
    CONSTRAINT fk_stw_mr     FOREIGN KEY (jute_mr_id) REFERENCES jute_mr    (jute_mr_id),
    CONSTRAINT fk_stw_item   FOREIGN KEY (item_id)   REFERENCES item_mst   (item_id),
    CONSTRAINT fk_stw_trolly FOREIGN KEY (trolly_id) REFERENCES trolly_mst (trolly_id),
    CONSTRAINT fk_stw_spell  FOREIGN KEY (spell_id)  REFERENCES spell_mst  (spell_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Stage tag for the trolley picker (D1) — Trolly Master lists it through
--    /api/spinningMasters/trolly_machine_types (SELECTION_MACHINE_TYPE_NAME). Idempotent:
--    a tenant that already carries a 'Selection' type keeps its row.
;
INSERT INTO machine_type_mst (machine_type_name, updated_by, active)
SELECT 'Selection', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM machine_type_mst WHERE machine_type_name = 'Selection');

-- VERIFICATION
;
SELECT machine_type_id, machine_type_name, active
FROM machine_type_mst
WHERE machine_type_name = 'Selection';

-- =============================================================================
-- ROLLBACK (the stage tag is removed only if no trolley was tagged with it):
-- DROP TABLE IF EXISTS jute_prod_selection_trolly_wt;
-- DELETE FROM machine_type_mst
--  WHERE machine_type_name = 'Selection'
--    AND NOT EXISTS (SELECT 1 FROM trolly_mst t
--                     WHERE t.machine_type_id = machine_type_mst.machine_type_id);
-- =============================================================================
;
