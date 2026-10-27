-- ============================================================================
-- co 27 — retire phantom jute MR 28062 sitting on the HO branch
-- STATUS: PREPARED, NOT RUN. Needs sign-off (see WHY NOT A BRANCH MOVE).
-- ============================================================================
-- Target DB : sls   (production tenant)
-- Tag       : CO27PHANTOMMR-20260822
--
-- WHY NOT A BRANCH MOVE
-- This was assumed to be a mis-keyed branch (HO instead of FACTORY) on an otherwise
-- normal receipt. It is not. Line 45265 of MR 28062 carries actual_weight = 14,800
-- against actual_qty = 650, while branch 33 -- the company's real jute branch --
-- has 2,587 lines whose MAXIMUM is 221.90 and ZERO lines above 1,000. The value is
-- kilograms typed into a quintal field (14,800 kg = 148 qtl, which lands squarely in
-- br 33's normal range). Re-filing it to br 33 unchanged would inject 14,800 quintals
-- -- roughly 1,480 tonnes -- of stock that does not exist into a live factory.
--
-- CORROBORATING SIGNALS (all point to a test / abandoned entry, not a receipt)
--   * branch_mr_no = 1 and jute_gate_entry_no = 1: the first and only MR ever at HO
--   * created 2026-08-13 by user 1; br 33's real MRs stop at 2026-01-09
--   * out_date 2026-04-02 PREDATES jute_mr_date 2026-08-13
--   * actual_rate NULL, warehouse_id NULL, po_id NULL
--   * zero jute_issue rows drawn against its line -- nothing downstream depends on it
--
-- WHAT THIS DOES
-- Sets the MR to status 6 (Cancelled) and deactivates its line, which is how the
-- rest of the system already expresses "not stock" -- JUTE_MR_STOCK_EXCLUDED_STATUSES
-- is (4, 6, 21) and every balance CTE also requires mrli.active = 1. It does NOT
-- delete anything. Effect: br 27 stops reporting 14,800 of phantom A TD-7 (U), and
-- co 27 is left with exactly one jute branch (33), satisfying rule 1 of the
-- raw-jute selling-branch consolidation.
--
-- WHAT THIS DOES *NOT* DO
-- co 27's 1,022 raw-jute invoices still cannot move to br 33: 219 of them collide on
-- (invoice_no, year) with invoices already at br 33, which is rule 3 of that
-- consolidation. That is a separate decision, same as co 26 and co 147.
--
-- ROLLBACK: sls_co27_phantom_jute_mr_28062_rollback.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS jute_mr_status_repair (
  repair_id      BIGINT      NOT NULL AUTO_INCREMENT,
  tag            VARCHAR(40) NOT NULL,
  jute_mr_id     BIGINT      NOT NULL,
  jute_mr_li_id  BIGINT      NULL,
  old_status_id  INT         NULL,
  new_status_id  INT         NULL,
  old_li_active  INT         NULL,
  new_li_active  INT         NULL,
  reason         VARCHAR(500) NULL,
  status         ENUM('applied','rolled_back') NOT NULL DEFAULT 'applied',
  applied_at     DATETIME    NULL,
  rolled_back_at DATETIME    NULL,
  created_at     TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (repair_id),
  KEY ix_tag (tag),
  KEY ix_mr (jute_mr_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Audit + rollback map for jute MR status/line-active repairs';

INSERT INTO jute_mr_status_repair
  (tag, jute_mr_id, jute_mr_li_id, old_status_id, new_status_id, old_li_active, new_li_active, reason, applied_at)
SELECT 'CO27PHANTOMMR-20260822', jm.jute_mr_id, l.jute_mr_li_id, jm.status_id, 6, l.active, 0,
       'phantom entry: actual_weight 14800 vs br33 max 221.90, kg typed into a quintal field; branch_mr_no=1, no rate/warehouse/po, no issues drawn',
       NOW()
FROM jute_mr jm
JOIN jute_mr_li l ON l.jute_mr_id = jm.jute_mr_id
WHERE jm.jute_mr_id = 28062;

UPDATE jute_mr_li l JOIN jute_mr_status_repair r
  ON r.jute_mr_li_id = l.jute_mr_li_id AND r.tag = 'CO27PHANTOMMR-20260822' AND r.status = 'applied'
SET l.active = 0;

UPDATE jute_mr jm JOIN jute_mr_status_repair r
  ON r.jute_mr_id = jm.jute_mr_id AND r.tag = 'CO27PHANTOMMR-20260822' AND r.status = 'applied'
SET jm.status_id = 6;
