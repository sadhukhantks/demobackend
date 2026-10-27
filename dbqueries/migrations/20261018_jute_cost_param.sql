-- =============================================================================
-- Migration: jute_cost_param -- costing parameters, effective-dated EAV (ITM-019)
-- =============================================================================
-- Date: 2026-10-18 (owner Q04 values, 2026-10-18)
-- TARGET: tenant DB only -- dev3 first. sls (co 2, br 29) ONLY on the owner's go:
--         the sls seed block below stays commented out until then.
-- Router: src/juteCosting/params.py   (prefix /api/juteCostParam)
-- ORM:    src/juteCosting/models.py
-- Report: src/juteCosting/daily_pl.py (prefix /api/juteCostReports) prices the
--         Daily P&L with these rows.
--
-- One NEW table, jute_cost_param: one row per (co_id, branch_id, effective_date,
-- param_code, product). branch_id NULL = all branches, product NULL = all
-- products. A change going forward is a NEW row with a later effective date
-- (history is kept, the master's edit files the new row).
--
-- Resolution rule (params.resolve_params): the active row with
-- MAX(effective_date) <= the report date, branch-specific over all-branch,
-- product-specific over all-product, newest id on a tie.
--
-- MySQL treats NULL as distinct inside a UNIQUE key, so uq_jute_cost_param cannot
-- block an all-branch / all-product duplicate. That guard lives in code
-- (params_query.check_cost_param_duplicate_query) and runs before every insert.
--
-- SEED (dev3, co 1 = Empire / branch 2 = Factory, from the test-credentials
-- registry). The ids are hardcoded, so every seed INSERT is ALSO guarded by an
-- EXISTS on branch_mst / co_mst that checks the names behind those ids
-- (co_mst.co_name and branch_mst.branch_name verified on dev3 and sls,
-- 2026-10-19). On any other tenant -- where ids 1 / 2 belong to someone else
-- or to nothing (sls has no branch 2) -- the whole block is a no-op, the way
-- ITM-004's 20261018_jute_power_reading.sql resolves its seed by name.
-- Nine rows effective 2026-10-01. NOT seeded (NULL by design, so the report lists
-- them under not_keyed until the owner keys them): JUTE_RATE_PER_KG (the booked
-- issue value is used instead), POWER_RATE_PER_KWH (waits for ITM-004),
-- FIXED_COST_PER_DAY (optional).
--
-- ENCODING: this file is UTF-8 (the uom column carries the rupee sign) and the
-- table is utf8mb4 -- run it over a utf8mb4 connection (the pymysql default).
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own.
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS jute_cost_param
;

-- 1. The table
;
CREATE TABLE IF NOT EXISTS jute_cost_param (
    cost_param_id     INT            NOT NULL AUTO_INCREMENT,
    co_id             INT            NOT NULL,
    branch_id         INT            NULL,                    -- NULL = all branches
    effective_date    DATE           NOT NULL,
    param_code        VARCHAR(40)    NOT NULL,                -- params.PARAM_CODES
    param_value       DECIMAL(16,4)  NOT NULL,
    uom               VARCHAR(20)    NULL,
    product           VARCHAR(20)    NULL,                    -- SACKING | HESSIAN | YARN | NULL = all
    source            VARCHAR(120)   NULL,                    -- where the value came from
    remarks           VARCHAR(255)   NULL,
    active            TINYINT        NOT NULL DEFAULT 1,
    updated_by        INT            NULL,
    updated_date_time TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (cost_param_id),
    UNIQUE KEY uq_jute_cost_param (co_id, branch_id, effective_date, param_code, product),
    KEY idx_jcp_lookup (co_id, param_code, effective_date),
    KEY idx_jcp_scope  (co_id, branch_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. dev3 seed -- co 1 / branch 2, effective 2026-10-01, updated_by 1.
--    Each INSERT is idempotent (NOT EXISTS on the exact key, NULL-safe on product).
;
INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'REALISATION_PER_T', 182000, '₹/t', 'SACKING', 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'REALISATION_PER_T' AND (product <=> 'SACKING'))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'REALISATION_PER_T', 210000, '₹/t', 'HESSIAN', 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'REALISATION_PER_T' AND (product <=> 'HESSIAN'))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'JUTE_PER_KG_CLOTH', 1.06, 'kg/kg', NULL, 'Sep 2026 gain/loss statement (analysis 2026-10-18)', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'JUTE_PER_KG_CLOTH' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'WAGE_PER_HAND_DAY', 400, '₹/hand-day', NULL, 'proxy', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'WAGE_PER_HAND_DAY' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'COST_OF_CAPITAL_PCT', 14, '%', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'COST_OF_CAPITAL_PCT' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'DUST_SALE_PER_KG', 1, '₹/kg', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'DUST_SALE_PER_KG' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'RESTART_CAPEX_PER_FRAME', 150000, '₹/frame', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'RESTART_CAPEX_PER_FRAME' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'PAYMENT_DAYS', 7, 'days', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'PAYMENT_DAYS' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
SELECT 1, 2, '2026-10-01', 'BALE_WEIGHT_TOLERANCE', 5, 'kg', NULL, 'owner Q04 2026-10-18', 'uom to confirm', 1, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 1 AND branch_id = 2 AND effective_date = '2026-10-01' AND param_code = 'BALE_WEIGHT_TOLERANCE' AND (product <=> NULL))
  AND EXISTS (SELECT 1 FROM branch_mst b JOIN co_mst c ON c.co_id = b.co_id WHERE b.branch_id = 2 AND b.co_id = 1 AND b.branch_name = 'Factory' AND c.co_name = 'The Empire Jute Company Limited');

