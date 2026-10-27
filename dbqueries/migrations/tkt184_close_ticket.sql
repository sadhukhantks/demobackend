-- TKT-000184 — close the ticket after the sls data fix was applied.
-- Target DB: vowconsole3 (central console registry), ticket_id 184.
--
-- The data repair is DONE: sls.proc_po_dtl.po_dtl_id 31750 uom_id 132 (KGS) -> 143 (PCS),
-- committed 2026-09-19. This script only moves the ticket to its terminal state.
--
-- NOTE ON THE LIFECYCLE: support-ticket statuses are a dedicated enum, NOT the global
-- 21/1/20/3/4/6 approval ids (src/common/supportTicket/constants.py).
--   1 Raised  2 Open  3 In Progress  4 On Hold  5 Resolved  6 Closed  7 Rejected
-- TRANSITIONS forbids 'close' directly from Raised — it is only legal from
-- {Open, In Progress, Resolved} and requires a close_reason. Hence three hops
-- (open -> resolve -> close), mirroring POST /supportTicket/manage/transition and the
-- system comment that endpoint writes per step.
--
-- Every WHERE pins the expected current status, so re-running this is a 0-row no-op.
-- updated_by is left untouched: there is no Control Desk actor id for an agent
-- (test-credentials.md has the console login as TBD); provenance is in the comments.

-- 1) VERIFY BEFORE — expect status_id 1 (Raised), active 1
SELECT ticket_id, ticket_no, status_id, close_reason,
       resolved_date_time, closed_date_time, active
FROM support_ticket WHERE ticket_id = 184;

-- 2) open: Raised -> Open
UPDATE support_ticket SET status_id = 2, updated_date_time = NOW()
WHERE ticket_id = 184 AND status_id = 1 AND active = 1;

-- 3) resolve: Open -> Resolved (records resolved_date_time + resolution notes)
UPDATE support_ticket
SET status_id = 5, resolved_date_time = NOW(), updated_date_time = NOW(),
    resolution_notes = 'Data fix applied on tenant sls: PO no 123 dated 14.09.2026, first line (proc_po_dtl.po_dtl_id 31750, item 3700E PNEUMATIC CONNECTOR) unit corrected from KGS to PCS (uom_id 132 -> 143). Qty 4 and rate 128 unchanged, so the PO value (net 1098.20) and GST are unaffected, and nothing had been received against the line. Root cause was not an application defect: the item master UOM was changed KGS -> PCS on 17.09.2026, three days after this PO was raised, so the PO had faithfully recorded the only unit available at entry time.'
WHERE ticket_id = 184 AND status_id = 2 AND active = 1;

-- 4) close: Resolved -> Closed (close_reason mandatory; 'completed' is a valid CLOSE_REASON)
UPDATE support_ticket
SET status_id = 6, close_reason = 'completed',
    closed_date_time = NOW(), updated_date_time = NOW()
WHERE ticket_id = 184 AND status_id = 5 AND active = 1;

-- 5) The system comments the endpoint would have written, one per transition
INSERT INTO support_ticket_comment
    (ticket_id, author_type, author_user_id, author_name, comment_text, is_internal)
VALUES
    (184, 'system', NULL, 'System', 'AI Triage changed status to Open.', 0),
    (184, 'system', NULL, 'System', 'AI Triage changed status to Resolved.', 0),
    (184, 'system', NULL, 'System', 'AI Triage changed status to Closed - reason: completed.', 0);

-- 6) VERIFY AFTER — expect status_id 6, close_reason 'completed', both timestamps set
SELECT ticket_id, ticket_no, status_id, close_reason,
       resolved_date_time, closed_date_time, active
FROM support_ticket WHERE ticket_id = 184;

-- ---------- ROLLBACK (back to Raised) ----------
-- UPDATE support_ticket
-- SET status_id = 1, close_reason = NULL, resolved_date_time = NULL,
--     closed_date_time = NULL, resolution_notes = NULL, updated_date_time = NOW()
-- WHERE ticket_id = 184 AND status_id = 6;
-- DELETE FROM support_ticket_comment
-- WHERE ticket_id = 184 AND author_type = 'system' AND author_name = 'System';
