-- =============================================================================
-- Migration: Low Performer Follow-up tables (Jute Production)
-- =============================================================================
-- Date: 2026-10-07 (owner request 2026-10-07)
-- TARGET: tenant DB only -- dev3 first, then sls (owner approved both).
--
-- The mill marks a few workers (by EB no) of Weaving / Spinning / Winding whose
-- production must be followed up, and keeps a dated record of remarks, warnings /
-- notices and hour deductions entered by the department. Production / efficiency
-- itself is NOT stored here: it is read live from the Low Producer computations.
--
-- Two NEW tables:
--   jute_prod_followup_person  one follow-up of one person in one department
--   jute_prod_followup_event   dated timeline rows of a follow-up
--                              (REMARK | NOTICE | HOURS_CUT)
--
-- House DDL style (20260928_loom_modification_tables.sql): co_id NOT NULL, active
-- TINYINT DEFAULT 1 (soft delete), updated_by / updated_date_time ON UPDATE, no
-- created_* columns (DB triggers). Portal persona, no approval workflow.
--
-- Duplicate guard lives in code: at most one OPEN (status = 1, active = 1) follow-up
-- per (co_id, branch_id, eb_id, dept_code). The DB backs it with the functional
-- unique key uq_jpfp_open added by 20261007b_jute_prod_followup_open_unique.sql.
-- Notice scans use the generic file_attachment table (module 'jute_prod_followup',
-- file_type 'notice', entity_id = followup_event_id) -- no column here.
-- Router: src/juteProduction/low_performer_followup.py (prefix /api/lowPerformerFollowup).
-- ORM:    src/juteProduction/low_performer_followup_models.py.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
;

-- 1. Follow-up header -- one person, one department
;
CREATE TABLE IF NOT EXISTS jute_prod_followup_person (
    followup_id       INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    branch_id         INT          NOT NULL,
    eb_id             INT          NOT NULL,                   -- hrms_ed_personal_details.eb_id
    dept_code         VARCHAR(20)  NOT NULL,                   -- WEAVING | SPINNING | WINDING
    reason            VARCHAR(500) NULL,
    start_date        DATE         NOT NULL,                   -- follow-up start (marked on)
    review_date       DATE         NULL,                       -- next review due
    status            TINYINT      NOT NULL DEFAULT 1,         -- 1 open / 0 closed
    closed_date       DATE         NULL,
    close_reason      VARCHAR(20)  NULL,                       -- IMPROVED | TRANSFERRED | LEFT | OTHER
    close_remarks     VARCHAR(500) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (followup_id),
    KEY idx_jpfp_scope (co_id, branch_id, dept_code, status, active),
    KEY idx_jpfp_eb    (eb_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Follow-up timeline -- remarks, warnings / notices, hour cuts
;
CREATE TABLE IF NOT EXISTS jute_prod_followup_event (
    followup_event_id INT           NOT NULL AUTO_INCREMENT,
    followup_id       INT           NOT NULL,
    event_type        VARCHAR(20)   NOT NULL,                  -- REMARK | NOTICE | HOURS_CUT
    event_date        DATE          NOT NULL,
    notice_type       VARCHAR(30)   NULL,                      -- NOTICE only: VERBAL_WARNING | WRITTEN_WARNING | SHOW_CAUSE | FINAL_WARNING | SUSPENSION | OTHER
    issued_by         VARCHAR(100)  NULL,                      -- NOTICE only
    spell_id          INT           NULL,                      -- HOURS_CUT only (spell_mst.spell_id)
    hours_cut         DECIMAL(5,2)  NULL,                      -- HOURS_CUT only, > 0 and <= the spell's hours
    remarks           VARCHAR(1000) NULL,                      -- REMARK text / notice remarks / hour-cut reason
    active            TINYINT       NOT NULL DEFAULT 1,
    updated_by        INT           NULL,
    updated_date_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (followup_event_id),
    KEY idx_jpfe_followup (followup_id, active, event_date),
    CONSTRAINT fk_jpfe_followup FOREIGN KEY (followup_id) REFERENCES jute_prod_followup_person (followup_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- VERIFICATION
--   SHOW TABLES LIKE 'jute_prod_followup%'
--   SHOW COLUMNS FROM jute_prod_followup_person
--   SHOW COLUMNS FROM jute_prod_followup_event
--
-- ROLLBACK (only while both tables are empty or the data may be discarded)
--   DROP TABLE IF EXISTS jute_prod_followup_event
--   DROP TABLE IF EXISTS jute_prod_followup_person
-- =============================================================================
;
