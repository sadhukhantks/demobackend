-- Migration: add_drcr_returns_to_vw_approved_inward_qty.sql
-- Date: 2026-08-19
-- Tenant scope: dev3, sls, amcl  (+ vownjm — see DEPLOY SURFACE below)
--
-- PROBLEM
--   vw_approved_inward_qty.balance_qty = approved_qty - issues, and ignores
--   DR/CR notes. When goods are sent BACK to the supplier after the store
--   receipt was approved, nothing removes them from stock, so Create Issue
--   keeps offering material that left the warehouse.
--   Measured on sls: 3,065.84 units across 49 inward lines. 38 of those 49
--   lines were received, fully returned and never issued — they currently show
--   their entire receipt as available (e.g. inward_dtl 18159 = 1,200 units,
--   20341 = 500, 15801 = 400, 15802 = 200).
--
-- WHY THIS DOES NOT VIOLATE THE 2026-08-11 "DO NOT SUBTRACT DR/CR" RULE
--   fix_vw_item_balance_qty_by_branch_new_stock_subquery.sql removed a DR/CR
--   term because it was a blanket SUM(drcr_note_dtl.quantity) with no status,
--   adjustment_type or line-semantics filter. That warning stands. It rejected
--   two specific things, and BOTH remain excluded here:
--     1. Rejections already netted out. approved_qty = inward_qty - rejected_qty
--        (src/procurement/material_inspection.py:248, sole write path), so
--        rejected goods never entered stock. Subtracting a note raised FROM
--        rejected_qty double-counts it.
--     2. Rate-difference notes. They carry quantity = approved_qty with
--        rate = the price delta purely to price a money document
--        (src/procurement/sr.py:1007-1018). No goods move.
--   What that migration did NOT consider is the complement: a full-value return
--   booked after SR approval, where rejected_qty = 0 so nothing was ever netted.
--   That is the only thing subtracted here.
--
-- THE DISCRIMINATOR IS THE RATE, NOT debitnote_type
--   debitnote_type is NOT reliable across tenants:
--     * dev3 follows the code convention (1 = Qty Rejection, 2 = Rate Diff,
--       src/procurement/drcr_note.py:51-52).
--     * sls is inverted — its legacy vowsls rows book quantity rejections as
--       debitnote_type = 2. 221 of 352 sls detail rows are type 2 carrying
--       quantity = rejected_qty, with remarks like 'DUE TO POOR QUALITY',
--       'DUE TO EXCESS SUPPLY', 'DUE TO SHORT RECEIVED', 'NOT RECIVED'.
--   A debitnote_type predicate would therefore be right on one tenant and wrong
--   on the other. The rate is not ambiguous:
--     note rate == line rate  -> the supplier is being charged the FULL price
--                                of the goods == the goods went back.
--     note rate <  line rate  -> the note carries only a price delta == money
--                                only, stock untouched.
--   Verified: every one of the 50 sls auto-created full-rate notes has
--   quantity = rejected_qty and therefore deducts 0 under the formula below.
--
-- THE FORMULA
--   returned_qty = GREATEST(SUM(note quantity) - rejected_qty, 0)
--   SUM first, subtract rejected_qty ONCE. 30 sls lines carry more than one
--   note (max 3); netting rejected_qty per note instead of per line would
--   subtract it up to 3 times. GREATEST floors already-netted rejections at 0
--   instead of letting them add stock back.
--
-- PREDICATE
--   dn.status_id       = 3   Approved only. Matches bill-pass netting
--                            (src/procurement/query.py:2629/2639/2765/2774) and
--                            accounting (posting_service.py:847-848). Excludes
--                            21 draft / 1 open / 4 rejected; 20 and 6 are
--                            unreachable (drcr_note has no send-for-approval,
--                            cancel or reopen endpoint).
--   dn.adjustment_type = 1   Debit note = supplier owes us = goods went back.
--                            Credit (2) means we owe the supplier — a rate rise.
--   rate equality            As above. pid.rate <> 0 guards the comparison;
--                            0.005 tolerance for float noise.
--
-- MEASURED IMPACT
--   sls   49 lines deduct, 3,065.84 qty removed. Positive lots 7,842 -> 7,800.
--         38 lines settle to exactly 0 (received, returned, never issued).
--         10 lines go negative — see DATA CONFLICT below.
--   dev3  0 lines. Its 12 approved notes are all auto-created rejections
--         already netted into approved_qty; its 2 manual notes are QA browser
--         -test rows. NOTE: dev3 inward lines carry rate = 0, so the rate test
--         cannot match there — the term is inert on dev3 by construction.
--   amcl  0 lines. vw_approved_inward_qty returns 0 rows on amcl (no sr_status
--         = 3 inwards at all) and drcr_note is empty. Applied for parity only.
--
-- DATA CONFLICT SURFACED (pre-existing, not caused by this change)
--   10 sls lines were BOTH returned to the supplier AND issued to production —
--   the same units consumed twice in the data. They will read negative:
--     19189 -100 | 19735 -100 | 23547 -60 | 20794 -50 | 15500 -40
--     18764  -20 | 15499  -17 | 18997  -6 | 20456  -6 | 20743  -4
--   All branch 87. This needs an operational answer (which document is wrong),
--   not a view change. Negatives are invisible to Create Issue — all three
--   consumers filter balance_qty > 0 (src/inventory/query.py:319/369/399) — so
--   the lot simply stops being offered, which is the safe outcome.
--
-- ALSO NORMALISES TENANT DRIFT (all three end byte-identical)
--   amcl was missing  AND pid.active = 1  and the ROUND(...,3) on balance_qty.
--   dev3 and sls already agreed with each other.
--
-- NEW COLUMN
--   returned_qty is exposed beside issue_qty so the deduction is auditable the
--   same way issues are. All three consuming queries name columns explicitly —
--   no SELECT * consumer exists — so adding a column is safe.
--
-- DOWNSTREAM — DO NOT ALSO ADD THE TERM THERE
--   vw_item_balance_qty_by_branch     = SUM(balance_qty) over this view.
--   vw_item_balance_qty_by_branch_new = cur_stock is a rollup of balance_qty.
--   Both inherit this change automatically. Adding a DR/CR term to either would
--   subtract the returns twice.
--
-- DEPLOY SURFACE
--   Four tenant DBs carry this view: dev3, sls, amcl, vownjm.
--   vownjm is NOT covered by this script: its definition additionally lacks the
--   issue_hdr join entirely (cancelled and inactive issues count as consumed),
--   i.e. it never received fix_vw_approved_inward_qty_exclude_cancelled_inactive.sql
--   or fix_vw_approved_inward_qty_active_filter.sql. Replay those first, then
--   this script applies unchanged.
--
-- ROLLBACK — restore the pre-change definition (dev3/sls form):
--   CREATE OR REPLACE ALGORITHM = UNDEFINED SQL SECURITY DEFINER
--   VIEW vw_approved_inward_qty AS
--   SELECT pid.inward_dtl_id, pid.inward_id, pid.item_id, pid.approved_qty,
--          pid.uom_id, pid.rate, pid.accepted_rate, pi.inward_date,
--          pi.branch_id, pi.sr_status,
--          COALESCE(ili.total_issue_qty, 0) AS issue_qty,
--          ROUND(pid.approved_qty - COALESCE(ili.total_issue_qty, 0), 3) AS balance_qty
--   FROM proc_inward_dtl pid
--   JOIN proc_inward pi ON pid.inward_id = pi.inward_id
--   LEFT JOIN (SELECT il.inward_dtl_id, SUM(il.issue_qty) AS total_issue_qty
--              FROM issue_li il JOIN issue_hdr ih ON ih.issue_id = il.issue_id
--              WHERE (ih.active = 1 OR ih.active IS NULL)
--                AND (ih.status_id IS NULL OR ih.status_id <> 6)
--              GROUP BY il.inward_dtl_id) ili
--     ON pid.inward_dtl_id = ili.inward_dtl_id
--   WHERE pi.sr_status = 3 AND pid.active = 1;
--
-- RUNNER NOTE: the migration runner splits on ';' and skips any chunk starting
-- with '--', which would swallow the statement after a comment block. The
-- SELECT below absorbs this header; no comments follow between statements.
SELECT 'add_drcr_returns_to_vw_approved_inward_qty' AS migration;

