-- =============================================================================
-- Migration: ITM-004 Daily power readings -- jute_power_meter_mst + jute_power_reading
-- =============================================================================
-- Date: 2026-10-18
-- TARGET: tenant DB -- dev3 FIRST. sls (co 2 / branch 29) ONLY on the owner's go
--         (the sls seed block below stays commented out until then).
-- Router:  src/juteMechanical/utility.py        (/api/mechUtility)
-- Queries: src/juteMechanical/utility_query.py
-- ORM:     src/juteMechanical/models.py          (JutePowerMeterMst, JutePowerReading)
-- Spec:    Empire Jute Mill AI GM and team/itmis/prompts/ITM-004-power-readings.md
--
-- WHAT: the mill's electrical shift keys the main grid meter and the DG sets
-- once a day (optionally per spell) -- opening / closing kWh (kWh = closing -
-- opening, or typed directly after a meter reset), DG running hours, diesel
-- litres, max demand kVA. The Power & DG report divides kWh by the DPR's day
-- tonnes (cloth / yarn) and the DPR gets a Power line.
--
-- TWO TABLES:
--   jute_power_meter_mst  one row per meter per (co_id, branch_id). meter_type
--                         GRID | DG | SOLAR. Managed from the entry page's
--                         Meters dialog. Never deleted -- deactivated, because
--                         readings reference the meter by CODE, not by id.
--   jute_power_reading    one row per meter per day (per spell when the mill
--                         reads meters at shift change). UNIQUE on
--                         (co_id, branch_id, tran_date, spell_id, meter_code).
--
-- spell_id SENTINEL: the spec had spell_id NULL = whole day. MySQL treats NULLs
-- as DISTINCT inside a UNIQUE key, so a nullable spell_id would let two
-- whole-day rows through uq_jpr_scope. spell_id is therefore INT NOT NULL
-- DEFAULT 0 with 0 = whole day. The API still shows and accepts null.
--
-- Audit: updated_by + updated_date_time only (created_* is trigger-based).
-- Soft delete: active TINYINT DEFAULT 1. A re-save under a soft-deleted key
-- re-activates that row in place (the router selects by key, then UPDATEs).
--
-- IDEMPOTENT: CREATE TABLE IF NOT EXISTS, seed INSERTs guarded by NOT EXISTS.
--
-- RUNNER NOTE: the pymysql runner (run-migration skill) splits this file on
-- the literal semicolon character and skips any chunk whose trimmed text
-- starts with '--'. Every comment block is therefore terminated by a lone
-- semicolon on its own line, and no comment prose contains a mid-line
-- semicolon. There are no DELIMITER blocks.
;

