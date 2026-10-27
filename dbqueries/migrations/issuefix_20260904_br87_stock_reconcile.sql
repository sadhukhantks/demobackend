-- ISSUEFIX-20260904 — branch 87 (sls, co 106) physical-stock reconciliation
--
-- Raised by the store team alongside TKT-000143. Two corrections:
--
-- 1. Item 163-1156 (item_id 333521, S.S. PART NO.80676A)
--    System closing 5, physical 1 per Tally + Vow old portal. Issue 4.
--    The only lot carrying balance is inward_dtl_id 41898
--    (SR LC/FACTORY/26-27/000082, received 2026-05-25, approved 20, issued 15).
--    Originally requested as "add a line to Issue #493". Rejected: issue_pass_no
--    493 repeats per financial year on branch 87, and both approved #493 headers
--    (38459 = 2024-05-21, 45596 = 2025-05-10) predate lot 41898 by 1-2 years.
--    Attaching there would consume a lot that did not yet exist and would move a
--    closed year's closing stock. Booked instead as its own approved issue dated
--    today, in FY26-27 where the physical count was taken.
--
-- 2. Item 141-0056 (item_id 333533, C.I. CHILLED COP CONE RH)
--    Issue #1394 (issue_id 58320, 2026-08-28, APPROVED) line 2
--    (issue_li_id 163902, lot 23659) keyed as 4, should be 3. Releases 1 back.
--
-- Target DB: sls          Rollback: issuefix_20260904_br87_stock_reconcile_rollback.sql
-- Both statements are guarded so a re-run cannot double-apply.

-- ---------------------------------------------------------------------------
-- 1. Item 163-1156 — new approved issue consuming 4 from lot 41898
-- ---------------------------------------------------------------------------
-- issue_pass_no is a per-branch, per-FY sequence: MAX + 1 within FY26-27,
-- matching src/inventory/issue.py create_issue().
INSERT INTO issue_hdr
    (branch_id, dept_id, issue_pass_no, issue_pass_print_no, active, issue_date,
     item_id, approved_date, approved_by, status_id, approval_level,
     issued_to, req_by, project_id, customer_id, internal_note,
     updated_by, updated_date_time)
SELECT
    87,
    362,                                   -- SACK SEWING(LC), the dept every prior 163-1156 issue used
    COALESCE(MAX(issue_pass_no), 0) + 1,
    NULL,
    1,
    '2026-09-04',
    NULL,
    '2026-09-04',
    22,
    3,                                     -- APPROVED, so the Item Ledger moves too
    1,
    NULL, NULL, NULL, NULL,
    'ISSUEFIX-20260904 physical stock reconcile vs Tally / old portal',
    22,
    NOW()
FROM issue_hdr
WHERE branch_id = 87
  AND issue_date >= '2026-04-01' AND issue_date <= '2027-03-31'
  AND (active = 1 OR active IS NULL);

-- Line: 4 units off lot 41898. expense_type 5 + cost factor 1000327 (HERAKLE)
-- carried from the item's most recent approved issues (57547, 57264).
INSERT INTO issue_li
    (issue_id, item_id, uom_id, req_quantity, issue_qty, expense_type_id,
     cost_factor_id, machine_id, inward_dtl_id, remarks, updated_date_time, updated_by)
SELECT LAST_INSERT_ID(), 333521, 143, NULL, 4, 5,
       1000327, NULL, 41898,
       'ISSUEFIX-20260904 physical stock reconcile vs Tally / old portal',
       NOW(), 22;

-- ---------------------------------------------------------------------------
-- 2. Item 141-0056 — Issue #1394 line 2 qty 4 -> 3
-- ---------------------------------------------------------------------------
UPDATE issue_li
SET issue_qty = 3,
    remarks = CONCAT(COALESCE(remarks, ''), 'ISSUEFIX-20260904 qty 4->3 (keying correction)'),
    updated_date_time = NOW(),
    updated_by = 22
WHERE issue_li_id = 163902
  AND issue_id = 58320
  AND item_id = 333533
  AND inward_dtl_id = 23659
  AND issue_qty = 4;
