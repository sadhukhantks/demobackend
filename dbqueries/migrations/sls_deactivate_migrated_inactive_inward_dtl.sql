-- =============================================================================
-- sls ONLY — repair the vowsls import leak of soft-deleted SR lines.
--
-- Ticket (branch 87): "items showing stock in new software agst SR NO.434
-- dt.6.8.25 (130-0152 / 130-0152N / 130-1921) and 120-1922 (SR NO.5 / 15.3.24);
-- actual position in old software NIL."
--
-- Cause
--   steps/step9_transfer_transactions/10_transfer_inward_detail.sql selected
--   FROM vowsls.tbl_proc_inward_detail with NO `is_active` filter and wrote a
--   constant `1 AS active`. vowsls soft-deleted SR lines (is_active = 0 — lines
--   the user deleted and re-entered corrected) landed in sls as live stock.
--   Proof on the ticket rows: vowsls tbl_proc_inward.total_amaount for SR 434 is
--   7127.00 = (only the is_active=1 lines, 6040) x 1.18, while sls
--   proc_inward.net_amount is 10301.00 = (all 8 imported lines, 8730) x 1.18.
--
-- Scope (verified, item-checked against vowsls_to_sls_transfer._map_items so a stale map cannot widen it)
--   212 proc_inward_dtl rows still active=1 whose vowsls source is is_active<>1:
--     branch 87 -> 153,  branch 29 -> 36,  branch 4 -> 23
--   172 of them still carry balance_qty > 0 in vw_approved_inward_qty.
--   45 issue_li rows (194.8 qty) were already issued against 20 of them.
--
-- What this script does — and deliberately does NOT do
--   DOES  : record every affected row in backup tables, repoint already-issued
--           lines onto their real sibling line, then set active = 0 on the 212
--           rows (and on the matching staging rows, so a re-promote can't
--           reintroduce them).
--   NOT   : touch proc_gst, proc_inward.net_amount / sr_value, or any bill pass.
--           Those totals are inflated by the same rows, but the SR-total and
--           bill-pass aggregates in src/procurement/query.py do not filter
--           pid.active, so deactivating alone leaves them exactly as they are
--           today — no regression, no silent rewrite of amounts on the 78
--           affected headers (38 bill passes OPEN, 36 PENDING APPROVAL LEVEL 2,
--           3 REJECTED). Correcting those values is a separate, explicit call.
--
--   Requires fix_vw_approved_inward_qty_active_filter.sql — without the view's
--   `pid.active = 1` guard, deactivating these rows does not remove the stock.
--
--   This is a targeted repair. It does NOT re-run any step of the transfer.
--
-- ROLLBACK (exact, in this order):
--   UPDATE issue_li il
--     JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_issue_20260813 b ON b.issue_li_id = il.issue_li_id
--      SET il.inward_dtl_id = b.old_inward_dtl_id;
--   UPDATE proc_inward_dtl pid
--     JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 b ON b.inward_dtl_id = pid.inward_dtl_id
--      SET pid.active = b.prev_active;
--   UPDATE vowsls_to_sls_transfer.staging_proc_inward_dtl s
--     JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 b
--       ON b.old_inward_detail_id = s.old_inward_detail_id
--      SET s.active = 1;
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Materialise the affected set (idempotent: rebuilt from scratch each run).
--    The vowsls_to_sls_transfer._map_items join asserts map validity — the mapped old item must equal
--    the item actually sitting on the prod line, so a stale vowsls_to_sls_transfer._map_inward_dtls
--    entry can never pull an unrelated row into the set.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813;
CREATE TABLE vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 (
    inward_dtl_id        INT         NOT NULL PRIMARY KEY,
    inward_id            INT         NOT NULL,
    item_id              INT         NULL,
    branch_id            INT         NULL,
    old_inward_detail_id BIGINT      NOT NULL,
    prev_active          TINYINT(1)  NOT NULL,
    sibling_dtl_id       INT         NULL,
    KEY ix_inward (inward_id),
    KEY ix_old (old_inward_detail_id)
) ENGINE=InnoDB;

INSERT INTO vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813
    (inward_dtl_id, inward_id, item_id, branch_id, old_inward_detail_id, prev_active, sibling_dtl_id)
SELECT
    pid.inward_dtl_id,
    pid.inward_id,
    pid.item_id,
    pi.branch_id,
    m.old_inward_detail_id,
    pid.active,
    NULL
FROM vowsls_to_sls_transfer._map_inward_dtls m
JOIN vowsls.tbl_proc_inward_detail d
  ON d.indent_details_id = m.old_inward_detail_id
