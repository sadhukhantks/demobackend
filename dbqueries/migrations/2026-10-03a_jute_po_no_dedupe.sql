-- =============================================================================
-- Jute PO numbering guard, step A: renumber historic duplicate PO numbers
-- =============================================================================
-- Date: 2026-10-03
-- Code: src/juteProcurement/po_numbering.py (lock protocol), src/models/jute.py
-- Pair: 2026-10-03b_jute_po_no_unique.sql (the unique key) -- run THIS file first,
--       the key cannot be created while duplicates exist.
--
-- po_no is unique per branch within the April-March financial year of po_date
-- (printed as co/branch/JPO/YY-YY/00000). Saves with no lock produced a few
-- duplicates. READ-ONLY check of 2026-10-03:
--   dev3: branch 12 FY 2025-26 po_no 1 twice (ids 13174 rejected, 13233 cancelled)
--   sls : branch 29 FY 2020-21 po_no 5 and 6 (ids 3352/5708, 3353/5709),
--         FY 2022-23 po_no 1 (ids 8431/11562), FY 2024-25 po_no 264
--         (ids 13138/13146, both dated 2025-03-31) -- all four pairs CLOSED (5)
--   other tenants: not checked -- the SELECT below lists them, the UPDATE fixes them
--
-- Rule: inside each duplicate group the LOWEST jute_po_id keeps the number, every
-- other row gets MAX(po_no) of its branch + FY, plus 1, 2, ... (ordered by
-- jute_po_id). Old and new numbers are kept in a backup table so the change can
-- be rolled back, and a sentence is appended to internal_note (visible on the PO
-- screen) so nobody wonders why a 2021 PO carries number 564.
-- Nothing else on the row changes (no updated_by / updated_date_time bump).
-- Idempotent once complete: a second run finds no duplicate groups and changes
-- nothing. (A re-run after a failure between the INSERT and the UPDATE stops on
-- the backup table's primary key -- finish by running the UPDATE by hand.)
--
-- Runner: the pymysql runner splits this file on the semicolon character and
-- skips chunks that start with a comment marker, so comment blocks end with a
-- lone semicolon line and contain no semicolons themselves.
;
CREATE TABLE IF NOT EXISTS jute_po_no_dedupe_backup_20261003 (
  jute_po_id    INT      NOT NULL PRIMARY KEY,
  branch_id     INT      NULL,
  fy_start_year SMALLINT NULL,
  old_po_no     INT      NULL,
  new_po_no     INT      NULL,
  changed_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Plan: which rows get which new number (nothing is changed yet)
;
INSERT INTO jute_po_no_dedupe_backup_20261003
    (jute_po_id, branch_id, fy_start_year, old_po_no, new_po_no)
SELECT d.jute_po_id, d.branch_id, d.fy_start_year, d.po_no,
       m.max_no + ROW_NUMBER() OVER (PARTITION BY d.branch_id, d.fy_start_year
                                     ORDER BY d.jute_po_id)
FROM (
    SELECT p.jute_po_id, p.branch_id, p.po_no,
           IF(MONTH(p.po_date) >= 4, YEAR(p.po_date), YEAR(p.po_date) - 1) AS fy_start_year
    FROM jute_po p
    WHERE p.po_no IS NOT NULL AND p.po_date IS NOT NULL AND p.branch_id IS NOT NULL
) d
JOIN (
    SELECT x.branch_id, x.fy_start_year, x.po_no, MIN(x.jute_po_id) AS keep_id
    FROM (
        SELECT jute_po_id, branch_id, po_no,
               IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1) AS fy_start_year
        FROM jute_po
        WHERE po_no IS NOT NULL AND po_date IS NOT NULL AND branch_id IS NOT NULL
    ) x
    GROUP BY x.branch_id, x.fy_start_year, x.po_no
    HAVING COUNT(*) > 1
) g ON g.branch_id = d.branch_id
   AND g.fy_start_year = d.fy_start_year
   AND g.po_no = d.po_no
   AND d.jute_po_id <> g.keep_id
JOIN (
    SELECT branch_id,
           IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1) AS fy_start_year,
           MAX(po_no) AS max_no
    FROM jute_po
    WHERE po_date IS NOT NULL
    GROUP BY branch_id, IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)
) m ON m.branch_id = d.branch_id AND m.fy_start_year = d.fy_start_year;

-- Apply: only rows still carrying their old (duplicate) number are touched
;
UPDATE jute_po p
JOIN jute_po_no_dedupe_backup_20261003 b ON b.jute_po_id = p.jute_po_id
SET p.po_no = b.new_po_no,
    p.internal_note = LEFT(CONCAT_WS(' | ', NULLIF(p.internal_note, ''),
        CONCAT('PO no ', b.old_po_no, ' was a duplicate, renumbered to ',
               b.new_po_no, ' on 2026-10-03')), 500)
WHERE p.po_no = b.old_po_no;

-- VERIFICATION
;
SELECT b.jute_po_id, b.branch_id, b.fy_start_year, b.old_po_no, b.new_po_no,
       p.po_no AS current_po_no, p.status_id, p.po_date
FROM jute_po_no_dedupe_backup_20261003 b
JOIN jute_po p ON p.jute_po_id = b.jute_po_id
ORDER BY b.branch_id, b.fy_start_year, b.old_po_no;

SELECT COUNT(*) AS remaining_duplicate_groups
FROM (
    SELECT 1
    FROM jute_po
    WHERE po_no IS NOT NULL AND po_date IS NOT NULL AND branch_id IS NOT NULL
    GROUP BY branch_id, IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1), po_no
    HAVING COUNT(*) > 1
) t;

-- =============================================================================
-- ROLLBACK (run each statement separately, terminated with its own semicolon):
-- UPDATE jute_po p
--   JOIN jute_po_no_dedupe_backup_20261003 b ON b.jute_po_id = p.jute_po_id
--   SET p.po_no = b.old_po_no
--   WHERE p.po_no = b.new_po_no
-- (internal_note keeps the renumber sentence -- strip it by hand if wanted)
-- DROP TABLE jute_po_no_dedupe_backup_20261003
-- =============================================================================
;
