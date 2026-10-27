CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._gstfix20260811_alias ( /* CONSENT GATE. Ships EMPTY on purpose, so a first run is a guaranteed no-op.
     Rows here are the ONLY authority this script has to infer a supplier
     branch state. Populate it by hand, after human review, then re-run.
     Rationale: sls state_mst stores ANDHRA PRADESH (state_id 29) under the
     pre-bifurcation GST code 28, and has NO row for code 37 (the current AP
     code). Every affected sls supplier branch carries gst_no = 37. Matching
     gst_no to state_mst.state_code therefore resolves to NOTHING, and the
     37 -> 29 equivalence is a human judgement, not a derivable fact.
     To approve it, run exactly this and nothing wider:
       INSERT INTO vowsls_to_sls_transfer._gstfix20260811_alias (gst_state_code, state_id, approved_by_note)
       VALUES (37, 29, 'AP post-2014 GST code 37 = state_mst ANDHRA PRADESH id 29') */
    gst_state_code   INT          NOT NULL,
    state_id         INT          NOT NULL,
    approved_by_note VARCHAR(255) NULL,
    PRIMARY KEY (gst_state_code),
    FOREIGN KEY (state_id) REFERENCES state_mst(state_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._gstfix20260811_scope ( /* the worklist: approved-SR lines that are taxable but carry no active proc_gst row */
    inward_dtl_id INT    NOT NULL,
    inward_id     INT    NOT NULL,
    branch_id     INT    NULL,
    line_amount   DOUBLE NULL,
    tax_pct       DOUBLE NULL,
    updated_by    INT    NULL,
    PRIMARY KEY (inward_dtl_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DELETE FROM vowsls_to_sls_transfer._gstfix20260811_scope;

INSERT INTO vowsls_to_sls_transfer._gstfix20260811_scope (inward_dtl_id, inward_id, branch_id, line_amount, tax_pct, updated_by)
SELECT pid.inward_dtl_id,
       pi.inward_id,
       pi.branch_id,
       COALESCE(pid.amount, pid.approved_qty * pid.accepted_rate), /* save_sr writes GST on the PRE-discount line amount (sr.py line_amount = line.amount). Discount is netted only into the header. Do NOT subtract discount_amount here. */
       CASE WHEN pid.po_dtl_id IS NOT NULL THEN COALESCE(pg.tax_pct, 0)
            ELSE COALESCE(im.tax_percentage, 0) END, /* mirrors get_inward_line_tax_pct_query */
       COALESCE(pi.updated_by, 1)
FROM proc_inward pi
JOIN proc_inward_dtl pid ON pid.inward_id = pi.inward_id AND pid.active = 1
LEFT JOIN po_gst pg  ON pg.po_dtl_id = pid.po_dtl_id AND pg.active = 1
LEFT JOIN item_mst im ON im.item_id = pid.item_id
WHERE pi.sr_status = 3
  AND pi.branch_id = 87 /* branch 87 / co 106 ONLY. Branches 4 and 29 are deliberately excluded: every affected line there is vowsls-migrated legacy data whose SOURCE system also had no GST row (staging_proc_gst has zero matches), spanning FY2019-20..FY2023-24. Writing tax onto filed periods would fabricate liability. */
  AND NOT EXISTS (SELECT 1 FROM vowsls_to_sls_transfer._map_inward_dtls m WHERE m.new_inward_dtl_id = pid.inward_dtl_id) /* native rows only, same reason */
  AND NOT EXISTS (SELECT 1 FROM proc_gst g WHERE g.proc_inward_dtl = pid.inward_dtl_id AND g.active = 1)
  AND (CASE WHEN pid.po_dtl_id IS NOT NULL THEN COALESCE(pg.tax_pct, 0)
            ELSE COALESCE(im.tax_percentage, 0) END) > 0;

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.proc_gst_bak_20260811 AS /* pre-image of any proc_gst row touching a scope line, so a re-run is reversible */
SELECT g.* FROM proc_gst g
JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_dtl_id = g.proc_inward_dtl
WHERE 1 = 0;

INSERT INTO vowsls_to_sls_transfer.proc_gst_bak_20260811
SELECT g.* FROM proc_gst g
JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_dtl_id = g.proc_inward_dtl
WHERE NOT EXISTS (SELECT 1 FROM vowsls_to_sls_transfer.proc_gst_bak_20260811 b WHERE b.gst_invoice_type = g.gst_invoice_type);

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.proc_inward_supplier_branch_bak_20260811 ( /* pre-image of every supplier_branch_id this script stamps */
    inward_id INT NOT NULL,
    old_supplier_branch_id INT NULL,
    backed_up_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (inward_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO vowsls_to_sls_transfer.proc_inward_supplier_branch_bak_20260811 (inward_id, old_supplier_branch_id)
SELECT DISTINCT pi.inward_id, pi.supplier_branch_id
FROM proc_inward pi
JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_id = pi.inward_id
WHERE pi.supplier_branch_id IS NULL
  AND EXISTS (SELECT 1 FROM party_branch_mst pb
              WHERE pb.party_id = pi.supplier_id AND pb.active = 1
              GROUP BY pb.party_id HAVING COUNT(*) = 1);

CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer.party_branch_state_bak_20260811 ( /* pre-image of every party_branch_mst.state_id this script stamps */
    party_mst_branch_id INT NOT NULL,
    old_state_id INT NULL,
    gst_no VARCHAR(255) NULL,
    backed_up_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (party_mst_branch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO vowsls_to_sls_transfer.party_branch_state_bak_20260811 (party_mst_branch_id, old_state_id, gst_no)
SELECT DISTINCT pb.party_mst_branch_id, pb.state_id, pb.gst_no
FROM party_branch_mst pb
JOIN vowsls_to_sls_transfer._gstfix20260811_alias a ON a.gst_state_code = CAST(LEFT(TRIM(pb.gst_no), 2) AS UNSIGNED)
JOIN proc_inward pi ON pi.supplier_id = pb.party_id
JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_id = pi.inward_id
WHERE pb.active = 1
  AND pb.state_id IS NULL
  AND pb.gst_no REGEXP '^[0-9]{2}';

UPDATE party_branch_mst pb /* STEP A. No-op unless vowsls_to_sls_transfer._gstfix20260811_alias was populated. Scoped to supplier branches actually referenced by the worklist -- never a blanket master rewrite. */
JOIN vowsls_to_sls_transfer._gstfix20260811_alias a ON a.gst_state_code = CAST(LEFT(TRIM(pb.gst_no), 2) AS UNSIGNED)
SET pb.state_id = a.state_id
WHERE pb.active = 1
  AND pb.state_id IS NULL
  AND pb.gst_no REGEXP '^[0-9]{2}'
  AND pb.party_mst_branch_id IN (SELECT party_mst_branch_id FROM vowsls_to_sls_transfer.party_branch_state_bak_20260811);

UPDATE proc_inward pi /* STEP B. Stamp supplier_branch_id ONLY where the supplier has exactly one active branch. Anything else stays NULL for the user to resolve on the SR screen. */
JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_id = pi.inward_id
SET pi.supplier_branch_id = (SELECT MAX(pb.party_mst_branch_id) FROM party_branch_mst pb
                             WHERE pb.party_id = pi.supplier_id AND pb.active = 1)
WHERE pi.supplier_branch_id IS NULL
  AND pi.inward_id IN (SELECT inward_id FROM vowsls_to_sls_transfer.proc_inward_supplier_branch_bak_20260811);

INSERT INTO proc_gst ( /* STEP C. Same column set and rounding as insert_proc_gst() + calculate_gst_amounts(). Lines whose states still do not resolve simply do not join, and are reported by vowsls_to_sls_transfer.vw_gstfix20260811_leftover. */
    proc_inward_dtl, proc_inward_additional_id, tax_pct, stax_percentage,
    s_tax_amount, i_tax_amount, i_tax_percentage,
    c_tax_amount, c_tax_percentage, tax_amount, active, updated_by)
SELECT s.inward_dtl_id,
       NULL,
       s.tax_pct,
       CASE WHEN pb.state_id = bm.state_id THEN s.tax_pct ELSE 0 END,
       CASE WHEN pb.state_id = bm.state_id THEN ROUND(s.line_amount * (s.tax_pct / 2) / 100, 2) ELSE 0 END,
       CASE WHEN pb.state_id = bm.state_id THEN 0 ELSE ROUND(s.line_amount * s.tax_pct / 100, 2) END,
       CASE WHEN pb.state_id = bm.state_id THEN 0 ELSE s.tax_pct END,
       CASE WHEN pb.state_id = bm.state_id THEN ROUND(s.line_amount * (s.tax_pct / 2) / 100, 2) ELSE 0 END,
       CASE WHEN pb.state_id = bm.state_id THEN s.tax_pct / 2 ELSE 0 END,
       CASE WHEN pb.state_id = bm.state_id
            THEN ROUND(2 * ROUND(s.line_amount * (s.tax_pct / 2) / 100, 2), 2)
            ELSE ROUND(s.line_amount * s.tax_pct / 100, 2) END,
       1,
       s.updated_by
FROM vowsls_to_sls_transfer._gstfix20260811_scope s
JOIN proc_inward pi ON pi.inward_id = s.inward_id
JOIN party_branch_mst pb ON pb.party_mst_branch_id = pi.supplier_branch_id
JOIN branch_mst bm ON bm.branch_id = COALESCE(pi.ship_branch_id, pi.bill_branch_id, pi.branch_id)
WHERE pb.state_id IS NOT NULL
  AND bm.state_id IS NOT NULL
  AND s.line_amount IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM proc_gst g WHERE g.proc_inward_dtl = s.inward_dtl_id AND g.active = 1);

CREATE OR REPLACE VIEW vowsls_to_sls_transfer.vw_gstfix20260811_leftover AS /* rows this script refused to guess. Hand this list to the user. */
SELECT s.inward_id, s.inward_dtl_id, pi.sr_no, pi.sr_date, pi.supplier_id, pm.supp_name,
       pi.supplier_branch_id, pb.state_id AS supplier_state_id, pb.gst_no AS supplier_gst_no,
       bm.state_id AS shipping_state_id, s.line_amount, s.tax_pct,
       ROUND(s.line_amount * s.tax_pct / 100, 2) AS tax_not_posted,
       (SELECT COUNT(*) FROM party_branch_mst x WHERE x.party_id = pi.supplier_id AND x.active = 1) AS supplier_active_branches,
       CASE WHEN pi.supplier_branch_id IS NULL
              THEN 'Open the Stores Receipt and pick the supplier branch in Supplier State (GST), then save'
            WHEN pb.state_id IS NULL
              THEN 'Set State on this supplier branch under Masters > Party Master, then re-save the Stores Receipt'
            WHEN bm.state_id IS NULL
              THEN 'Set State on the receiving branch under Admin dashboard > Branch Management'
            ELSE 'Unclassified -- investigate' END AS user_action
FROM vowsls_to_sls_transfer._gstfix20260811_scope s
JOIN proc_inward pi ON pi.inward_id = s.inward_id
LEFT JOIN party_mst pm ON pm.party_id = pi.supplier_id
LEFT JOIN party_branch_mst pb ON pb.party_mst_branch_id = pi.supplier_branch_id
LEFT JOIN branch_mst bm ON bm.branch_id = COALESCE(pi.ship_branch_id, pi.bill_branch_id, pi.branch_id)
WHERE NOT EXISTS (SELECT 1 FROM proc_gst g WHERE g.proc_inward_dtl = s.inward_dtl_id AND g.active = 1);

-- Migration: 20260811_backfill_sr_gst_missing_supplier_branch.sql
-- Target DB: sls   Date: 2026-08-11   Scope: branch 87 / co 106, native (non-migrated) rows only
--
-- WHY
-- save_sr (src/procurement/sr.py) only writes proc_gst when BOTH the supplier state
-- and the shipping state resolve. Historically it skipped the whole GST block in
-- silence, minting zero-tax Stores Receipts and zero-tax manual DR/CR notes.
-- The guard now raises HTTP 400 (gst_context_error), so this repairs the rows that
-- were saved before that guard existed.
--
-- WHAT IT DELIBERATELY DOES NOT DO
-- 1. It never touches branches 4 and 29, nor any vowsls-migrated line. Those rows had
--    no GST in the SOURCE system either, so recomputing tax there would invent
--    liability on already-filed returns.
-- 2. It never guesses a supplier branch when the supplier has 0 or 2+ active branches.
-- 3. It never guesses a state from gst_no unless a human has seeded
--    vowsls_to_sls_transfer._gstfix20260811_alias. Shipped empty, so the first run changes nothing.
-- 4. It cannot restore GST on additional charges. proc_inward_additional stores no
--    tax_pct or apply_tax column, so those percentages exist only in the original
--    request payload and are unrecoverable. Re-save the SR to rebuild them.
-- 5. It does not repair DR/CR notes built from these SRs. Those are separate documents
--    already Approved and must be cancelled and re-raised by the user.
--
-- AFTER RUNNING, always read the leftovers:
--   SELECT * FROM vowsls_to_sls_transfer.vw_gstfix20260811_leftover ORDER BY inward_id
--
-- ROLLBACK
--   DELETE g FROM proc_gst g
--   JOIN vowsls_to_sls_transfer._gstfix20260811_scope s ON s.inward_dtl_id = g.proc_inward_dtl
--   WHERE NOT EXISTS (SELECT 1 FROM vowsls_to_sls_transfer.proc_gst_bak_20260811 b WHERE b.gst_invoice_type = g.gst_invoice_type)
--
--   UPDATE proc_inward pi
--   JOIN vowsls_to_sls_transfer.proc_inward_supplier_branch_bak_20260811 b ON b.inward_id = pi.inward_id
--   SET pi.supplier_branch_id = b.old_supplier_branch_id
--
--   UPDATE party_branch_mst pb
--   JOIN vowsls_to_sls_transfer.party_branch_state_bak_20260811 b ON b.party_mst_branch_id = pb.party_mst_branch_id
--   SET pb.state_id = b.old_state_id
--
--   DROP VIEW IF EXISTS vowsls_to_sls_transfer.vw_gstfix20260811_leftover
--   DROP TABLE IF EXISTS vowsls_to_sls_transfer._gstfix20260811_scope
--   DROP TABLE IF EXISTS vowsls_to_sls_transfer._gstfix20260811_alias
--
-- RUNNER NOTE: the project runner splits the file on the statement separator and then
-- skips any chunk beginning with a double dash. So every statement chunk here starts
-- with real SQL, and no comment anywhere in this file contains a statement separator
-- character. Keep it that way when editing, or statements will be silently dropped.
