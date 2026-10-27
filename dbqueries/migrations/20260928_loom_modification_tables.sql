-- =============================================================================
-- Migration: Loom Modification register tables (Jute Production, loom-only)
-- =============================================================================
-- Date: 2026-09-28   Phase 1 of
-- docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md (§4.1-§4.4)
-- Applies to: tenant database dev3 (QA) first via run-migration. sls ONLY when the
-- owner names the DB (sls holds production data).
--
-- Four NEW tables:
--   jute_prod_loom_group           the owner's "loom group" (current membership label)
--   jute_prod_loom_group_dtl       current members of a group (soft-delete + reinsert)
--   jute_prod_loom_modification    dated event register: one row per chain stage
--                                  (INITIAL / FOLLOWUP / RECTIFY / REVERSAL), linear
--                                  chain by parent_modification_id (self-FK, precedent
--                                  cost_element_mst.parent_element_id), whole chain
--                                  by root_modification_id (stamped at save)
--   jute_prod_loom_modification_mc membership FROZEN per event, with optional per-loom
--                                  done_date / done_to_date. The report reads only this.
--
-- House DDL style of create_weaving_tables.sql: parent co_id NOT NULL, branch_id NULL
-- (derived machine_mst -> dept_mst.branch_id, machine_mst has no branch), active
-- TINYINT DEFAULT 1, updated_by / updated_date_time ON UPDATE. Child tables: own PK +
-- parent FK + active only, no audit. No created_* columns (DB triggers). Event header
-- shape of jute_mech_overhaul (create_jute_mech_maintenance_tables.sql). total_cost is
-- DOUBLE (house rule for amounts). Portal persona, soft delete, no approval workflow.
--
-- Duplicate guards live in code (soft-delete pattern, no DB unique keys):
--   group   (co_id, group_code) among active = 1
--   snapshot one row per (modification_id, machine_id) among active = 1
-- No standard-speed columns: the register never writes jute_prod_weaving_target_map
-- (D6), the report resolves the standard in force when it runs.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
;

