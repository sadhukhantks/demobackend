-- =============================================================================
-- Data fix: close migrated jute POs dated before 2026-07-01
-- =============================================================================
-- Date: 2026-09-25
-- TARGET: sls, branch_id 29 (EJM factory) ONLY -- owner's instruction 2026-09-25.
-- Why: every jute PO dated before 2026-07-01 came in through the legacy-data
-- migration. Their receipts are not linked back to the PO (jute_mr.po_id), so
-- they show as outstanding in the Outstanding PO Position report although the
-- material was received long ago. The owner asked for them to be CLOSED (5).
--
-- Scope: live POs only (not draft 2/21, rejected 4, cancelled 6, or already
-- closed 5) with po_date < 2026-07-01. Status 17 (PENDING APPROVAL LEVEL 2, 4
-- POs on sls branch 4) is left alone at the owner's request, and only branch 29 is
-- touched. Line items are left alone -- the
-- app's own closed POs keep their line status too. updated_by/updated_date_time
-- are not touched so the migration audit stays intact, and the backup table is the
-- audit trail of this change.
;
CREATE TABLE IF NOT EXISTS jute_po_status_backup_20260925 (
    jute_po_id     INT NOT NULL PRIMARY KEY,
    old_status_id  INT NULL,
    po_date        DATE NULL,
    branch_id      INT NULL,
    backed_up_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

;
INSERT IGNORE INTO jute_po_status_backup_20260925 (jute_po_id, old_status_id, po_date, branch_id)
SELECT jute_po_id, status_id, po_date, branch_id
FROM jute_po
WHERE status_id NOT IN (2, 4, 6, 21, 5, 17)
  AND po_date < '2026-07-01'
  AND branch_id = 29;

;
UPDATE jute_po
SET status_id = 5
WHERE status_id NOT IN (2, 4, 6, 21, 5, 17)
  AND po_date < '2026-07-01'
  AND branch_id = 29;

-- VERIFICATION
;
SELECT COUNT(*) AS backed_up FROM jute_po_status_backup_20260925;

;
SELECT COUNT(*) AS still_live_before_cutoff
FROM jute_po
WHERE status_id NOT IN (2, 4, 6, 21, 5, 17) AND po_date < '2026-07-01' AND branch_id = 29;

-- =============================================================================
-- ROLLBACK:
-- UPDATE jute_po jp JOIN jute_po_status_backup_20260925 b ON b.jute_po_id = jp.jute_po_id
--    SET jp.status_id = b.old_status_id;
-- DROP TABLE jute_po_status_backup_20260925;
-- =============================================================================
;
