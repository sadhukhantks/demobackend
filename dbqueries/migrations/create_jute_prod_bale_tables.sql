-- Migration: Bale Production (Bale Press) — bale-series counter master + per-bale entry
--            log, plus two nullable standards columns on the Finishing Quality Master.
--            Per docs/bale-production-spec-sheet.md §4.1, §4.2, §4.3 and decisions D1–D4.
-- Date: 2026-09-16
-- Applies to: tenant database dev3 (QA). Promote to other tenants later
--             (migrate-tenant-tables skill). Run with the run-migration skill (pymysql).
--
-- Context: today every bale leaving the bale press is weighed and typed into a Google
-- Form (date, spell, quality, machine, consignee/"vendor" series, bale no, gross, trolley).
-- This script creates the two tables that replace the BALE/REPACK tabs and the
-- "Last Bale No" tab, and extends jute_prod_finishing_quality with the STD tab's
-- std MR % and weft yarn link:
--   1. jute_prod_bale_series    — one running bale-number counter per sheet "vendor"
--                                 (sacking: state / FCI; hessian: per quality)        (§4.1)
--   2. jute_prod_bale_entry     — one row per bale, PROD or REPACK, with the tare
--                                 snapshots that make net_wt reproducible (F0)          (§4.2)
--   3. ALTER jute_prod_finishing_quality ADD std_mr_pct, weft_yarn_item_id             (§4.3)
--   4. Seed std_mr_pct (16 hessian / 20 sacking) and packsheet_wt (5 kg) where NULL    (D1)
--
-- Conventions match create_finishing_tables.sql / create_jute_prod_waste_entry.sql:
-- co_id + branch_id scoping, soft delete (active TINYINT DEFAULT 1), audit cols
-- updated_by + updated_date_time only (triggers handle the rest — NO created_*),
-- idx_/uk_/fk_ naming. Rollback block at the end.

-- 1. Bale series master (§4.1) — the "Last Bale No" tab as data. One row = one running
--    counter. item_id set -> the series is bound to ONE quality (hessian pattern);
--    item_id NULL -> any quality of that quality_type (sacking / state pattern).
--    state_id / party_id are optional matches to existing masters (a state agency or
--    FCI), never forced. last_bale_no is bumped by every save whose bale_no is higher
--    (F9) and is editable in the master = a manual reset (D3).
--    Duplicate guard on (co_id, branch_id, series_name) among active = 1 is APP-level
--    (soft-delete pattern — no DB unique key).
CREATE TABLE jute_prod_bale_series (
    bale_series_id      INT           NOT NULL AUTO_INCREMENT,
    co_id               INT           NOT NULL,
    branch_id           INT           NOT NULL,                 -- counters are per mill
    series_name         VARCHAR(60)   NOT NULL,                 -- PUNJAB | FCI | 39"- 5.85 ...
    quality_type        TINYINT       NOT NULL,                 -- 1=Hessian, 2=Sacking (= jute_prod_finishing_quality.quality_type)
    item_id             INT           NULL,                     -- bound quality item (hessian); NULL = any quality of the type
    state_id            INT           NULL,                     -- optional match to state_mst
    party_id            INT           NULL,                     -- optional match to party_mst (e.g. FCI)
    last_bale_no        INT           NOT NULL DEFAULT 0,       -- the running counter (F9); edit = reset
    active              TINYINT       NOT NULL DEFAULT 1,
    updated_by          INT           NULL,
    updated_date_time   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bale_series_id),
    KEY idx_bs_co_branch (co_id, branch_id),
    KEY idx_bs_item      (item_id),
    CONSTRAINT fk_bs_item  FOREIGN KEY (item_id)  REFERENCES item_mst  (item_id),
    CONSTRAINT fk_bs_state FOREIGN KEY (state_id) REFERENCES state_mst (state_id),
    CONSTRAINT fk_bs_party FOREIGN KEY (party_id) REFERENCES party_mst (party_id)
);

