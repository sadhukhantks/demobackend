-- ============================================================================
-- Raw-jute sales get a LINE-level link to the MR line they were sold from, and
-- vw_jute_stock_outstanding subtracts those sales per line.
-- ============================================================================
-- Target DB   : any tenant (dev3, sls, amcl, ...) — tenant-agnostic, rerunnable
-- Date        : 2026-08-23
-- Applies to  : sales_invoice_dtl (new nullable column + index),
--               vw_jute_stock_outstanding (CREATE OR REPLACE)
--
-- WHY
-- Raw jute is sold straight out of MR stock (invoice_type = 5). The jute balance
-- REPORTS already subtract those sales per branch (`_sold_cte` in
-- reportQueries.py), but the per-MR-LINE ledger — vw_jute_stock_outstanding,
-- which feeds the Jute Issue picker, mrInStock, MR Wise and Godown Wise — had no
-- way to see a sale, so every sold MR line stayed "in stock" forever. On sls
-- branch 87 that is 1,129 legacy lines / ~90,900 qtl that were sold in the old
-- system and still show as issuable. Zeroing them with adjustment issue rows
-- would double-count the outflow in the reports (sale AND issue), so the line
-- ledger has to learn about sales instead. The old system (vowsls
-- invoice_line_items.mr_line_id) kept exactly this link; the new schema dropped it.
--
-- WHAT CHANGES
--   1. sales_invoice_dtl.jute_mr_li_id  BIGINT NULL  (+ index idx_sid_jute_mr_li_id)
--      NULL = "not attributed to an MR line" (today: every row on every tenant;
--      the sls branch-87 backfill is a separate script). Nothing in the app
--      writes it yet, so new invoices stay NULL and the view behaves exactly as
--      before for them.
--   2. vw_jute_stock_outstanding:
--        bal_weight = actual_weight - issued_weight - sold_weight
--        bal_qty    = actual_qty    - issued_qty    - sold_qty
--      where sold_* come from approved raw-jute invoice lines carrying this
--      jute_mr_li_id (same invoice predicate as `_sold_cte`: invoice_type 5,
--      status 3, active). A sales line has no bale/drum count, so sold_qty is
--      PRO-RATED by weight: actual_qty * sold_weight / actual_weight.
--      Two NEW columns appended LAST: sold_qty, sold_weight.
--
-- COLUMN CONTRACT
-- Every existing view column keeps its name, type and ordinal position; the two
-- new columns are appended, so `SELECT *` consumers gain columns but never lose
-- or reorder one. On a tenant with no linked sales the view output is identical
-- to before (sold_* = 0).
--
-- DEPLOY ORDER
-- No code depends on this. Safe to apply tenant by tenant at any time. The
-- column MUST exist before the view is replaced (same script, in order).
--
-- ROLLBACK
--   CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS <previous definition>;
--     sls + dev3: update_vw_jute_stock_outstanding_eligibility.sql (18 columns,
--       NOT IN (4,6,21)) — verified byte-identical to live on 2026-08-23.
--     amcl / vownjm and any tenant NOT yet on the eligibility migration are still
--       on the 17-column IN (3,13) view — capture SHOW CREATE VIEW there BEFORE
--       applying and inline that text as the rollback; do not reuse the sls one.
--   ALTER TABLE sales_invoice_dtl DROP INDEX idx_sid_jute_mr_li_id,
--                                 DROP COLUMN jute_mr_li_id;
--   (restore the view FIRST — it references the column)
--   Full branch-87 exercise reverses in the order 3 -> 2 -> 1:
--     zero_jute_stock_br87_20260331.sql, sls_br87_backfill_sales_dtl_jute_mr_li.sql,
--     then this file.
--
-- KNOWN CONSUMER FIXED ALONGSIDE: vowslsdatatransfer steps step11/step12
-- rollback.sql used `REPLACE INTO sales_invoice_dtl SELECT * FROM bak_...` (17
-- columns) — rewritten to an explicit column list on 2026-08-23 so this 18th
-- column cannot break them.
-- ============================================================================

-- fail fast instead of queueing every later sales query behind a stuck ALTER
SET SESSION lock_wait_timeout = 10;

