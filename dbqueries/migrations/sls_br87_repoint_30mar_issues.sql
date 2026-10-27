-- ============================================================================
-- sls branch 87 — re-point four 30-Mar-2026 jute issues onto sheet stock lines
-- ============================================================================
-- Target DB   : sls ONLY (Limelight Commercial Pvt. Ltd., co_id 106, branch 87)
-- Date        : 2026-08-23   User ruling: "if they are issued after 29th, then change
--               the issue to this newly made one instead of using the old one and zero
--               that" + "confirmed go ahead" on the targets below.
-- Run order   : sls_br87_date_invoice_15702.sql -> THIS -> sls_br87_stock_asof_20260329_adjust.sql
--
-- WHY
-- Four legacy MR lines are NOT on the user's 29-Mar-2026 stock sheet but each had one
-- approved issue dated 30-Mar-2026 drawing on it (migrated rows, updated_by '19').
-- The adjustment script zeroes those lines as on 29-Mar, so left alone the 30-Mar
-- issues would drive them negative. The ruling: the issues draw from sheet stock
-- instead. No sheet line carries the same quality (J TD-6 / A TD-7 (L)), so the
-- user chose the nearest grade with spare capacity; item_id on the issue row follows
-- the target line so every report reads the issue as that line's item. Date, qty,
-- weight, value, status, yarn type, unit are untouched.
--
--   jute_issue  from line (MR / quality)            -> to line (MR / quality)      qty / qtl
--   81781       43897 (27262 / 1645 J TD-6)         -> 44563 (27637 / 737 J TD-8)   14 / 18.34
--   81782       44331 (27504 / 1645 J TD-6)         -> 44563 (27637 / 737 J TD-8)    7 /  9.98
--   81777       44501 (27604 / 1053 A TD-7 (L))     -> 45014 (27938 / 1535 A TD-9 (U)) 23 / 32.25
--   81778       44975 (27918 / 1053 A TD-7 (L))     -> 45014 (27938 / 1535 A TD-9 (U)) 12 / 17.20
--   item_id: 1139 -> 1138 (J TD-8) for the first two; 1111 -> 1122 (A TD-9 (U)) for the last two.
--
-- After this + the adjustment: 43897/44331/44501/44975 read exactly 0; 44563 reads
-- 33 - 21 = 12 bales / 15.27 qtl today; 45014 reads 74 - 35 = 39 bales / 56.50 qtl.
--
-- GUARDED on the current (old) line id, so it is rerunnable and cannot move a row twice.
--
-- ROLLBACK (exact reversal; run only if the adjustment batch has been rolled back or
-- you accept the four old lines going negative again):
--   UPDATE jute_issue SET jute_mr_li_id = 43897, item_id = 1139 WHERE jute_issue_id = 81781 AND jute_mr_li_id = 44563;
--   UPDATE jute_issue SET jute_mr_li_id = 44331, item_id = 1139 WHERE jute_issue_id = 81782 AND jute_mr_li_id = 44563;
--   UPDATE jute_issue SET jute_mr_li_id = 44501, item_id = 1111 WHERE jute_issue_id = 81777 AND jute_mr_li_id = 45014;
--   UPDATE jute_issue SET jute_mr_li_id = 44975, item_id = 1111 WHERE jute_issue_id = 81778 AND jute_mr_li_id = 45014;
-- ============================================================================

UPDATE jute_issue
SET jute_mr_li_id = 44563, item_id = 1138
WHERE jute_issue_id = 81781 AND jute_mr_li_id = 43897 AND branch_id = 87 AND issue_date = '2026-03-30';

UPDATE jute_issue
SET jute_mr_li_id = 44563, item_id = 1138
WHERE jute_issue_id = 81782 AND jute_mr_li_id = 44331 AND branch_id = 87 AND issue_date = '2026-03-30';

UPDATE jute_issue
SET jute_mr_li_id = 45014, item_id = 1122
WHERE jute_issue_id = 81777 AND jute_mr_li_id = 44501 AND branch_id = 87 AND issue_date = '2026-03-30';

UPDATE jute_issue
SET jute_mr_li_id = 45014, item_id = 1122
WHERE jute_issue_id = 81778 AND jute_mr_li_id = 44975 AND branch_id = 87 AND issue_date = '2026-03-30';

-- Verification (expect 4 rows: 81781/81782 -> 44563 item 1138; 81777/81778 -> 45014 item 1122)
-- SELECT jute_issue_id, jute_mr_li_id, item_id, quantity, weight, issue_date FROM jute_issue
-- WHERE jute_issue_id IN (81777, 81778, 81781, 81782);
