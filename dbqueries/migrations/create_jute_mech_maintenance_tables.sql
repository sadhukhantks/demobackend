-- Migration: Jute Mechanical & Maintenance module — core schema (10 tables)
-- Date: 2026-08-24
-- Database: tenant DB (dev3, sls)
-- Author: VOW dev
--
-- Replaces the mill-side mechanical breakdown Excel workbooks with a structured
-- incident log + preventive-maintenance/overhaul register.
--
-- REUSES existing masters — creates NO new machine/dept/shift master:
--   machine_mst        (machine_id, machine_name, mech_code, machine_type_id, dept_id)
--   machine_type_mst   (machine_type_id, machine_type_name)  -- == the Excel "section"
--   dept_mst           (dept_id, dept_desc, branch_id)       -- branch derived via machine->dept
--   sub_dept_mst       (attending crew: MECH FACTORY SIDE / MOTOR / FOUNDRY)
--   spell_mst          (spell_id, spell_code, working_hours) -- NOTE: status=1, not active=1
--   shift_mst          (shift_id, shift_name, is_overnight)
--   item_mst           (spare parts)   party_mst (overhaul vendor)
--
-- machine_mst has NO co_id and NO branch_id. branch_id on the transaction tables is
-- DERIVED on insert via:  machine_mst m -> dept_mst d ON d.dept_id = m.dept_id -> d.branch_id
-- (same derivation used by jute_prod_stoppage_hours).
--
-- Deliberately independent of jute_prod_stoppage_hours: this module is the engineering
-- log, stoppage hours stays the production hours-accounting feed. No writes across.
--
-- Audit: updated_by + updated_date_time only (created_* is trigger-based per CLAUDE.md).
-- Soft delete: active TINYINT DEFAULT 1.
--
-- ROLLBACK (reverse order of creation):
--   DROP TABLE IF EXISTS jute_mech_pm_execution_dtl;
--   DROP TABLE IF EXISTS jute_mech_pm_execution;
--   DROP TABLE IF EXISTS jute_mech_pm_schedule;
--   DROP TABLE IF EXISTS jute_mech_overhaul_spare;
--   DROP TABLE IF EXISTS jute_mech_overhaul;
--   DROP TABLE IF EXISTS jute_mech_breakdown_spare;
--   DROP TABLE IF EXISTS jute_mech_breakdown;
--   DROP TABLE IF EXISTS jute_mech_machine_attr;
--   DROP TABLE IF EXISTS jute_mech_reason_mst;
--   DROP TABLE IF EXISTS jute_mech_component_mst;

