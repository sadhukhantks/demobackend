-- ============================================================================
-- vw_jute_stock_outstanding — widen MR eligibility + expose mr_status_id
-- ============================================================================
-- Target DB   : any tenant (dev3, sls, amcl, ...) — tenant-agnostic, rerunnable
-- Date        : 2026-08-13
-- Author      : migration-writer agent
--
-- WHAT THIS VIEW IS
-- The single authority for jute MR stock. Read by mrInStock, MR Wise, Godown Wise
-- and by the Jute Issue stock picker. Balance = receipt line minus every issue
-- ever booked against it — there is NO date filter, and this migration does not
-- add one.
--
-- WHY THE CHANGE
-- The old WHERE clause was `jm.status_id IN (3,13)` (Approved / Closed). That is
-- narrower than the eligibility rule the jute balance reports are standardising
-- on, so the same physical stock appeared in one family of reports and not the
-- other. It also ignored line-level soft-deletes entirely, so a deactivated or
-- cancelled jute_mr_li kept contributing its full actual_qty to stock forever.
--
-- WHAT CHANGES
--   1. Header eligibility  IN (3,13)  ->  NOT IN (4, 6, 21)
--      Admits OPEN 1, PENDING-L2 17 and TRANSFER-PENDING 24 in addition to
--      Approved 3 / Closed 13. Excludes REJECTED 4, CANCELLED 6, DRAFT 21.
--   2. Line soft-delete honoured   AND jml.active = 1
--   3. Line status honoured        AND (jml.status IS NULL OR jml.status NOT IN ('4','6'))
--      jute_mr_li.status is VARCHAR — compare against strings, not integers.
--   4. NEW COLUMN mr_status_id (= jm.status_id) appended LAST.
--
-- NOTE: a tare_weight guard was considered and deliberately LEFT OUT. MR status
-- already covers it for new entries, and the migrated legacy rows need a separate
-- investigation.
--
-- COLUMN CONTRACT
-- Every existing column keeps its name, type and ordinal position. mr_status_id is
-- APPENDED, so `SELECT *` consumers gain a column but never lose or reorder one.
--
-- IMPACT ON THE ISSUE PICKER — ACTION REQUIRED IN CODE
-- The view is now BROADER than approved-only stock. The Jute Issue stock picker
-- must stay restricted to approved stock, so its queries need
--     AND v.mr_status_id IN (3,13)
-- That is exactly why mr_status_id is exposed here. Ship this migration and that
-- code change together, or the picker will offer un-approved MR stock for issue.
--
-- KNOWN HAZARD LEFT UNCHANGED
-- bal_accepted_weight divides by jml.actual_qty and therefore returns NULL on any
-- line with actual_qty = 0 or NULL. Nothing currently reads this column, so the
-- expression is carried over untouched rather than fixed here. Fix it in its own
-- migration if a report ever starts consuming it.
--
-- DEFINER NOTE
-- The live view was created with DEFINER=`Tarun` / SQL SECURITY DEFINER. CREATE OR
-- REPLACE below re-stamps the definer to whoever runs the migration. Harmless in
-- this project (all app connections share one DB user), but worth knowing.
--
-- ROLLBACK — restores the previous live definition verbatim as captured by
-- SHOW CREATE VIEW on sls and dev3 on 2026-08-13 (both tenants byte-identical).
-- Uncomment and run to reverse.
--
--   CREATE OR REPLACE VIEW `vw_jute_stock_outstanding` AS select `jml`.`jute_mr_li_id` AS
--   `jute_mr_li_id`,`jm`.`out_date` AS `inward_date`,`jm`.`branch_id` AS `branch_id`,`jm`.`branch_mr_no`
--   AS `branch_mr_no`,`jm`.`jute_gate_entry_no` AS `jute_gate_entry_no`,`wm`.`warehouse_name` AS
--   `warehouse_name`,`jml`.`actual_quality` AS `actual_quality`,`jml`.`actual_item_id` AS
--   `actual_item_id`,`jml`.`actual_qty` AS `actual_qty`,`jml`.`actual_weight` AS
--   `actual_weight`,`jm`.`unit_conversion` AS `unit_conversion`,(`jml`.`actual_qty` -
--   ifnull(`iss`.`issqty`,0)) AS `bal_qty`,round((`jml`.`actual_weight` - ifnull(`iss`.`isswt`,0)),3) AS
--   `bal_weight`,`jml`.`accepted_weight` AS `accepted_weight`,(`jml`.`accepted_weight` -
--   round(((`jml`.`accepted_weight` / `jml`.`actual_qty`) * ifnull(`iss`.`issqty`,0)),3)) AS
--   `bal_accepted_weight`,`jml`.`rate` AS `rate`,`jml`.`actual_rate` AS `actual_rate` from (((`jute_mr`
--   `jm` join `jute_mr_li` `jml` on((`jm`.`jute_mr_id` = `jml`.`jute_mr_id`))) left join `warehouse_mst`
--   `wm` on((`wm`.`warehouse_id` = `jml`.`warehouse_id`))) left join (select `ji`.`jute_mr_li_id` AS
--   `jute_mr_li_id`,sum(`ji`.`quantity`) AS `issqty`,sum(`ji`.`weight`) AS `isswt` from `jute_issue`
--   `ji` where (`ji`.`status_id` <> 4) group by `ji`.`jute_mr_li_id`) `iss` on((`iss`.`jute_mr_li_id` =
--   `jml`.`jute_mr_li_id`))) where (`jm`.`status_id` in (3,13))
-- ============================================================================

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
    (jml.actual_qty - IFNULL(iss.issqty, 0))                       AS bal_qty,
    ROUND((jml.actual_weight - IFNULL(iss.isswt, 0)), 3)           AS bal_weight,
    jml.accepted_weight                                            AS accepted_weight,
    -- HAZARD: NULL when jml.actual_qty is 0 or NULL. Carried over unchanged --
    -- nothing reads this column today.
    (jml.accepted_weight
       - ROUND(((jml.accepted_weight / jml.actual_qty) * IFNULL(iss.issqty, 0)), 3))
                                                                   AS bal_accepted_weight,
    jml.rate                                                       AS rate,
    jml.actual_rate                                                AS actual_rate,
    -- NEW: lets the Jute Issue picker re-narrow to approved stock with
    -- `AND mr_status_id IN (3,13)` now that the view itself is broader.
    jm.status_id                                                   AS mr_status_id
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
WHERE jm.status_id NOT IN (4, 6, 21)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'));