CREATE OR REPLACE
ALGORITHM = UNDEFINED
SQL SECURITY DEFINER
VIEW vw_approved_inward_qty AS
SELECT
    pid.inward_dtl_id                                   AS inward_dtl_id,
    pid.inward_id                                       AS inward_id,
    pid.item_id                                         AS item_id,
    pid.approved_qty                                    AS approved_qty,
    pid.uom_id                                          AS uom_id,
    pid.rate                                            AS rate,
    pid.accepted_rate                                   AS accepted_rate,
    pi.inward_date                                      AS inward_date,
    pi.branch_id                                        AS branch_id,
    pi.sr_status                                        AS sr_status,
    COALESCE(ili.total_issue_qty, 0)                    AS issue_qty,
    COALESCE(dnr.total_returned_qty, 0)                 AS returned_qty,
    ROUND(pid.approved_qty
          - COALESCE(ili.total_issue_qty, 0)
          - COALESCE(dnr.total_returned_qty, 0), 3)     AS balance_qty
FROM proc_inward_dtl pid
JOIN proc_inward pi
  ON pid.inward_id = pi.inward_id
LEFT JOIN (
    SELECT il.inward_dtl_id       AS inward_dtl_id,
           SUM(il.issue_qty)      AS total_issue_qty
    FROM issue_li il
    JOIN issue_hdr ih ON ih.issue_id = il.issue_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
      AND (ih.status_id IS NULL OR ih.status_id <> 6)
    GROUP BY il.inward_dtl_id
) ili
  ON pid.inward_dtl_id = ili.inward_dtl_id
LEFT JOIN (
    SELECT dnd.inward_dtl_id AS inward_dtl_id,
           GREATEST(SUM(COALESCE(dnd.quantity, 0))
                    - COALESCE(MAX(rpid.rejected_qty), 0), 0) AS total_returned_qty
    FROM drcr_note_dtl dnd
    JOIN drcr_note dn
      ON dn.debit_credit_note_id = dnd.debit_credit_note_id
    JOIN proc_inward_dtl rpid
      ON rpid.inward_dtl_id = dnd.inward_dtl_id
    WHERE dn.status_id = 3
      AND dn.adjustment_type = 1
      AND COALESCE(rpid.rate, 0) <> 0
      AND ABS(COALESCE(dnd.rate, 0) - COALESCE(rpid.rate, 0)) < 0.005
    GROUP BY dnd.inward_dtl_id
) dnr
  ON pid.inward_dtl_id = dnr.inward_dtl_id
WHERE pi.sr_status = 3
  AND pid.active = 1;

SELECT COUNT(*)                        AS view_rows,
       SUM(returned_qty > 0)           AS rows_with_returns,
       COALESCE(SUM(returned_qty), 0)  AS total_returned_qty,
       SUM(balance_qty > 0)            AS issuable_rows,
       SUM(balance_qty < 0)            AS negative_rows
FROM vw_approved_inward_qty;

SELECT inward_dtl_id, branch_id, item_id, approved_qty, issue_qty,
       returned_qty, balance_qty
FROM vw_approved_inward_qty
WHERE returned_qty > 0
ORDER BY balance_qty, inward_dtl_id;
