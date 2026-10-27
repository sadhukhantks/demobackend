-- =============================================================================
-- Migration: Period Master — period types (rules) + generated dated periods
-- =============================================================================
-- Date: 2026-10-24
-- TARGET: tenant DB (dev3 first, sls after owner review).
--
-- A reusable calendar of periods that reports and wages can reference.
--
--   period_type_mst   one rule per (co, branch, name): FORTNIGHTLY, MONTHLY,
--                     WEEKLY or CUSTOM. WEEKLY uses week_start_day
--                     (ISO 1=Mon .. 7=Sun) and has no slots.
--   period_type_slot  month split for the non-weekly types, e.g. Fortnightly:
--                       slot 1 "1st Fortnight" F1  day 1  .. 15
--                       slot 2 "2nd Fortnight" F2  day 16 .. NULL (= month end)
--                     from_prev_month = 1 (slot 1 only) starts the cycle in the
--                     previous month, e.g. a 26th-to-25th payroll month.
--   period_mst        dated periods generated from a type for a year. Unique
--                     per (type, from_date) so re-generating is idempotent.
--
-- Separate from the legacy forthnight_period_mst (no co/branch, used by the
-- EJM FNE wage screens) and from pay_period (one row per wage run).
--
-- ROLLBACK:
--   DROP TABLE period_mst;
--   DROP TABLE period_type_slot;
--   DROP TABLE period_type_mst;
;

CREATE TABLE IF NOT EXISTS period_type_mst (
    period_type_id   INT NOT NULL AUTO_INCREMENT,
    co_id            INT NOT NULL,
    branch_id        INT NOT NULL,
    period_type_name VARCHAR(100) NOT NULL,
    frequency        VARCHAR(20) NOT NULL,
    week_start_day   TINYINT NULL,
    remarks          VARCHAR(255) NULL,
    active           INT NOT NULL DEFAULT 1,
    updated_by       INT NULL,
    updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (period_type_id),
    UNIQUE KEY uq_period_type_name (co_id, branch_id, period_type_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS period_type_slot (
    period_type_slot_id INT NOT NULL AUTO_INCREMENT,
    period_type_id      INT NOT NULL,
    slot_no             INT NOT NULL,
    slot_name           VARCHAR(50) NOT NULL,
    slot_code           VARCHAR(10) NOT NULL,
    from_day            TINYINT NOT NULL,
    from_prev_month     TINYINT NOT NULL DEFAULT 0,
    to_day              TINYINT NULL,
    PRIMARY KEY (period_type_slot_id),
    UNIQUE KEY uq_period_type_slot (period_type_id, slot_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS period_mst (
    period_id           INT NOT NULL AUTO_INCREMENT,
    period_type_id      INT NOT NULL,
    period_type_slot_id INT NULL,
    co_id               INT NOT NULL,
    branch_id           INT NOT NULL,
    period_year         INT NOT NULL,
    period_month        INT NOT NULL,
    period_no           INT NOT NULL,
    period_code         VARCHAR(20) NOT NULL,
    period_name         VARCHAR(100) NOT NULL,
    from_date           DATE NOT NULL,
    to_date             DATE NOT NULL,
    active              INT NOT NULL DEFAULT 1,
    updated_by          INT NULL,
    updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (period_id),
    UNIQUE KEY uq_period_type_from (period_type_id, from_date),
    KEY idx_period_scope_dates (co_id, branch_id, from_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
