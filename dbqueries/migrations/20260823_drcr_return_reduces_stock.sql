-- Migration: 20260823_drcr_return_reduces_stock.sql
-- Target DB: EVERY tenant that has proc_inward/drcr_note (dev3, sls, amcl, vownjm)
-- Date: 2026-08-23   Scope: 2 views, no data change
--
-- WHY
-- Stock never subtracted goods returned to the supplier. vw_approved_inward_qty
-- computed  balance_qty = approved_qty - issue_qty  with no DR-note term, even
-- though src/models/procurement.py documents current stock as
-- "inward_qty - drcr_qty - issue_qty" (lines 395 and 446). Consequences:
--   * Inventory -> Material Issue picker offered lots already returned
--     (get_available_inward_inventory_query / get_searchable_inventory_list_query)
--   * vw_item_balance_qty_by_branch_new.cur_stock = SUM(balance_qty) overstated,
--     so indent/PO min-max validation used a stock figure that was too high
-- Reported case: SRN LC/FACTORY/24-25/000122 (br 87), item 141-0056 -- 20 pcs
-- returned to the supplier on 2024-05-31 (drcr note 62), yet 17 pcs were still
-- issued from stores in Jun/Jul 2026 because the picker still showed them.
--
-- WHAT COUNTS AS A RETURN  (vw_drcr_return_qty)
--   approved note (status_id = 3), DEBIT note (adjustment_type = 1),
--   quantity-rejection line (debitnote_type = 1), quantity > 0.
-- Rate-difference lines are excluded: they carry quantity = approved_qty with
-- rate = the rate DIFF (src/procurement/sr.py approve_sr), so counting them
-- would wipe the whole lot's stock.
--
-- WHY THE rejected_qty NETTING
-- An SR-time rejection is already OUT of approved_qty AND is mirrored by an
-- auto DR note of exactly rejected_qty -- subtracting that note again would
-- double-count. Only the portion of the returned qty that EXCEEDS rejected_qty
-- ever entered stores. The running-sum window applies that allowance once per
-- inward line, oldest note first, so multi-note lines net correctly too.
--
-- sls PREREQUISITE: run 20260823_fix_migrated_drcr_debitnote_type.sql FIRST --
-- migrated rows carry the inverted legacy enum and would otherwise be read as
-- rate differences.
--
-- ROLLBACK
--   DROP VIEW IF EXISTS vw_drcr_return_qty;
--   CREATE OR REPLACE VIEW vw_approved_inward_qty AS
--   SELECT pid.inward_dtl_id, pid.inward_id, pid.item_id, pid.approved_qty,
--          pid.uom_id, pid.rate, pid.accepted_rate, pi.inward_date, pi.branch_id,
--          pi.sr_status, COALESCE(ili.total_issue_qty, 0) AS issue_qty,
--          ROUND(pid.approved_qty - COALESCE(ili.total_issue_qty, 0), 3) AS balance_qty
--     FROM proc_inward_dtl pid
--     JOIN proc_inward pi ON pid.inward_id = pi.inward_id
--     LEFT JOIN (SELECT il.inward_dtl_id, SUM(il.issue_qty) AS total_issue_qty
--                  FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
--                 WHERE (ih.active = 1 OR ih.active IS NULL)
--                   AND (ih.status_id IS NULL OR ih.status_id <> 6)
--                 GROUP BY il.inward_dtl_id) ili
--            ON pid.inward_dtl_id = ili.inward_dtl_id
--    WHERE pi.sr_status = 3 AND pid.active = 1;

-- ---------------------------------------------------------------------------
-- 1. Single source of truth for "goods returned to supplier", at note-line
--    grain so date-ranged reports can place the movement in the right period.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_drcr_return_qty AS
SELECT
    x.drcr_note_dtl_id,
    x.debit_credit_note_id,
    x.inward_dtl_id,
    x.inward_id,
    x.item_id,
    x.branch_id,
    x.note_date,
    GREATEST(LEAST(x.quantity, x.cum_qty - x.rejected_qty), 0) AS return_qty
FROM (
    SELECT
        dnd.drcr_note_dtl_id,
        dnd.debit_credit_note_id,
        dnd.inward_dtl_id,
        pid.inward_id,
        pid.item_id,
        pi.branch_id,
        dn.`date`                       AS note_date,
        dnd.quantity                    AS quantity,
        COALESCE(pid.rejected_qty, 0)   AS rejected_qty,
        SUM(dnd.quantity) OVER (
            PARTITION BY dnd.inward_dtl_id
            ORDER BY dn.`date`, dnd.drcr_note_dtl_id
            ROWS UNBOUNDED PRECEDING
        )                               AS cum_qty
    FROM drcr_note_dtl dnd
    JOIN drcr_note      dn  ON dn.debit_credit_note_id = dnd.debit_credit_note_id
    JOIN proc_inward_dtl pid ON pid.inward_dtl_id      = dnd.inward_dtl_id
    JOIN proc_inward     pi  ON pi.inward_id           = pid.inward_id
    WHERE dn.status_id       = 3
      AND dn.adjustment_type = 1
      AND dnd.debitnote_type = 1
      AND dnd.quantity       > 0
      AND pid.active         = 1
      AND pi.sr_status       = 3
) x;

-- ---------------------------------------------------------------------------
-- 2. Stock per approved inward line, now net of returns.
--    New column: returned_qty -- the name src/models/inventory.py
--    VwApprovedInwardQty already declares (the model was written for this
--    behaviour; only the view was missing it).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_approved_inward_qty AS
SELECT
    pid.inward_dtl_id,
    pid.inward_id,
    pid.item_id,
    pid.approved_qty,
    pid.uom_id,
    pid.rate,
    pid.accepted_rate,
    pi.inward_date,
    pi.branch_id,
    pi.sr_status,
    COALESCE(ili.total_issue_qty, 0)  AS issue_qty,
    COALESCE(dr.total_return_qty, 0)  AS returned_qty,
    ROUND(pid.approved_qty
          - COALESCE(ili.total_issue_qty, 0)
          - COALESCE(dr.total_return_qty, 0), 3) AS balance_qty
FROM proc_inward_dtl pid
JOIN proc_inward pi ON pid.inward_id = pi.inward_id
LEFT JOIN (
    SELECT il.inward_dtl_id, SUM(il.issue_qty) AS total_issue_qty
    FROM issue_li il
    JOIN issue_hdr ih ON ih.issue_id = il.issue_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
      AND (ih.status_id IS NULL OR ih.status_id <> 6)
    GROUP BY il.inward_dtl_id
) ili ON pid.inward_dtl_id = ili.inward_dtl_id
LEFT JOIN (
    SELECT inward_dtl_id, SUM(return_qty) AS total_return_qty
    FROM vw_drcr_return_qty
    GROUP BY inward_dtl_id
) dr ON pid.inward_dtl_id = dr.inward_dtl_id
WHERE pi.sr_status = 3
  AND pid.active   = 1;