-- =============================================================================
-- 1. COMPONENT MASTER
-- Cross-section failure component (Gear, Chain, Bearing, Roller, Motor, Pump...).
-- Reason codes are machine-type-specific (DHGT only exists for Draw Head, RF only
-- for Spreader), so component is what makes mill-wide Pareto possible.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_component_mst (
    component_id      INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    component_name    VARCHAR(100) NOT NULL,
    component_group   VARCHAR(50)  NULL,          -- Transmission | Bearing | Roller | Electrical | Hydraulic | Structural
    remarks           VARCHAR(255) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (component_id),
    UNIQUE KEY uq_mech_component_co_name (co_id, component_name),
    KEY idx_mech_component_co (co_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 2. BREAKDOWN REASON MASTER
-- Scoped by machine_type_id so the entry screen shows only that section's codes.
-- machine_type_id NULL = global code (NR, MT, PP + the electrical set).
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_reason_mst (
    reason_id         INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    machine_type_id   INT          NULL,          -- NULL = applies to every machine type
    reason_code       VARCHAR(20)  NOT NULL,      -- RF, BJ, DHGT, FCGT, CSGWO ...
    reason_desc       VARCHAR(255) NOT NULL,
    discipline        VARCHAR(20)  NOT NULL DEFAULT 'MECHANICAL',  -- MECHANICAL|ELECTRICAL|UTILITY|OPERATIONAL
    component_id      INT          NULL,
    remarks           VARCHAR(255) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (reason_id),
    UNIQUE KEY uq_mech_reason_co_type_code (co_id, machine_type_id, reason_code),
    KEY idx_mech_reason_co_type (co_id, machine_type_id, active),
    KEY idx_mech_reason_component (component_id),
    CONSTRAINT fk_mech_reason_mctype  FOREIGN KEY (machine_type_id) REFERENCES machine_type_mst (machine_type_id),
    CONSTRAINT fk_mech_reason_comp    FOREIGN KEY (component_id)    REFERENCES jute_mech_component_mst (component_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 3. MACHINE MAINTENANCE ATTRIBUTES  (satellite on machine_mst)
-- Follows the spreader_machine_attr / jute_prod_drawing_machine_attr pattern:
-- one row per (co_id, machine_id). Keeps machine_mst untouched — it is shared
-- with every production module.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_machine_attr (
    machine_attr_id      INT          NOT NULL AUTO_INCREMENT,
    co_id                INT          NOT NULL,
    machine_id           INT          NOT NULL,
    make                 VARCHAR(100) NULL,
    model                VARCHAR(100) NULL,
    serial_no            VARCHAR(100) NULL,
    commissioning_date   DATE         NULL,
    criticality          VARCHAR(1)   NULL,       -- A = critical, B = important, C = normal
    last_overhaul_date   DATE         NULL,
    overhaul_freq_months INT          NULL,
    next_overhaul_due    DATE         NULL,       -- maintained on overhaul save
    last_pm_date         DATE         NULL,
    pm_freq_days         INT          NULL,
    next_pm_due          DATE         NULL,       -- maintained on PM execution save
    remarks              VARCHAR(255) NULL,
    active               TINYINT      NOT NULL DEFAULT 1,
    updated_by           INT          NULL,
    updated_date_time    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (machine_attr_id),
    UNIQUE KEY uq_mech_attr_co_machine (co_id, machine_id),
    KEY idx_mech_attr_overhaul_due (co_id, next_overhaul_due),
    KEY idx_mech_attr_pm_due       (co_id, next_pm_due),
    CONSTRAINT fk_mech_attr_machine FOREIGN KEY (machine_id) REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 4. BREAKDOWN — incident event log (one row per breakdown)
-- (machine_id, tran_date) intentionally NON-unique: many incidents per machine/day.
-- duration_min is computed by the app on save (restart - stop), NULL while OPEN.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_breakdown (
    breakdown_id        INT          NOT NULL AUTO_INCREMENT,
    co_id               INT          NOT NULL,
    branch_id           INT          NULL,        -- derived machine -> dept_mst.branch_id
    machine_id          INT          NOT NULL,
    tran_date           DATE         NOT NULL,    -- date part of stop_datetime, for indexing/reporting
    spell_id            INT          NULL,        -- FK spell_mst (spell_mst.status = 1)
    stop_datetime       DATETIME     NOT NULL,
    attended_datetime   DATETIME     NULL,        -- fitter reached machine -> response time
    restart_datetime    DATETIME     NULL,        -- NULL while breakdown is open
    duration_min        INT          NULL,        -- computed: restart - stop, in minutes
    response_min        INT          NULL,        -- computed: attended - stop, in minutes
    reason_id           INT          NOT NULL,
    component_id        INT          NULL,        -- defaults from reason, user may override
    discipline          VARCHAR(20)  NOT NULL DEFAULT 'MECHANICAL',
    downtime_type       VARCHAR(20)  NOT NULL DEFAULT 'BREAKDOWN',
                        -- BREAKDOWN | PLANNED_PM | OVERHAUL | NO_MATERIAL | NO_POWER | CHANGEOVER
    attended_dept_id    INT          NULL,        -- sub_dept_mst: MECH FACTORY SIDE / MOTOR / FOUNDRY
    attended_by         VARCHAR(255) NULL,
    reported_by         VARCHAR(255) NULL,
    root_cause          TEXT         NULL,
    action_taken        TEXT         NULL,
    preventive_action   TEXT         NULL,
    production_loss_qty DECIMAL(12,3) NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'OPEN',   -- OPEN | UNDER_REPAIR | CLOSED
    remarks             VARCHAR(500) NULL,
    active              TINYINT      NOT NULL DEFAULT 1,
    updated_by          INT          NULL,
    updated_date_time   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (breakdown_id),
    KEY idx_mech_bd_co_branch_date (co_id, branch_id, tran_date),
    KEY idx_mech_bd_machine_date   (machine_id, tran_date),
    KEY idx_mech_bd_reason         (reason_id),
    KEY idx_mech_bd_component      (component_id),
    KEY idx_mech_bd_status         (co_id, status, active),
    KEY idx_mech_bd_stop_dt        (co_id, stop_datetime),
    KEY idx_mech_bd_spell          (spell_id),
    CONSTRAINT fk_mech_bd_machine FOREIGN KEY (machine_id)   REFERENCES machine_mst (machine_id),
    CONSTRAINT fk_mech_bd_reason  FOREIGN KEY (reason_id)    REFERENCES jute_mech_reason_mst (reason_id),
    CONSTRAINT fk_mech_bd_comp    FOREIGN KEY (component_id) REFERENCES jute_mech_component_mst (component_id),
    CONSTRAINT fk_mech_bd_spell   FOREIGN KEY (spell_id)     REFERENCES spell_mst (spell_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 5. BREAKDOWN SPARES CONSUMED (detail)
-- item_id references item_mst but is left unconstrained: item_mst PK type varies
-- across tenants and this table must migrate cleanly into every one of them.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_breakdown_spare (
    breakdown_spare_id INT           NOT NULL AUTO_INCREMENT,
    breakdown_id       INT           NOT NULL,
    item_id            INT           NOT NULL,    -- item_mst.item_id (no FK: cross-tenant safety)
    qty                DOUBLE        NULL,
    uom_id             INT           NULL,
    rate               DOUBLE        NULL,
    amount             DOUBLE        NULL,
    remarks            VARCHAR(255)  NULL,
    active             TINYINT       NOT NULL DEFAULT 1,
    updated_by         INT           NULL,
    updated_date_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (breakdown_spare_id),
    KEY idx_mech_bd_spare_bd   (breakdown_id, active),
    KEY idx_mech_bd_spare_item (item_id),
    CONSTRAINT fk_mech_bd_spare_bd FOREIGN KEY (breakdown_id) REFERENCES jute_mech_breakdown (breakdown_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 6. OVERHAUL REGISTER
-- On save the app updates jute_mech_machine_attr.last_overhaul_date + next_overhaul_due.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_overhaul (
    overhaul_id       INT           NOT NULL AUTO_INCREMENT,
    co_id             INT           NOT NULL,
    branch_id         INT           NULL,         -- derived machine -> dept_mst.branch_id
    machine_id        INT           NOT NULL,
    overhaul_type     VARCHAR(10)   NOT NULL DEFAULT 'MINOR',   -- MAJOR | MINOR
    from_date         DATE          NOT NULL,
    to_date           DATE          NULL,
    downtime_days     DECIMAL(6,2)  NULL,         -- computed: to_date - from_date + 1
    work_done         TEXT          NULL,
    done_by           VARCHAR(10)   NOT NULL DEFAULT 'INHOUSE', -- INHOUSE | VENDOR
    party_id          INT           NULL,         -- party_mst.party_id when done_by = VENDOR
    labour_cost       DOUBLE        NULL,
    spare_cost        DOUBLE        NULL,         -- rolled up from jute_mech_overhaul_spare
    total_cost        DOUBLE        NULL,
    next_due_date     DATE          NULL,
    remarks           VARCHAR(500)  NULL,
    active            TINYINT       NOT NULL DEFAULT 1,
    updated_by        INT           NULL,
    updated_date_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (overhaul_id),
    KEY idx_mech_oh_co_branch_date (co_id, branch_id, from_date),
    KEY idx_mech_oh_machine        (machine_id, from_date),
    KEY idx_mech_oh_due            (co_id, next_due_date),
    CONSTRAINT fk_mech_oh_machine FOREIGN KEY (machine_id) REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 7. OVERHAUL SPARES CONSUMED (detail)
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_overhaul_spare (
    overhaul_spare_id INT          NOT NULL AUTO_INCREMENT,
    overhaul_id       INT          NOT NULL,
    item_id           INT          NOT NULL,      -- item_mst.item_id (no FK: cross-tenant safety)
    qty               DOUBLE       NULL,
    uom_id            INT          NULL,
    rate              DOUBLE       NULL,
    amount            DOUBLE       NULL,
    remarks           VARCHAR(255) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (overhaul_spare_id),
    KEY idx_mech_oh_spare_oh   (overhaul_id, active),
    KEY idx_mech_oh_spare_item (item_id),
    CONSTRAINT fk_mech_oh_spare_oh FOREIGN KEY (overhaul_id) REFERENCES jute_mech_overhaul (overhaul_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 8. PREVENTIVE-MAINTENANCE SCHEDULE (checklist template)
-- machine_type_id set  -> template applies to every machine of that type
-- machine_id      set  -> task specific to one machine
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_pm_schedule (
    pm_schedule_id    INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    machine_type_id   INT          NULL,
    machine_id        INT          NULL,
    task_desc         VARCHAR(500) NOT NULL,
    frequency         VARCHAR(20)  NOT NULL,      -- DAILY|WEEKLY|MONTHLY|QUARTERLY|HALF_YEARLY|ANNUAL
    freq_days         INT          NULL,          -- resolved day count, drives next_pm_due
    seq_no            INT          NULL,
    remarks           VARCHAR(255) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (pm_schedule_id),
    KEY idx_mech_pm_sch_co_type (co_id, machine_type_id, active),
    KEY idx_mech_pm_sch_machine (machine_id),
    CONSTRAINT fk_mech_pm_sch_mctype  FOREIGN KEY (machine_type_id) REFERENCES machine_type_mst (machine_type_id),
    CONSTRAINT fk_mech_pm_sch_machine FOREIGN KEY (machine_id)      REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 9. PM EXECUTION (header — one PM visit on one machine)
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_pm_execution (
    pm_exec_id        INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    branch_id         INT          NULL,          -- derived machine -> dept_mst.branch_id
    machine_id        INT          NOT NULL,
    pm_date           DATE         NOT NULL,
    frequency         VARCHAR(20)  NULL,          -- which cycle this visit covers
    done_by           VARCHAR(255) NULL,
    downtime_min      INT          NULL,
    next_due_date     DATE         NULL,
    remarks           VARCHAR(500) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (pm_exec_id),
    KEY idx_mech_pm_exec_co_branch_date (co_id, branch_id, pm_date),
    KEY idx_mech_pm_exec_machine        (machine_id, pm_date),
    CONSTRAINT fk_mech_pm_exec_machine FOREIGN KEY (machine_id) REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 10. PM EXECUTION DETAIL (per-checklist-task result)
-- =============================================================================
CREATE TABLE IF NOT EXISTS jute_mech_pm_execution_dtl (
    pm_exec_dtl_id    INT          NOT NULL AUTO_INCREMENT,
    pm_exec_id        INT          NOT NULL,
    pm_schedule_id    INT          NULL,          -- NULL for an ad-hoc task not in the template
    task_desc         VARCHAR(500) NULL,          -- snapshot, so template edits don't rewrite history
    result            VARCHAR(10)  NOT NULL DEFAULT 'OK',   -- OK | NOT_OK | NA
    observation       VARCHAR(500) NULL,
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (pm_exec_dtl_id),
    KEY idx_mech_pm_dtl_exec (pm_exec_id, active),
    KEY idx_mech_pm_dtl_sch  (pm_schedule_id),
    CONSTRAINT fk_mech_pm_dtl_exec FOREIGN KEY (pm_exec_id)     REFERENCES jute_mech_pm_execution (pm_exec_id),
    CONSTRAINT fk_mech_pm_dtl_sch  FOREIGN KEY (pm_schedule_id) REFERENCES jute_mech_pm_schedule (pm_schedule_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- VERIFICATION
-- SHOW TABLES LIKE 'jute_mech_%';
-- SELECT COUNT(*) FROM information_schema.tables
--  WHERE table_schema = DATABASE() AND table_name LIKE 'jute_mech_%';   -- expect 10