-- 3. sls seed -- co 2 / branch 29, the same nine rows, guarded the same way
--    (sls branch 29 = 'FACTORY' of co 2). COMMENTED OUT: run only when the
--    owner says go for sls, by removing the leading comment markers.
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'REALISATION_PER_T', 182000, '₹/t', 'SACKING', 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'REALISATION_PER_T' AND (product <=> 'SACKING'))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'REALISATION_PER_T', 210000, '₹/t', 'HESSIAN', 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'REALISATION_PER_T' AND (product <=> 'HESSIAN'))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'JUTE_PER_KG_CLOTH', 1.06, 'kg/kg', NULL, 'Sep 2026 gain/loss statement (analysis 2026-10-18)', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'JUTE_PER_KG_CLOTH' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'WAGE_PER_HAND_DAY', 400, '₹/hand-day', NULL, 'proxy', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'WAGE_PER_HAND_DAY' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'COST_OF_CAPITAL_PCT', 14, '%', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'COST_OF_CAPITAL_PCT' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'DUST_SALE_PER_KG', 1, '₹/kg', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'DUST_SALE_PER_KG' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'RESTART_CAPEX_PER_FRAME', 150000, '₹/frame', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'RESTART_CAPEX_PER_FRAME' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'PAYMENT_DAYS', 7, 'days', NULL, 'owner Q04 2026-10-18', NULL, 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'PAYMENT_DAYS' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
--
-- INSERT INTO jute_cost_param (co_id, branch_id, effective_date, param_code, param_value, uom, product, source, remarks, active, updated_by)
-- SELECT 2, 29, '2026-10-01', 'BALE_WEIGHT_TOLERANCE', 5, 'kg', NULL, 'owner Q04 2026-10-18', 'uom to confirm', 1, 1
-- FROM DUAL
-- WHERE NOT EXISTS (SELECT 1 FROM jute_cost_param WHERE co_id = 2 AND branch_id = 29 AND effective_date = '2026-10-01' AND param_code = 'BALE_WEIGHT_TOLERANCE' AND (product <=> NULL))
--   AND EXISTS (SELECT 1 FROM branch_mst b WHERE b.branch_id = 29 AND b.co_id = 2 AND b.branch_name = 'FACTORY')
;

-- VERIFICATION (dev3: expect 9 rows, every one active)
;
SELECT param_code, product, param_value, uom
FROM jute_cost_param
WHERE co_id = 1 AND branch_id = 2
ORDER BY param_code, product;
