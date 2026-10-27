-- add_jute_mr_returned_status_48.sql (2026-09-03)
-- New jute_mr status 48 RETURNED: lorry came in, sent back to supplier without settlement.
-- Must leave stock: excluded in constants.JUTE_MR_STOCK_EXCLUDED_STATUSES (code) AND in the view (here).
-- Run on dev3 first; sls only after owner approval; any tenant on the 2026-08-23 view definition.
-- Rollback:
--   DELETE FROM status_mst WHERE status_id = 48;
--   re-run dbqueries/migrations/add_sales_dtl_jute_mr_li_link_and_view_sold.sql section 3 (view only).

INSERT INTO status_mst (status_id, status_name)
SELECT 48, 'RETURNED' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM status_mst WHERE status_id = 48);

-- View body copied verbatim from
-- dbqueries/migrations/add_sales_dtl_jute_mr_li_link_and_view_sold.sql:100-168,
-- changing only the WHERE (status_id NOT IN adds 48).
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
WHERE jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));
