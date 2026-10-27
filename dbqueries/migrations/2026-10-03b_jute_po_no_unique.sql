-- =============================================================================
-- Jute PO numbering guard, step B: one PO number per branch and financial year
-- =============================================================================
-- Date: 2026-10-03
-- Code: src/juteProcurement/po_numbering.py (named lock + retry on 1062),
--       src/models/jute.py (JutePo.fy_start_year, uq_jute_po_branch_fy_no)
-- Pair: 2026-10-03a_jute_po_no_dedupe.sql must have run first on the same tenant --
--       ADD UNIQUE KEY fails with error 1062 while any duplicate exists, which is
--       the intended guard (nothing is changed by a failed ALTER).
--
-- jute_po has no financial-year column, so the key goes on a STORED generated
-- column: fy_start_year = the April-March FY that po_date falls in (2026 for
-- 01-Apr-2026 .. 31-Mar-2027), NULL when po_date is NULL. MySQL maintains it on
-- every INSERT/UPDATE of po_date and refuses explicit values (error 3105) -- the
-- ORM maps it with Computed() and never writes it. Rows with po_no NULL stay
-- allowed (NULLs never collide in a unique key).
--
-- Both ALTERs rebuild/index a small table (sls 3.9k rows, dev3 134) -- seconds.
-- Needed on EVERY tenant that saves jute POs: dev3, sls, and the others
-- (amcl, ...) -- confirm each target with the run-migration skill.
--
-- Runner: the pymysql runner splits this file on the semicolon character and
-- skips chunks that start with a comment marker, so comment blocks end with a
-- lone semicolon line and contain no semicolons themselves.
;
ALTER TABLE jute_po
  ADD COLUMN fy_start_year SMALLINT
    GENERATED ALWAYS AS (IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)) STORED
    AFTER po_date;

ALTER TABLE jute_po
  ADD UNIQUE KEY uq_jute_po_branch_fy_no (branch_id, fy_start_year, po_no);

-- VERIFICATION
;
SELECT COUNT(*) AS rows_total,
       SUM(fy_start_year IS NULL) AS fy_null_rows,
       SUM(fy_start_year = IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)) AS fy_matches
FROM jute_po;

SELECT index_name, seq_in_index, column_name, non_unique
FROM information_schema.statistics
WHERE table_schema = DATABASE() AND table_name = 'jute_po'
  AND index_name = 'uq_jute_po_branch_fy_no'
ORDER BY seq_in_index;

-- =============================================================================
-- ROLLBACK (run each statement separately, terminated with its own semicolon):
-- ALTER TABLE jute_po DROP INDEX uq_jute_po_branch_fy_no
-- ALTER TABLE jute_po DROP COLUMN fy_start_year
-- =============================================================================
;