-- 2. Bale entry (§4.2) — one row per bale weighed at the bale press. entry_type PROD
--    (BALE tab) or REPACK (REPACK tab) in the SAME table, flagged (D2): a REPACK row
--    takes the next series number as its own bale_no and stores the number of the bale
--    that was opened in original_bale_no (NULL on PROD rows).
--    trolly_wt / packsheet_wt / std_bale_wt are SNAPSHOTS taken at save so
--    net_wt = gross_wt − trolly_wt − packsheet_wt (F0) and the HY/LT history survive
--    later master edits. weigh_time is the form's Timestamp — a business field that
--    orders the Daily Bale Sheet within a spell, NOT an audit column.
--    idx_be_series_no is deliberately NON-unique: REPACK re-uses numbers and a series
--    reset legitimately re-uses old numbers; the (series, bale_no) clash is an app-level
--    409 the operator can override with allow_duplicate (Q5).
CREATE TABLE jute_prod_bale_entry (
    bale_entry_id         INT           NOT NULL AUTO_INCREMENT,
    co_id                 INT           NOT NULL,
    branch_id             INT           NOT NULL,
    tran_date             DATE          NOT NULL,               -- production date (never blank)
    spell_id              INT           NOT NULL,
    machine_id            INT           NULL,                   -- Bale Press; NULL for branches without a machine row
    entry_type            VARCHAR(8)    NOT NULL DEFAULT 'PROD', -- 'PROD' | 'REPACK' (D2)
    finishing_quality_id  INT           NOT NULL,               -- item + type + standards
    bale_series_id        INT           NOT NULL,
    bale_no               INT           NOT NULL,               -- this row's own number
    original_bale_no      INT           NULL,                   -- REPACK only: the re-pressed bale's number
    trolly_id             INT           NULL,
    trolly_wt             DECIMAL(10,3) NOT NULL DEFAULT 0,     -- snapshot of trolly_mst.trolly_weight (kg)
    packsheet_wt          DECIMAL(10,3) NOT NULL DEFAULT 0,     -- snapshot of jute_prod_finishing_quality.packsheet_wt (kg)
    gross_wt              DECIMAL(10,3) NOT NULL,               -- scale reading (kg), > 0
    net_wt                DECIMAL(10,3) NOT NULL,               -- gross_wt − trolly_wt − packsheet_wt (server-computed, F0)
    std_bale_wt           DECIMAL(10,3) NULL,                   -- snapshot of the standard at save
    weigh_time            DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP, -- business timestamp (orders the day sheet)
    remarks               VARCHAR(255)  NULL,
    active                TINYINT       NOT NULL DEFAULT 1,
    updated_by            INT           NULL,
    updated_date_time     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bale_entry_id),
    KEY idx_be_co_branch_date (co_id, branch_id, tran_date),
    KEY idx_be_series_no      (bale_series_id, bale_no),       -- NON-unique by design (REPACK / series reset)
    KEY idx_be_quality_date   (finishing_quality_id, tran_date),
    KEY idx_be_spell          (spell_id),
    CONSTRAINT fk_be_spell   FOREIGN KEY (spell_id)             REFERENCES spell_mst                    (spell_id),
    CONSTRAINT fk_be_machine FOREIGN KEY (machine_id)           REFERENCES machine_mst                  (machine_id),
    CONSTRAINT fk_be_quality FOREIGN KEY (finishing_quality_id) REFERENCES jute_prod_finishing_quality  (finishing_quality_id),
    CONSTRAINT fk_be_series  FOREIGN KEY (bale_series_id)       REFERENCES jute_prod_bale_series        (bale_series_id),
    CONSTRAINT fk_be_trolly  FOREIGN KEY (trolly_id)            REFERENCES trolly_mst                   (trolly_id)
);

-- 3. Finishing Quality Master — two new NULLABLE standards columns (§4.3, STD tab).
--    std_mr_pct: standard moisture regain % (16 hessian / 20 sacking in the sheet).
--    weft_yarn_item_id: the weft yarn item (item_type_id = 4) whose
--    jute_yarn_mst.jute_yarn_count HY/LT shows as "WFT USED".
ALTER TABLE jute_prod_finishing_quality
    ADD COLUMN std_mr_pct        DECIMAL(5,2) NULL AFTER no_of_bags,
    ADD COLUMN weft_yarn_item_id INT          NULL AFTER std_mr_pct,
    ADD KEY idx_fq_weft_item (weft_yarn_item_id),
    ADD CONSTRAINT fk_fq_weft_item FOREIGN KEY (weft_yarn_item_id) REFERENCES item_mst (item_id);

-- 4. Seed the standards where blank (STD tab values; D1 for the packsheet).
--    Only NULLs are touched so a tenant that already maintains these keeps its values.
UPDATE jute_prod_finishing_quality
   SET std_mr_pct = 16.00
 WHERE std_mr_pct IS NULL AND quality_type = 1;            -- Hessian

UPDATE jute_prod_finishing_quality
   SET std_mr_pct = 20.00
 WHERE std_mr_pct IS NULL AND quality_type = 2;            -- Sacking

UPDATE jute_prod_finishing_quality
   SET packsheet_wt = 5.000
 WHERE packsheet_wt IS NULL;                                -- D1: the sheet's flat 5 kg deduction is the packsheet

-- =============================================================================
-- ROLLBACK (reverse order — drop the entry log first, then the series master it
-- references, then undo the quality-master extension; the seeded standards are
-- reverted to NULL only where this script set them):
-- UPDATE jute_prod_finishing_quality SET packsheet_wt = NULL WHERE packsheet_wt = 5.000;
-- UPDATE jute_prod_finishing_quality SET std_mr_pct = NULL WHERE std_mr_pct IN (16.00, 20.00);
-- ALTER TABLE jute_prod_finishing_quality
--     DROP FOREIGN KEY fk_fq_weft_item,
--     DROP KEY idx_fq_weft_item,
--     DROP COLUMN weft_yarn_item_id,
--     DROP COLUMN std_mr_pct;
-- DROP TABLE IF EXISTS jute_prod_bale_entry;
-- DROP TABLE IF EXISTS jute_prod_bale_series;
-- =============================================================================

-- APPLIED: dev3 on 2026-09-20 (2 tables, ALTER on 12 fq rows, seeds 5/7/7).
-- APPLIED: sls  on 2026-09-20 (2 tables, ALTER on 22 fq rows, seeds 6/16/16).
