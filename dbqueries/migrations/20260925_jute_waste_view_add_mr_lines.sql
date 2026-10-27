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
WHERE jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));

-- ===========================================================================
-- Migration: purchased jute waste joins the waste stock pool     (2026-09-25)
--
-- NOTE ON COMMENT PLACEMENT: the project migration runner does
-- sql.split on the semicolon character and skips any chunk whose strip()
-- starts with '--'. A comment placed ABOVE a statement therefore SWALLOWS that
-- statement. All commentary in this file lives after the final statement on
-- purpose, and contains no semicolon. Do not move it to the top.
--
-- WHAT THIS DOES (owner ruling 2026-09-25, D3)
-- vw_jute_waste_stock_outstanding gains a second leg: jute_mr_li lines whose
-- item belongs to an item group of item_type_id 3 (jute waste). Waste bought
-- through the ordinary purchase chain (Jute PO -> Gate Entry -> Quality Check
-- -> MR) is therefore ONE stock per item together with the waste generated on
-- the floor (jute_prod_waste_entry). No new table, no new document.
--
-- COLUMN CONTRACT: the nine original columns keep their names and ordinals
--   waste_entry_id, co_id, branch_id, inward_date, item_id,
--   actual_qty, actual_weight, bal_qty, bal_weight
-- and three are APPENDED
--   source_type   'PROD' (Waste Entry booking) or 'MR' (purchased waste line)
--   jute_mr_li_id the MR line key, NULL on the PROD leg
--   rate          jml.rate on the MR leg, NULL on the PROD leg (informational
--                 -- the issue allocator prices an MR slice through the MR
--                 line guard, i.e. jml.actual_rate, same as a direct MR issue)
--
-- MR LEG RULES
--   * eligibility is the same as vw_jute_stock_outstanding
--     (jm.status_id NOT IN 4, 6, 21, 48 / jml.active = 1 /
--     jml.status NOT IN '4', '6'), so in-flight MRs are visible for reports
--     and the readers (issue picker, FIFO allocator) re-narrow to the issuable
--     statuses 1, 3, 13 themselves
--   * jute_mr has NO co_id column, so co_id comes from branch_mst
--   * inward_date = jm.out_date, matching the raw-jute view
--   * waste is kilograms only, so actual_qty = actual_weight = jml.actual_weight
--   * balance = actual_weight minus jute_issue rows carrying that jute_mr_li_id
--     (status_id NULL or <> 4, the waste view's own convention) minus approved
--     raw-jute sales lines (invoice_type 5, status 3, active), the sold
--     sub-select being copied verbatim from add_jute_mr_returned_status_48.sql
--   * the sold netting subtracts a NULL branch/co row nowhere: a line of a
--     type-3 item that is sold leaves this pool exactly as it leaves
--     vw_jute_stock_outstanding
--
-- vw_jute_stock_outstanding itself is NOT changed by this migration.
--
-- Apply to dev3 first. sls / amcl only after owner approval (run-migration
-- skill asks for the target DB).
--
-- ROLLBACK -- previous definition (dbqueries/migrations/add_jute_waste_stock.sql)
-- CREATE OR REPLACE VIEW vw_jute_waste_stock_outstanding AS
-- SELECT
--     w.waste_entry_id,
--     w.co_id,
--     COALESCE(w.branch_id, d.branch_id)                      AS branch_id,
--     w.tran_date                                             AS inward_date,
--     w.item_id,
--     w.waste_qty                                             AS actual_qty,
--     w.waste_qty                                             AS actual_weight,
--     ROUND(w.waste_qty - IFNULL(iss.issqty, 0), 3)           AS bal_qty,
--     ROUND(w.waste_qty - IFNULL(iss.isswt, 0), 3)            AS bal_weight
-- FROM jute_prod_waste_entry w
-- INNER JOIN dept_mst d ON d.dept_id = w.dept_id
-- LEFT JOIN (
--     SELECT ji.waste_entry_id,
--            SUM(ji.quantity) AS issqty,
--            SUM(ji.weight)   AS isswt
--     FROM jute_issue ji
--     WHERE ji.waste_entry_id IS NOT NULL
--       AND (ji.status_id IS NULL OR ji.status_id <> 4)
--     GROUP BY ji.waste_entry_id
-- ) iss ON iss.waste_entry_id = w.waste_entry_id
-- WHERE w.active = 1
-- (terminate the rollback statement with a semicolon when re-running it)
-- ===========================================================================
