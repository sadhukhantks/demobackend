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
WHERE jm.status_id NOT IN (4, 6, 21)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));

CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS
SELECT
    w.waste_entry_id                                        AS waste_entry_id,
    w.co_id                                                 AS co_id,
    COALESCE(w.branch_id, d.branch_id)                      AS branch_id,
    w.tran_date                                             AS inward_date,
    w.item_id                                               AS item_id,
    w.waste_qty                                             AS actual_qty,
    w.waste_qty                                             AS actual_weight,
    ROUND(w.waste_qty - IFNULL(iss.issqty, 0), 3)           AS bal_qty,
    ROUND(w.waste_qty - IFNULL(iss.isswt, 0), 3)            AS bal_weight,
    'PROD'                                                  AS source_type,
    NULL                                                    AS jute_mr_li_id,
    NULL                                                    AS rate
FROM jute_prod_waste_entry w
INNER JOIN dept_mst d ON d.dept_id = w.dept_id
LEFT JOIN (
    SELECT ji.waste_entry_id,
           SUM(ji.quantity) AS issqty,
           SUM(ji.weight)   AS isswt
    FROM jute_issue ji
    WHERE ji.waste_entry_id IS NOT NULL
      AND (ji.status_id IS NULL OR ji.status_id <> 4)
    GROUP BY ji.waste_entry_id
) iss ON iss.waste_entry_id = w.waste_entry_id
WHERE w.active = 1

UNION ALL

SELECT
    NULL                                                    AS waste_entry_id,
    bm.co_id                                                AS co_id,
    jm.branch_id                                            AS branch_id,
    jm.out_date                                             AS inward_date,
    jml.actual_item_id                                      AS item_id,
    jml.actual_weight                                       AS actual_qty,
    jml.actual_weight                                       AS actual_weight,
    ROUND(jml.actual_weight
          - IFNULL(mriss.isswt, 0)
          - IFNULL(sold.soldwt, 0), 3)                      AS bal_qty,
    ROUND(jml.actual_weight
          - IFNULL(mriss.isswt, 0)
          - IFNULL(sold.soldwt, 0), 3)                      AS bal_weight,
    'MR'                                                    AS source_type,
    jml.jute_mr_li_id                                       AS jute_mr_li_id,
    jml.rate                                                AS rate
FROM jute_mr jm
INNER JOIN jute_mr_li jml ON jml.jute_mr_id = jm.jute_mr_id
INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
INNER JOIN item_mst im ON im.item_id = jml.actual_item_id
INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
                          AND ig.item_type_id = 3
LEFT JOIN (
    SELECT ji.jute_mr_li_id,
           SUM(ji.weight)   AS isswt
    FROM jute_issue ji
    WHERE ji.jute_mr_li_id IS NOT NULL
      AND (ji.status_id IS NULL OR ji.status_id <> 4)
    GROUP BY ji.jute_mr_li_id
) mriss ON mriss.jute_mr_li_id = jml.jute_mr_li_id
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
) sold ON sold.jute_mr_li_id = jml.jute_mr_li_id
WHERE jm.status_id NOT IN (4, 6, 21)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));

-- ===========================================================================
-- Migration: Returned MRs (status 48) stay in stock                (2026-10-03)
--
-- NOTE ON COMMENT PLACEMENT: the migration runner splits on the semicolon
-- character, so all commentary lives after the last statement and has none.
--
-- WHY (owner ruling 2026-10-03)
-- Status 48 RETURNED is only a marker for PO logic and reports. A lorry that is
-- sent back after inspection may still leave material that the mill consumed.
-- That retained material is bought, stocked, issuable and paid for. The MR lines
-- hold the retained quantity.
--
-- WHAT THIS DOES
-- Re-creates vw_jute_stock_outstanding and vw_jute_waste_stock_outstanding
-- (MR leg) with the eligibility predicate jm.status_id NOT IN (4, 6, 21), i.e.
-- the 48 exclusion is dropped. Both bodies are the latest definitions
-- (20260925_vw_jute_stock_outstanding_waste_kg_qty.sql and
-- 20260925_jute_waste_view_add_mr_lines.sql) with ONLY that predicate changed.
-- Mirrors the code constant JUTE_MR_STOCK_EXCLUDED_STATUSES = (4, 6, 21).
--
-- Run AFTER 20261003_jute_mr_is_returned_flag.sql. Apply to dev3 first. sls / amcl only after owner approval (run-migration
-- skill asks for the target DB).
--
-- ROLLBACK: re-run the two source files above (each re-creates its view with 48 excluded).
-- ===========================================================================
