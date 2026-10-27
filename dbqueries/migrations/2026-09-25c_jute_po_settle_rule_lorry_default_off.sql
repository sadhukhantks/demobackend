-- =============================================================================
-- Jute PO settlement -- lorry-count rule OFF by default
-- =============================================================================
-- Date: 2026-09-25 (owner decision while reviewing the sls branch-29 dry run)
-- A lorry arriving does not settle a PO by itself. The weight test decides:
--   settled when received weight >= PO weight - tolerance_lorry_fraction x one lorry.
-- Companies can still switch the lorry rule on per company in the Settlement rules dialog.
-- Target: dev3 and sls (both already migrated with DEFAULT 1). Fresh tenants get
-- DEFAULT 0 from the rewritten base script.
;
ALTER TABLE jute_po_settle_rule ALTER COLUMN lorry_rule_enabled SET DEFAULT 0;

-- Existing per-company rows keep whatever a user saved (none exist as of this date).
-- VERIFICATION
;
SELECT COLUMN_NAME, COLUMN_DEFAULT FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jute_po_settle_rule' AND COLUMN_NAME = 'lorry_rule_enabled';

-- ROLLBACK: ALTER TABLE jute_po_settle_rule ALTER COLUMN lorry_rule_enabled SET DEFAULT 1
;
