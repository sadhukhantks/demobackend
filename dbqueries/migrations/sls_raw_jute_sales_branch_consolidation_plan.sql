-- ============================================================================
-- Raw-jute sales: consolidate the selling branch onto the receiving branch
-- STEP 1 of 3 — PLAN ONLY (creates the ledger, changes NO business data)
-- ============================================================================
-- Target DB   : sls   (production tenant)
-- Tag         : JUTESALESBR-20260822
-- Companion   : _apply.sql (step 2), _rollback.sql (step 3)
--
-- THE PROBLEM
-- Jute is received against jute_mr at a company's FACTORY branch but the raw-jute
-- sales invoice (invoice_type = 5) is raised from its HO branch. Every jute balance
-- report scopes both sides by branch, so the sale is deducted from a branch that
-- never held the stock:
--     br 16 (HO, co 81)   ->  55 rows, ALL negative, closing -160,202.7
--     br 25 (HO, co 65)   ->  65 rows, ALL negative, closing -192,488.9
--     br  9 (HO, co 67)   ->  55 rows, ALL negative, closing -158,016.7
-- and the matching FACTORY branch is overstated by the same quantity, because its
-- sales are never deducted at all (br 87 closing 1,110,233.1).
--
-- THE FIX
-- Move the raw-jute invoice onto the branch that actually held the jute. Source and
-- target always belong to the SAME co_id (asserted below), so this re-files a
-- document inside one company — it does not move value between companies.
--
-- SCOPE RULES (a remap is included only if ALL THREE hold)
--   1. the company has EXACTLY ONE jute_mr receiving branch -> no ambiguity
--   2. the invoicing branch is not already that branch
--   3. no invoice_no at the target branch collides in the same calendar year
-- Rule 3 matters because invoice_no has no unique index here and numbering is
-- MAX(invoice_no)+1 per (branch, FY) across all types
-- (src/sales/query.py get_max_invoice_no_for_branch_fy). Moving a colliding set
-- would mint duplicate document numbers, so those companies stay out.
--
-- EXPECTED PLAN (verified against sls 2026-08-22)
--   co   1: br   3 ->   4      37     co  91: br  28 ->  37   1,052
--   co  45: br  15 ->  34     183     co  92: br  44 -> 105       7
--   co  65: br  25 ->  30   1,639     co 106: br  23 ->  87     685
--   co  67: br   9 ->  35   1,441     co 127: br  46 -> 108     724
--   co  81: br  16 ->  36   1,475     co 128: br  49 ->  89      70
--   co  90: br  24 -> 104     952
--   TOTAL 11 remaps / 8,265 invoices
--
-- DELIBERATELY EXCLUDED (each needs a separate decision, none are moved here)
--   co  26  br  22 (871)   126 invoice_no collisions at br 32
--   co 147  br 100 (1,351)  10 invoice_no collisions at br 99
--   co  27  br  27 (1,022)  company has TWO jute branches (33 with 1,744 MRs, 27 with 1)
--   co 100  br  74 (47)     no jute_mr branch exists for this company
--   co 101  br  75 (150)    no jute_mr branch exists for this company
--
-- BLAST RADIUS (verified on sls, not assumed)
--   * sales_invoice is the ONLY table carrying the invoice's branch_id; every child
--     (sales_invoice_dtl / _gst / _jute / _additional / _freight) keys on invoice_id.
--   * no accounting voucher tables exist on sls, so nothing is posted against the
--     old branch.
--   * sales_delivery_order_id is NULL on all 17,634 raw-jute invoices, so no DO
--     linkage moves with them.
--   * only invoice_type = 5 moves; the HO branch keeps its other invoice types and
--     its numbering series.
--
-- SAFE TO RE-RUN: the plan only selects invoices still sitting on the old branch,
-- so after the apply step it inserts nothing.
-- ROLLBACK: this step creates a table and inserts rows. To undo, run
--   DELETE FROM vowsls_to_sls_transfer.jute_sales_branch_remap WHERE tag = 'JUTESALESBR-20260822';
-- ============================================================================

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.jute_sales_branch_remap (
  remap_id       BIGINT      NOT NULL AUTO_INCREMENT,
  tag            VARCHAR(40) NOT NULL COMMENT 'batch tag, rolls back as one set',
  co_id          INT         NOT NULL,
  invoice_id     BIGINT      NOT NULL,
  invoice_no     BIGINT      NULL COMMENT 'value at plan time, for reconciliation',
  invoice_date   DATE        NULL,
  invoice_type   INT         NULL,
  status_id      INT         NULL COMMENT 'invoice status at plan time',
  old_branch_id  BIGINT      NOT NULL COMMENT 'the branch that invoiced it',
  new_branch_id  BIGINT      NOT NULL COMMENT 'the branch that received the jute',
  reason         VARCHAR(255) NULL,
  status         ENUM('proposed','applied','rolled_back') NOT NULL DEFAULT 'proposed',
  applied_at     DATETIME    NULL,
  rolled_back_at DATETIME    NULL,
  created_at     TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (remap_id),
  UNIQUE KEY uq_tag_invoice (tag, invoice_id),
  KEY ix_invoice (invoice_id),
  KEY ix_tag_status (tag, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Audit + rollback map for the raw-jute selling-branch consolidation';

INSERT INTO vowsls_to_sls_transfer.jute_sales_branch_remap
  (tag, co_id, invoice_id, invoice_no, invoice_date, invoice_type, status_id,
   old_branch_id, new_branch_id, reason)
WITH jute_branches AS (
  SELECT b.co_id, jm.branch_id
  FROM jute_mr jm
  JOIN branch_mst b ON b.branch_id = jm.branch_id
  WHERE jm.status_id NOT IN (4, 6, 21)
  GROUP BY b.co_id, jm.branch_id
),
single_jute_branch AS (
  SELECT co_id, MIN(branch_id) AS dst
  FROM jute_branches
  GROUP BY co_id
  HAVING COUNT(*) = 1
),
candidate AS (
  SELECT b.co_id, si.branch_id AS old_branch_id, sjb.dst AS new_branch_id
  FROM sales_invoice si
  JOIN branch_mst b ON b.branch_id = si.branch_id
  JOIN single_jute_branch sjb ON sjb.co_id = b.co_id
  WHERE si.invoice_type = 5
    AND si.branch_id <> sjb.dst
  GROUP BY b.co_id, si.branch_id, sjb.dst
),
collision_free AS (
  SELECT c.*
  FROM candidate c
  WHERE NOT EXISTS (
    SELECT 1
    FROM sales_invoice src
    JOIN sales_invoice tgt
      ON tgt.branch_id = c.new_branch_id
     AND tgt.invoice_no = src.invoice_no
     AND YEAR(tgt.invoice_date) = YEAR(src.invoice_date)
    WHERE src.branch_id = c.old_branch_id
      AND src.invoice_type = 5
  )
)
SELECT
  'JUTESALESBR-20260822',
  cf.co_id,
  si.invoice_id,
  si.invoice_no,
  si.invoice_date,
  si.invoice_type,
  si.status_id,
  cf.old_branch_id,
  cf.new_branch_id,
  CONCAT('raw-jute sale invoiced from br ', cf.old_branch_id,
         ' but the jute was received at br ', cf.new_branch_id)
FROM collision_free cf
JOIN sales_invoice si
  ON si.branch_id = cf.old_branch_id
 AND si.invoice_type = 5;