JOIN proc_inward_dtl pid
  ON pid.inward_dtl_id = m.new_inward_dtl_id
JOIN vowsls_to_sls_transfer._map_items mi
  ON mi.old_item_id = d.item
 AND mi.new_item_id = pid.item_id
JOIN proc_inward pi
  ON pi.inward_id = pid.inward_id
WHERE COALESCE(d.is_active, 1) <> 1
  AND pid.active = 1;

-- ---------------------------------------------------------------------------
-- 2. Resolve the surviving sibling line for each affected row.
--    A sibling = the still-live line on the SAME inward for the SAME item, i.e.
--    the corrected re-entry the old system kept. Only an UNAMBIGUOUS sibling
--    (exactly one) is used; anything else stays NULL and is left unlinked.
--    Built as its own table because MySQL cannot select from the table being
--    updated inside the same statement (error 1093).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS vowsls_to_sls_transfer._fix_inactive_inward_dtl_sib_20260813;
CREATE TABLE vowsls_to_sls_transfer._fix_inactive_inward_dtl_sib_20260813 (
    inward_id      INT NOT NULL,
    item_id        INT NULL,
    sibling_dtl_id INT NOT NULL,
    n_siblings     INT NOT NULL,
    KEY ix_key (inward_id, item_id)
) ENGINE=InnoDB;

INSERT INTO vowsls_to_sls_transfer._fix_inactive_inward_dtl_sib_20260813 (inward_id, item_id, sibling_dtl_id, n_siblings)
SELECT pid.inward_id, pid.item_id, MIN(pid.inward_dtl_id), COUNT(*)
FROM proc_inward_dtl pid
WHERE pid.active = 1
  AND pid.inward_dtl_id NOT IN (SELECT inward_dtl_id FROM vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813)
  AND pid.inward_id IN (SELECT inward_id FROM vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813)
GROUP BY pid.inward_id, pid.item_id;

UPDATE vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 f
JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_sib_20260813 s
  ON s.inward_id = f.inward_id
 AND s.item_id  <=> f.item_id
 AND s.n_siblings = 1
SET f.sibling_dtl_id = s.sibling_dtl_id;

-- ---------------------------------------------------------------------------
-- 3. Repoint issues already booked against an affected line onto its sibling.
--    The issue physically happened; only the lot it was attributed to is wrong.
--    Rows with no unambiguous sibling keep pointing at the (now inactive) line —
--    the view drops it either way, and the issue history row stays intact.
--    NOTE: some siblings are already fully issued, so this pushes their
--    balance_qty negative. That is the real historical over-issue surfacing, and
--    create-issue hides it via its `balance_qty > 0` filter.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS vowsls_to_sls_transfer._fix_inactive_inward_dtl_issue_20260813;
CREATE TABLE vowsls_to_sls_transfer._fix_inactive_inward_dtl_issue_20260813 (
    issue_li_id       INT NOT NULL PRIMARY KEY,
    old_inward_dtl_id INT NOT NULL,
    new_inward_dtl_id INT NOT NULL
) ENGINE=InnoDB;

INSERT INTO vowsls_to_sls_transfer._fix_inactive_inward_dtl_issue_20260813 (issue_li_id, old_inward_dtl_id, new_inward_dtl_id)
SELECT il.issue_li_id, il.inward_dtl_id, f.sibling_dtl_id
FROM issue_li il
JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 f
  ON f.inward_dtl_id = il.inward_dtl_id
WHERE f.sibling_dtl_id IS NOT NULL;

UPDATE issue_li il
JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_issue_20260813 b
  ON b.issue_li_id = il.issue_li_id
SET il.inward_dtl_id = b.new_inward_dtl_id;

-- ---------------------------------------------------------------------------
-- 4. Deactivate the affected lines. Soft delete only — the rows stay readable
--    so the issue_li rows that still reference them do not dangle.
-- ---------------------------------------------------------------------------
UPDATE proc_inward_dtl pid
JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 f
  ON f.inward_dtl_id = pid.inward_dtl_id
SET pid.active            = 0,
    pid.updated_by        = 1,
    pid.updated_date_time = NOW();

-- ---------------------------------------------------------------------------
-- 5. Patch staging identically, so a future re-promote cannot reintroduce them.
-- ---------------------------------------------------------------------------
UPDATE vowsls_to_sls_transfer.staging_proc_inward_dtl s
JOIN vowsls_to_sls_transfer._fix_inactive_inward_dtl_20260813 f
  ON f.old_inward_detail_id = s.old_inward_detail_id
SET s.active = 0;
