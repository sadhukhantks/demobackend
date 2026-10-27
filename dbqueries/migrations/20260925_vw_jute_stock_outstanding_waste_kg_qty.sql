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
    -- purchased WASTE (type-3 item) is kg-only: its qty ledger is its weight
    CASE WHEN ig.item_type_id = 3
         THEN jml.actual_weight
         ELSE jml.actual_qty END                                   AS actual_qty,
    jml.actual_weight                                              AS actual_weight,
    jm.unit_conversion                                             AS unit_conversion,
    -- balance = receipt - issued - sold (sold qty pro-rated by weight)
    CASE WHEN ig.item_type_id = 3
         THEN ROUND((jml.actual_weight
                - IFNULL(iss.isswt, 0)
                - IFNULL(sold.soldwt, 0)), 3)
         ELSE (jml.actual_qty
                - IFNULL(iss.issqty, 0)
                - CASE WHEN jml.actual_weight > 0
                       THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
                       ELSE 0 END) END                             AS bal_qty,
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
    CASE WHEN ig.item_type_id = 3
         THEN IFNULL(sold.soldwt, 0)
         WHEN jml.actual_weight > 0
         THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
         ELSE 0 END                                                AS sold_qty,
    IFNULL(sold.soldwt, 0)                                         AS sold_weight
FROM jute_mr jm
JOIN jute_mr_li jml
    ON jm.jute_mr_id = jml.jute_mr_id
LEFT JOIN warehouse_mst wm
    ON wm.warehouse_id = jml.warehouse_id
LEFT JOIN item_mst im
    ON im.item_id = jml.actual_item_id
LEFT JOIN item_grp_mst ig
    ON ig.item_grp_id = im.item_grp_id
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
WHERE jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));

-- ===========================================================================
-- Migration: purchased-waste lines are kg-only in vw_jute_stock_outstanding
--                                                                (2026-09-25)
--
-- NOTE ON COMMENT PLACEMENT: the project migration runner splits on the
-- semicolon character and skips any chunk whose strip() starts with '--', so
-- a comment ABOVE the statement would swallow it. All commentary lives after
-- the statement on purpose and contains no semicolon. Do not move it up.
--
-- WHAT THIS DOES (owner ruling 2026-09-25, D3/D4 follow-up)
-- An MR line whose item sits in an item group of item_type_id 3 (jute waste)
-- is bought in bales/loose units (jml.actual_qty) but issued in KILOGRAMS as
-- FIFO slices of the grouped waste pool, whose jute_issue.quantity IS the kg.
-- With actual_qty in units and issues in kg, bal_qty for such a line went
-- negative on the first pooled issue (sls line 45659: 108 units received,
-- one 500 kg slice, bal_qty -392) and the MR-in-stock / MR-wise issue
-- reports showed negative unit balances. For a type-3 line this view now
-- reports actual_qty, bal_qty and sold_qty on the WEIGHT ledger (kg) so the
-- qty columns agree with the weight columns. Raw-jute (type-2) lines and
-- lines whose item has no group are unchanged (the group is LEFT JOINed).
--
-- The view body is otherwise copied verbatim from
-- dbqueries/migrations/add_jute_mr_returned_status_48.sql (2026-09-03), the
-- definition live on dev3 and sls. bal_weight, accepted_weight,
-- bal_accepted_weight, rate, actual_rate, mr_status_id, sold_weight and the
-- WHERE clause are untouched.
--
-- Readers: the Jute Issue picker (already excludes type-3 lines from its MR
-- arm), the MR-line write guard (already ignores bal_qty for a type-3 line)
-- and the Cluster B reports in src/juteProcurement/reportQueries.py, which
-- read bal_qty/actual_qty as-is and are what this fixes.
--
-- ORDER: apply together with 20260925_jute_waste_view_add_mr_lines.sql,
-- BEFORE the backend that ships the grouped purchased-waste pool. dev3 first,
-- sls / amcl only after owner approval (run-migration skill asks for the
-- target DB).
--
-- ROLLBACK: re-run the CREATE OR REPLACE VIEW vw_jute_stock_outstanding
-- statement in dbqueries/migrations/add_jute_mr_returned_status_48.sql
-- (view section only, not the status_mst insert).
-- ===========================================================================