-- =============================================================================
-- 1. METER MASTER
-- =============================================================================
;
CREATE TABLE IF NOT EXISTS jute_power_meter_mst (
    power_meter_id    INT          NOT NULL AUTO_INCREMENT,
    co_id             INT          NOT NULL,
    branch_id         INT          NOT NULL,
    meter_code        VARCHAR(20)  NOT NULL,
    meter_name        VARCHAR(100) NOT NULL,
    meter_type        VARCHAR(10)  NOT NULL,     /* GRID | DG | SOLAR */
    active            TINYINT      NOT NULL DEFAULT 1,
    updated_by        INT          NULL,
    updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (power_meter_id),
    UNIQUE KEY uq_jpm_scope (co_id, branch_id, meter_code),
    KEY idx_jpm_branch (branch_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 2. READINGS
-- =============================================================================
;
CREATE TABLE IF NOT EXISTS jute_power_reading (
    power_reading_id  INT           NOT NULL AUTO_INCREMENT,
    co_id             INT           NOT NULL,
    branch_id         INT           NOT NULL,
    tran_date         DATE          NOT NULL,
    spell_id          INT           NOT NULL DEFAULT 0,   /* 0 = whole day. Sentinel so the UNIQUE key holds (NULLs are distinct in MySQL unique keys). The API shows null. */
    meter_code        VARCHAR(20)   NOT NULL,
    opening_kwh       DECIMAL(14,2) NULL,
    closing_kwh       DECIMAL(14,2) NULL,
    kwh               DECIMAL(12,2) NOT NULL,             /* closing - opening, or typed after a meter reset */
    dg_hours          DECIMAL(6,2)  NULL,
    dg_litres         DECIMAL(10,2) NULL,
    max_demand_kva    DECIMAL(10,2) NULL,
    remarks           VARCHAR(255)  NULL,
    active            TINYINT       NOT NULL DEFAULT 1,
    updated_by        INT           NULL,
    updated_date_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (power_reading_id),
    UNIQUE KEY uq_jpr_scope (co_id, branch_id, tran_date, spell_id, meter_code),
    KEY idx_jpr_branch_date (branch_id, tran_date),
    KEY idx_jpr_meter_date (co_id, branch_id, meter_code, tran_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================================================
-- 3. SEED -- dev3 only, resolved BY NAME (no hardcoded ids)
-- =============================================================================
-- The Empire Jute Company Limited (co_id 1 on dev3) / branch 'Factory'
-- (branch_id 2 on dev3): one MAIN grid meter and one DG set. If the names do
-- not resolve on the target the derived table is empty and the INSERT is a
-- silent no-op -- run the VERIFICATION query afterwards.
;
INSERT INTO jute_power_meter_mst (co_id, branch_id, meter_code, meter_name, meter_type, active, updated_by)
SELECT b.co_id, b.branch_id, 'MAIN', 'Main grid meter', 'GRID', 1, 1
FROM branch_mst b
JOIN co_mst c ON c.co_id = b.co_id
WHERE c.co_name = 'The Empire Jute Company Limited'
  AND b.branch_name = 'Factory'
  AND NOT EXISTS (
      SELECT 1 FROM jute_power_meter_mst m
      WHERE m.co_id = b.co_id AND m.branch_id = b.branch_id AND m.meter_code = 'MAIN'
  );

INSERT INTO jute_power_meter_mst (co_id, branch_id, meter_code, meter_name, meter_type, active, updated_by)
SELECT b.co_id, b.branch_id, 'DG1', 'DG set 1', 'DG', 1, 1
FROM branch_mst b
JOIN co_mst c ON c.co_id = b.co_id
WHERE c.co_name = 'The Empire Jute Company Limited'
  AND b.branch_name = 'Factory'
  AND NOT EXISTS (
      SELECT 1 FROM jute_power_meter_mst m
      WHERE m.co_id = b.co_id AND m.branch_id = b.branch_id AND m.meter_code = 'DG1'
  );

-- -----------------------------------------------------------------------------
-- sls seed (co 2 / branch 29) -- DO NOT RUN until the owner says so. Kept
-- commented out on purpose. Uncomment both statements when sls goes live.
-- -----------------------------------------------------------------------------
-- INSERT INTO jute_power_meter_mst (co_id, branch_id, meter_code, meter_name, meter_type, active, updated_by)
-- SELECT b.co_id, b.branch_id, 'MAIN', 'Main grid meter', 'GRID', 1, 1
-- FROM branch_mst b
-- WHERE b.co_id = 2 AND b.branch_id = 29
--   AND NOT EXISTS (
--       SELECT 1 FROM jute_power_meter_mst m
--       WHERE m.co_id = b.co_id AND m.branch_id = b.branch_id AND m.meter_code = 'MAIN'
--   )
--
-- INSERT INTO jute_power_meter_mst (co_id, branch_id, meter_code, meter_name, meter_type, active, updated_by)
-- SELECT b.co_id, b.branch_id, 'DG1', 'DG set 1', 'DG', 1, 1
-- FROM branch_mst b
-- WHERE b.co_id = 2 AND b.branch_id = 29
--   AND NOT EXISTS (
--       SELECT 1 FROM jute_power_meter_mst m
--       WHERE m.co_id = b.co_id AND m.branch_id = b.branch_id AND m.meter_code = 'DG1'
--   )
;

-- =============================================================================
-- VERIFICATION -- run manually against the target after applying
-- =============================================================================
--   SHOW CREATE TABLE jute_power_meter_mst
--   SHOW CREATE TABLE jute_power_reading
--   SELECT * FROM jute_power_meter_mst
--   -- dev3 expectation: two rows, co_id 1 / branch_id 2, MAIN (GRID) and DG1 (DG)
--   SELECT meter_code, meter_type FROM jute_power_meter_mst WHERE co_id = 1 AND branch_id = 2
;

-- =============================================================================
-- ROLLBACK -- readings first (they reference meters by code only, no FK)
-- =============================================================================
--   DROP TABLE IF EXISTS jute_power_reading
--   DROP TABLE IF EXISTS jute_power_meter_mst
-- =============================================================================
