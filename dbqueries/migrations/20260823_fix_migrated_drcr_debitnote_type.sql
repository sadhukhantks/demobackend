-- Migration: 20260823_fix_migrated_drcr_debitnote_type.sql
-- Target DB: sls ONLY   Date: 2026-08-23   Scope: 349 migrated drcr_note_dtl rows
--
-- WHY
-- The vowsls -> sls transfer (step10 / mapping_notes §15b) copied
-- `debitnote_type` DIRECT because "old values already 1/2". The two enums are
-- INVERTED:
--     vowsls.tbl_proc_debit_credit_lineitem.debitnote_type
--         COLUMN COMMENT: '1-for ratechange, 2-for qtychange'
--     sls.drcr_note_dtl.debitnote_type  (src/procurement/drcr_note.py:62)
--         1 = quantity rejection, 2 = rate difference
-- Evidence: the ONE legacy row with type=1 (adjustment_detail_id 435) has
-- reason 'RATE DIFF'; of the 439 legacy type=2 rows, 437 carry rate = the
-- inward line rate and reasons like 'RECEIVED SMALL SIZE' / 'WRONG SUPPLY'
-- -- i.e. goods physically returned, not a price adjustment.
--
-- IMPACT OF THE BUG
-- Every migrated purchase return is labelled "Rate Difference" in the portal
-- DR/CR note screen, and the single true rate-difference note is labelled
-- "Quantity Rejection". Reported case: item 141-0056 on SRN
-- LC/FACTORY/24-25/000122 (br 87, drcr note 62, 20 pcs returned 2024-05-31).
-- The companion migration 20260823_drcr_return_reduces_stock.sql keys stock
-- reduction off debitnote_type = 1, so this flip MUST run FIRST on sls.
--
-- IDEMPOTENT: the backup table doubles as the "already flipped" guard -- on a
-- re-run no row still matches its pre-image, so the UPDATE affects 0 rows.
--
-- TENANT GUARD -- DO NOT REMOVE
-- `updated_by = 1` only means "migration row" on sls, where user_mst has NO
-- user_id 1. On dev3 user_id 1 is a REAL portal user (user1@empirejute.com)
-- whose 6 notes already carry the correct new-system semantics; flipping those
-- would corrupt them. Both statements below are therefore no-ops on any tenant
-- that has a user_mst row with user_id = 1.
--
-- ROLLBACK
--   UPDATE drcr_note_dtl d
--     JOIN vowsls_to_sls_transfer.drcr_note_dtl_type_bak_20260823 b
--       ON b.drcr_note_dtl_id = d.drcr_note_dtl_id
--      SET d.debitnote_type = b.debitnote_type;

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.drcr_note_dtl_type_bak_20260823 (
    drcr_note_dtl_id INT NOT NULL,
    debitnote_type   INT NULL,
    saved_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (drcr_note_dtl_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Pre-image of every migration-owned line (step10 stamps updated_by = 1).
INSERT IGNORE INTO vowsls_to_sls_transfer.drcr_note_dtl_type_bak_20260823
    (drcr_note_dtl_id, debitnote_type)
SELECT drcr_note_dtl_id, debitnote_type
FROM drcr_note_dtl
WHERE updated_by = 1
  AND NOT EXISTS (SELECT 1 FROM user_mst WHERE user_id = 1);

-- Flip 1 <-> 2, only on rows still holding their pre-image value.
UPDATE drcr_note_dtl d
  JOIN vowsls_to_sls_transfer.drcr_note_dtl_type_bak_20260823 b
    ON b.drcr_note_dtl_id = d.drcr_note_dtl_id
   SET d.debitnote_type = 3 - b.debitnote_type
 WHERE d.debitnote_type = b.debitnote_type
   AND b.debitnote_type IN (1, 2)
   AND NOT EXISTS (SELECT 1 FROM user_mst WHERE user_id = 1);
