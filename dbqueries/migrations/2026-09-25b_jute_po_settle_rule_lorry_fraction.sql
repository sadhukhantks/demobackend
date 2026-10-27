-- =============================================================================
-- Jute PO settlement -- revision 2 follow-up for tenants that already ran
-- 2026-09-25_jute_po_settlement.sql in its first shape (dev3)
-- =============================================================================
-- Date: 2026-09-25
-- Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md (section 11.1, 11.3)
-- Target: dev3. Tenants not yet migrated run the rewritten base script instead.
--
-- Tolerance is now a fraction of ONE lorry (po_weight / vehicle_quantity),
-- default 0.50 = half a lorry, instead of a percentage of the PO weight:
-- ADD tolerance_lorry_fraction, DROP weight_tolerance_pct. Also makes sure
-- status_mst has row 48 RETURNED (dev3 has it, the guard makes this a no-op).
--
-- AFTER running this, re-evaluate every branch so the new tolerance takes
-- effect: POST /api/jutePO/recompute_settlement?co_id=&branch_id= or
-- scripts/recompute_po_settlement.py --db <db> --co <co> --branch <br>
--
-- Runner: the pymysql runner splits this file on the semicolon character and
-- skips chunks that start with a comment marker, so comment blocks end with a
-- lone semicolon line and contain no semicolons themselves.
;
ALTER TABLE jute_po_settle_rule
  ADD COLUMN tolerance_lorry_fraction DECIMAL(4,2) NOT NULL DEFAULT 0.50 AFTER lorry_rule_enabled;

ALTER TABLE jute_po_settle_rule
  DROP COLUMN weight_tolerance_pct;

INSERT INTO status_mst (status_id, status_name)
SELECT 48, 'RETURNED' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM status_mst WHERE status_id = 48);

-- VERIFICATION
;
SELECT COUNT(*) AS has_new_column FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'jute_po_settle_rule' AND column_name = 'tolerance_lorry_fraction';

SELECT status_id, status_name FROM status_mst WHERE status_id = 48;

-- =============================================================================
-- ROLLBACK:
-- ALTER TABLE jute_po_settle_rule ADD COLUMN weight_tolerance_pct DECIMAL(5,2) NOT NULL DEFAULT 5.00 AFTER lorry_rule_enabled
-- ALTER TABLE jute_po_settle_rule DROP COLUMN tolerance_lorry_fraction
-- (the status_mst 48 row is left in place -- return_mr needs it)
-- (run each rollback statement separately, terminated with its own semicolon)
-- =============================================================================
;
