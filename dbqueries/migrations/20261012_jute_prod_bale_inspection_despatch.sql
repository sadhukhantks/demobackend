-- =============================================================================
-- Migration: Bale Stock & Inspection — per-bale inspection marking and
--            bale -> sales-invoice despatch link.
-- Date: 2026-10-12
-- Spec: docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md §2
-- Applies to: tenant DBs (dev3 first; sls after the owner's OK).
-- APPLIED: dev3 2026-10-12; sls 2026-10-12 (owner-requested).
--
-- Bale stock itself is DERIVED from jute_prod_bale_entry (one row = one bale,
-- decision D1) — nothing here stores a stock figure. These two tables record
-- the two events that change a bale's state after it is pressed:
--   1. jute_prod_bale_inspection — the bale was inspected (sacking / govt
--      supply only, D3). One ACTIVE row per bale (app-level rule, soft delete).
--   2. jute_prod_bale_despatch   — the bale left against a sales invoice (D4).
--      One ACTIVE row per bale (app-level rule, soft delete). A link to an
--      invoice later Rejected/Cancelled is void at read time, never deleted.
-- A REPACK needs no row: the REPACK entry's original_bale_no already says
-- which bale was opened (D2).
--
-- Conventions match create_jute_prod_bale_tables.sql: co_id + branch_id
-- scoping, soft delete (active TINYINT DEFAULT 1), audit cols updated_by +
-- updated_date_time only (triggers handle the rest — NO created_*), idx_/fk_
-- naming, FK to the bale row. No UNIQUE keys (soft-delete pattern).
--
-- Runner note: the pymysql runner splits on ';' and skips chunks starting with
-- '--', so each comment block is terminated by a lone ';'.
;

-- 1. Inspection marking (§2) — the inspected-goods register for sacking bales.
;
CREATE TABLE jute_prod_bale_inspection (
    bale_inspection_id    INT           NOT NULL AUTO_INCREMENT,
    co_id                 INT           NOT NULL,
    branch_id             INT           NOT NULL,
    bale_entry_id         INT           NOT NULL,                 -- jute_prod_bale_entry (sacking bale)
    inspection_date       DATE          NOT NULL,
    inspection_ref        VARCHAR(100)  NULL,                     -- certificate / inspection-note no
    inspector             VARCHAR(100)  NULL,                     -- agency / inspector name
    remarks               VARCHAR(255)  NULL,
    active                TINYINT       NOT NULL DEFAULT 1,
    updated_by            INT           NULL,
    updated_date_time     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bale_inspection_id),
    KEY idx_bi_bale           (bale_entry_id, active),
    KEY idx_bi_co_branch_date (co_id, branch_id, inspection_date),
    CONSTRAINT fk_bi_bale FOREIGN KEY (bale_entry_id) REFERENCES jute_prod_bale_entry (bale_entry_id)
);

-- 2. Despatch link (§2) — which invoice a bale left against.
;
CREATE TABLE jute_prod_bale_despatch (
    bale_despatch_id      INT           NOT NULL AUTO_INCREMENT,
    co_id                 INT           NOT NULL,
    branch_id             INT           NOT NULL,
    bale_entry_id         INT           NOT NULL,
    invoice_id            BIGINT        NOT NULL,                 -- sales_invoice.invoice_id
    invoice_line_item_id  BIGINT        NULL,                     -- sales_invoice_dtl, optional (future per-line pick)
    despatch_date         DATE          NOT NULL,                 -- defaults to the invoice date
    remarks               VARCHAR(255)  NULL,
    active                TINYINT       NOT NULL DEFAULT 1,
    updated_by            INT           NULL,
    updated_date_time     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bale_despatch_id),
    KEY idx_bd_bale           (bale_entry_id, active),
    KEY idx_bd_invoice        (invoice_id),
    KEY idx_bd_co_branch_date (co_id, branch_id, despatch_date),
    CONSTRAINT fk_bd_bale FOREIGN KEY (bale_entry_id) REFERENCES jute_prod_bale_entry (bale_entry_id)
);

-- =============================================================================
-- ROLLBACK (both tables are leaf tables — nothing references them):
--   DROP TABLE IF EXISTS jute_prod_bale_despatch;
--   DROP TABLE IF EXISTS jute_prod_bale_inspection;
-- =============================================================================
;
