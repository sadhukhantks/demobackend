-- Rollback for sls_co27_phantom_jute_mr_28062.sql (tag CO27PHANTOMMR-20260822).
-- Restores the MR status and the line's active flag from the repair ledger, then
-- marks the ledger rows rolled_back. Ledger rows are the audit trail -- keep them.

UPDATE jute_mr jm JOIN jute_mr_status_repair r
  ON r.jute_mr_id = jm.jute_mr_id AND r.tag = 'CO27PHANTOMMR-20260822' AND r.status = 'applied'
SET jm.status_id = r.old_status_id;

UPDATE jute_mr_li l JOIN jute_mr_status_repair r
  ON r.jute_mr_li_id = l.jute_mr_li_id AND r.tag = 'CO27PHANTOMMR-20260822' AND r.status = 'applied'
SET l.active = r.old_li_active;

UPDATE jute_mr_status_repair
SET status = 'rolled_back', rolled_back_at = NOW()
WHERE tag = 'CO27PHANTOMMR-20260822' AND status = 'applied';