-- 1. Loom Group header (§4.1) — current membership label, STUDY or CONTROL
;
CREATE TABLE IF NOT EXISTS jute_prod_loom_group (
    loom_group_id     INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    branch_id         INT          NULL,                      -- derived from members' dept_mst.branch_id (all agree, branch of co_id)
    group_code        VARCHAR(30)  NOT NULL,                  -- dup guard in code on (co_id, group_code) among active = 1
    group_name        VARCHAR(100) NOT NULL,
    group_purpose     VARCHAR(10)  NOT NULL DEFAULT 'STUDY',  -- STUDY | CONTROL (LOOM_GROUP_PURPOSES)
    remarks           VARCHAR(500) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (loom_group_id),
    KEY idx_lg_co        (co_id, active),
    KEY idx_lg_co_branch (co_id, branch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Loom Group members (§4.2) — current members, NOT dated (history = event snapshot)
;
CREATE TABLE IF NOT EXISTS jute_prod_loom_group_dtl (
    loom_group_dtl_id INT     NOT NULL AUTO_INCREMENT,
    loom_group_id     INT     NOT NULL,
    machine_id        INT     NOT NULL,                       -- must be a loom (machine_type name 'Loom', ci) and active when NEWLY added
    active            TINYINT NOT NULL DEFAULT 1,             -- soft-delete + reinsert on edit
    PRIMARY KEY (loom_group_dtl_id),
    KEY idx_lgd_group   (loom_group_id, active),
    KEY idx_lgd_machine (machine_id),
    CONSTRAINT fk_lgd_group   FOREIGN KEY (loom_group_id) REFERENCES jute_prod_loom_group (loom_group_id),
    CONSTRAINT fk_lgd_machine FOREIGN KEY (machine_id)    REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. Loom Modification event register (§4.3) — one row per chain stage
;
CREATE TABLE IF NOT EXISTS jute_prod_loom_modification (
    modification_id        INT          NOT NULL AUTO_INCREMENT,
    co_id                  INT          NOT NULL,
    branch_id              INT          NULL,                      -- derived from the first snapshot loom -> dept_mst.branch_id
    loom_group_id          INT          NULL,                      -- set when the looms were taken from a group, NULL = hand-picked list
    mod_date               DATE         NOT NULL,                  -- the day the change was done / started
    mod_to_date            DATE         NULL,                      -- last day of the work when it spanned days, NULL = same day
    settle_days            INT          NOT NULL DEFAULT 3,        -- settling-in days after the work, excluded from analysis
    mod_kind               VARCHAR(10)  NOT NULL DEFAULT 'INITIAL',-- INITIAL | FOLLOWUP | RECTIFY | REVERSAL (MOD_KINDS)
    parent_modification_id INT          NULL,                      -- the stage this one follows / rectifies / undoes, NULL for INITIAL
    root_modification_id   INT          NULL,                      -- stamped at save = parent's root, else own id (chain key)
    mod_category           VARCHAR(20)  NOT NULL,                  -- MOD_CATEGORIES: MECHANICAL | ELECTRICAL | SPEED_CHANGE | ACCESSORY | SETTING | OTHER
    title                  VARCHAR(150) NOT NULL,
    description            TEXT         NULL,                      -- what exactly was changed
    expected_effect        VARCHAR(300) NULL,                      -- the hypothesis
    done_by                VARCHAR(10)  NOT NULL DEFAULT 'INHOUSE',-- INHOUSE | VENDOR (MOD_DONE_BY)
    party_id               INT          NULL,                      -- party_mst.party_id, required when done_by = VENDOR
    total_cost             DOUBLE       NULL,
    remarks                VARCHAR(500) NULL,
    active                 TINYINT      NOT NULL DEFAULT 1,
    updated_by             INT          NULL,
    updated_date_time      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (modification_id),
    KEY idx_lm_co_branch_date (co_id, branch_id, mod_date),
    KEY idx_lm_group_date     (loom_group_id, mod_date),
    KEY idx_lm_root           (root_modification_id, mod_date),
    KEY idx_lm_parent         (parent_modification_id),
    CONSTRAINT fk_lm_group  FOREIGN KEY (loom_group_id)          REFERENCES jute_prod_loom_group (loom_group_id),
    CONSTRAINT fk_lm_parent FOREIGN KEY (parent_modification_id) REFERENCES jute_prod_loom_modification (modification_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. Frozen membership per event (§4.4) — one row per (event, loom) among active = 1
;
CREATE TABLE IF NOT EXISTS jute_prod_loom_modification_mc (
    modification_mc_id INT          NOT NULL AUTO_INCREMENT,
    modification_id    INT          NOT NULL,
    machine_id         INT          NOT NULL,                  -- a loom (validated in code)
    done_date          DATE         NULL,                      -- per-loom start, NULL = header mod_date
    done_to_date       DATE         NULL,                      -- per-loom end, NULL = done_date when set, else header end
    remarks            VARCHAR(200) NULL,
    active             TINYINT      NOT NULL DEFAULT 1,        -- soft-delete + reinsert on edit
    PRIMARY KEY (modification_mc_id),
    KEY idx_lmm_mod          (modification_id, active),
    KEY idx_lmm_machine_date (machine_id, done_date),
    CONSTRAINT fk_lmm_mod     FOREIGN KEY (modification_id) REFERENCES jute_prod_loom_modification (modification_id),
    CONSTRAINT fk_lmm_machine FOREIGN KEY (machine_id)      REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW TABLES LIKE 'jute_prod_loom%'
--     expected 4 rows: jute_prod_loom_group, jute_prod_loom_group_dtl,
--     jute_prod_loom_modification, jute_prod_loom_modification_mc
--
--   SHOW CREATE TABLE jute_prod_loom_modification
--     expected keys idx_lm_co_branch_date, idx_lm_group_date, idx_lm_root,
--     idx_lm_parent and constraints fk_lm_group, fk_lm_parent
--
--   SELECT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME
--     FROM information_schema.REFERENTIAL_CONSTRAINTS
--    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'jute_prod_loom%'
--     expected 6 rows (fk_lgd_group, fk_lgd_machine, fk_lm_group, fk_lm_parent,
--     fk_lmm_mod, fk_lmm_machine)
-- =============================================================================
-- ROLLBACK (children first, FK order)
-- =============================================================================
--   DROP TABLE IF EXISTS jute_prod_loom_modification_mc
--   DROP TABLE IF EXISTS jute_prod_loom_modification
--   DROP TABLE IF EXISTS jute_prod_loom_group_dtl
--   DROP TABLE IF EXISTS jute_prod_loom_group
-- =============================================================================
;