-- 1. Column (idempotent — MySQL 8.0 has no ADD COLUMN IF NOT EXISTS)
SET @col_missing := (
    SELECT COUNT(*) = 0
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name   = 'sales_invoice_dtl'
      AND column_name  = 'jute_mr_li_id'
);
SET @ddl := IF(@col_missing,
    'ALTER TABLE sales_invoice_dtl ADD COLUMN jute_mr_li_id BIGINT NULL COMMENT ''MR line this raw-jute sale drew from (jute_mr_li.jute_mr_li_id) - NULL = unattributed''',
    'SELECT ''sales_invoice_dtl.jute_mr_li_id already exists'' AS note');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_missing := (
    SELECT COUNT(*) = 0
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name   = 'sales_invoice_dtl'
      AND index_name   = 'idx_sid_jute_mr_li_id'
);
SET @ddl := IF(@idx_missing,
    'ALTER TABLE sales_invoice_dtl ADD INDEX idx_sid_jute_mr_li_id (jute_mr_li_id)',
    'SELECT ''idx_sid_jute_mr_li_id already exists'' AS note');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. View
CREATE OR REPLACE VIEW vw_jute_stock_outstanding AS
SELECT
    jml.jute_mr_li_id                                              AS jute_mr_li_id,
    jm.out_date                                                    AS inward_date,
    jm.branch_id                                                   AS branch_id,
    jm.branch_mr_no                                                AS branch_mr_no,
    jm.jute_gate_entry_no                                          AS jute_gate_entry_no,
    wm.warehouse_name                                              AS warehouse_name,
    jml.actual_quality                                             AS actual_quality,
    jml.actual_item_id                                             AS actual_item_id,
    jml.actual_qty                                                 AS actual_qty,
    jml.actual_weight                                              AS actual_weight,
    jm.unit_conversion                                             AS unit_conversion,
    -- balance = receipt - issued - sold (sold qty pro-rated by weight)
    (jml.actual_qty
       - IFNULL(iss.issqty, 0)
       - CASE WHEN jml.actual_weight > 0
              THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
              ELSE 0 END)                                          AS bal_qty,
    ROUND((jml.actual_weight
       - IFNULL(iss.isswt, 0)
       - IFNULL(sold.soldwt, 0)), 3)                               AS bal_weight,
    jml.accepted_weight                                            AS accepted_weight,
    -- HAZARD (unchanged): NULL when jml.actual_qty is 0 or NULL. Nothing reads it.
    (jml.accepted_weight
       - ROUND(((jml.accepted_weight / jml.actual_qty) * IFNULL(iss.issqty, 0)), 3))
                                                                   AS bal_accepted_weight,
    jml.rate                                                       AS rate,
    jml.actual_rate                                                AS actual_rate,
    jm.status_id                                                   AS mr_status_id,
    -- NEW: what left this line through approved raw-jute sales invoices
    CASE WHEN jml.actual_weight > 0
         THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
         ELSE 0 END                                                AS sold_qty,
    IFNULL(sold.soldwt, 0)                                         AS sold_weight
FROM jute_mr jm
JOIN jute_mr_li jml
    ON jm.jute_mr_id = jml.jute_mr_id
LEFT JOIN warehouse_mst wm
    ON wm.warehouse_id = jml.warehouse_id
LEFT JOIN (
    SELECT
        ji.jute_mr_li_id      AS jute_mr_li_id,
        SUM(ji.quantity)      AS issqty,
        SUM(ji.weight)        AS isswt
    FROM jute_issue ji
    WHERE ji.status_id <> 4
    GROUP BY ji.jute_mr_li_id
) iss
    ON iss.jute_mr_li_id = jml.jute_mr_li_id
LEFT JOIN (
    -- Same invoice predicate as reportQueries._sold_cte (raw jute = invoice
    -- type 5, approved = 3). Weight falls back to quantity the same way.
    SELECT
        sid.jute_mr_li_id                                         AS jute_mr_li_id,
        SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)) AS soldwt
    FROM sales_invoice_dtl sid
    JOIN sales_invoice sinv
        ON sinv.invoice_id = sid.invoice_id
    WHERE sid.jute_mr_li_id IS NOT NULL
      AND sinv.invoice_type = 5
      AND sinv.status_id = 3
      AND COALESCE(sinv.active, 1) = 1
    GROUP BY sid.jute_mr_li_id
) sold
    ON sold.jute_mr_li_id = jml.jute_mr_li_id
WHERE jm.status_id NOT IN (4, 6, 21)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));
