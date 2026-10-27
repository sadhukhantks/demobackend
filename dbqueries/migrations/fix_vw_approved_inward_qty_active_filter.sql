-- =============================================================================
-- Fix: vw_approved_inward_qty must exclude soft-deleted inward detail lines.
--
-- Problem
--   The view drives "Available Inventory" on /dashboardportal/inventory/issue/
--   createIssue (get_available_inward_inventory_query / get_searchable_inventory_*
--   in src/inventory/query.py). It joined proc_inward_dtl with NO `active` filter,
--   so any soft-deleted SR line still showed as issuable stock.
--
--   Two ways rows get soft-deleted:
--     1. app-side  — update_inward reconciles line items and sets active = 0
--     2. migration — vowsls rows with tbl_proc_inward_detail.is_active = 0 were
--                    imported as active = 1 (see the companion repair script
--                    sls_deactivate_migrated_inactive_inward_dtl.sql)
--
--   Every Stores-Inventory report query already filters `pid.active = 1`
--   (src/inventory/reportQueries.py), so the view was the sole surface that
--   disagreed — which is exactly the "stock in new software, NIL in old" symptom.
--
-- Change
--   Add `AND pid.active = 1` to the view's WHERE clause. Column list unchanged.
--
-- Targets: dev3 + sls now; every other tenant BEFORE the code deploy.
--
-- ROLLBACK (restores the pre-fix definition):
--   CREATE OR REPLACE VIEW vw_approved_inward_qty AS
--   SELECT pid.inward_dtl_id, pid.inward_id, pid.item_id, pid.approved_qty,
--          pid.uom_id, pid.rate, pid.accepted_rate, pi.inward_date, pi.branch_id,
--          pi.sr_status, COALESCE(ili.total_issue_qty, 0) AS issue_qty,
--          ROUND(pid.approved_qty - COALESCE(ili.total_issue_qty, 0), 3) AS balance_qty
--   FROM proc_inward_dtl pid
--   JOIN proc_inward pi ON pid.inward_id = pi.inward_id
--   LEFT JOIN (
--       SELECT il.inward_dtl_id, SUM(il.issue_qty) AS total_issue_qty
--       FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
--       WHERE (ih.active = 1 OR ih.active IS NULL)
--         AND (ih.status_id IS NULL OR ih.status_id <> 6)
--       GROUP BY il.inward_dtl_id
--   ) ili ON pid.inward_dtl_id = ili.inward_dtl_id
--   WHERE pi.sr_status = 3;
-- =============================================================================

CREATE OR REPLACE VIEW vw_approved_inward_qty AS
SELECT
    pid.inward_dtl_id                                                   AS inward_dtl_id,
    pid.inward_id                                                       AS inward_id,
    pid.item_id                                                         AS item_id,
    pid.approved_qty                                                    AS approved_qty,
    pid.uom_id                                                          AS uom_id,
    pid.rate                                                            AS rate,
    pid.accepted_rate                                                   AS accepted_rate,
    pi.inward_date                                                      AS inward_date,
    pi.branch_id                                                        AS branch_id,
    pi.sr_status                                                        AS sr_status,
    COALESCE(ili.total_issue_qty, 0)                                    AS issue_qty,
    ROUND(pid.approved_qty - COALESCE(ili.total_issue_qty, 0), 3)       AS balance_qty
FROM proc_inward_dtl pid
JOIN proc_inward pi
  ON pid.inward_id = pi.inward_id
LEFT JOIN (
    SELECT il.inward_dtl_id, SUM(il.issue_qty) AS total_issue_qty
    FROM issue_li il
    JOIN issue_hdr ih ON ih.issue_id = il.issue_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
      AND (ih.status_id IS NULL OR ih.status_id <> 6)
    GROUP BY il.inward_dtl_id
) ili
  ON pid.inward_dtl_id = ili.inward_dtl_id
WHERE pi.sr_status = 3
  AND pid.active = 1;
