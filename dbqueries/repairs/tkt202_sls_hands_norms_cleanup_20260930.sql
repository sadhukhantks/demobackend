-- TKT-202 (TKT-000202): Hands Norms master on sls flags duplicate norms; two norms disagree
-- with their own norms_text. Tenant sls, co_id 2, branch 29. Found by the TKT-202 triage.
--
-- Cause (data): tools/seed_hands_norm_from_empmill12.py copied the legacy
-- EMPMILL12.OCCUPATION_MASTER_NORMS rows verbatim (SHIFT_A..G and NORMS text alike) into
-- jute_prod_hands_norms_mst, all with effective_date 2000-01-01. sls holds exactly that seed:
-- 242 rows, ids 1..242, all co 2 / branch 29, all active. The legacy table carried two ACTIVE
-- rows for five designations, and in a number of rows the free-text NORMS does not describe the
-- SHIFT_* columns. Both defects were copied as-is.
--
-- How the app treats them:
--   * The Hands Report (src/juteProduction/hands_report.py, _norm_sort_key) takes, per
--     designation, the active row with the latest effective_date and, on a tie, the HIGHEST
--     hands_norm_id. Fixed ('F') norms take their targets ONLY from shift_a..shift_g;
--     norms_text is display-only.
--   * The TKT-202 Hands Norms page (src/juteProduction/hands_norms.py, mark_in_force) flags
--     active rows sharing designation + branch + effective_date as "duplicate" and refuses to
--     save another one (get_duplicate_norm_query). Inactive rows are ignored by both the flag
--     and the report, and stay visible in the page with include_inactive=1.
--
-- SCOPE OF THIS SCRIPT:
--   1. Set active = 0 on the OLDER (lower id) row of each of the 5 duplicate pairs. The report
--      already uses the newer row, so its targets do not change; the page stops flagging.
--   3. hands_norm_id 109 SHIFT ELECTRICIAL: norms_text 'A=1' but every shift column is 0 (the
--      only active row on sls with a shift-count text and all-zero columns). Set shift_a = 1.
--      This is the one change that alters a report target (0 -> 1 in shift A).
--   2. hands_norm_id 52 SPINNING OILER (norms_text 'A=1,B=1', stored A=1 B=0 C=1) is NOT
--      repaired here -- see OWNER TO DECIDE (a). It is the older half of pair 52/78 and is
--      deactivated by step 1; its newer twin 78 says 'A=1, C=1' with columns 1/0/1, and every
--      other department oiler (Breaker 45, Winding 56 'A=1,C=1', Drawing 63) is also 1/0/1.
--      So the columns look right and the text looks wrong. Changing the columns to match the
--      text was requested by the triage; it is inert once 52 is inactive, but it would put a
--      value into the row that the sibling data contradicts, so it is left for the owner.
--   4. A scan of all 242 active rows found 36 whose shift-count text disagrees with the stored
--      columns (parser covers 'A=n[,B=n[,C=n]]', 'n PER SHIFT', 'n / SHIFT', 'n IN EACH SHIFT',
--      'n IN A[+B]', 'A+B=n, C=n'). 109 is repaired (step 3); 163/164 are consistent under the
--      "A+B = total" reading; 52 and the remaining 32 are listed under OWNER TO DECIDE, because
--      for each of them it is not knowable from the data which side is wrong, and applying the
--      text would change live report targets for 29 designations.
--
-- Sized 2026-09-30 with SELECT only: the REPAIR WHERE clauses match 5 + 1 = 6 rows.
-- No triggers on the table (SHOW TRIGGERS LIKE 'jute_prod_hands_norms_mst' is empty).
-- sls is SELECT-only for the agent: the owner must approve and run this. NOT EXECUTED.

-- === VERIFY (run first, confirm current state before applying) ===

-- V1: the five duplicate pairs (both rows of each; the LOWER id is deactivated in step 1)
SELECT n.hands_norm_id, n.designation_id, d.desig, n.shift_a, n.shift_b, n.shift_c, n.shift_g,
       n.norms_text, n.effective_date, n.active
FROM jute_prod_hands_norms_mst n
JOIN designation_mst d ON d.designation_id = n.designation_id
WHERE n.hands_norm_id IN (17, 199, 47, 74, 48, 75, 49, 76, 52, 78)
ORDER BY n.designation_id, n.hands_norm_id;
-- expect 10 rows, all co 2 / branch 29, effective_date 2000-01-01, active 1 (A/B/C/G | text):
--   desig 190 OILER                  17: 3/0/0/0 'A = 3'         <- deactivate
--                                   199: 1/1/1/0 '1 PER SHIFT'   <- survives (report uses this today)
--   desig 556 SPINNING VICE FITTER   47: 1/0/0/0 'A=1'           <- deactivate (exact copy of 74)
--                                    74: 1/0/0/0 'A=1'           <- survives
--   desig 561 SPINNING ROLL GRINDER  48: 1/0/0/0 'A=1'           <- deactivate (exact copy of 75)
--                                    75: 1/0/0/0 'A=1'           <- survives
--   desig 562 SPINNING LINE FITTER   49: 2/2/1/0 'A=2,B=2,C=1'   <- deactivate (same values as 76)
--                                    76: 2/2/1/0 'A=2,B=2, C=1'  <- survives
--   desig 564 SPINNING OILER         52: 1/0/1/0 'A=1,B=1'       <- deactivate (text disagrees with its columns)
--                                    78: 1/0/1/0 'A=1, C=1'      <- survives (text matches columns)
-- Only the OILER pair changes the norm in substance (17 says 3 in A, 199 says 1 per shift);
-- the report has been using 199 since the seed, and step 1 keeps it that way.

-- V2: the two text-vs-column rows named by the triage
SELECT n.hands_norm_id, n.designation_id, d.desig, n.variable_fixed, n.shift_a, n.shift_b, n.shift_c, n.shift_g,
       n.machine_type_id, n.hands_per_machine, n.norms_text, n.active
FROM jute_prod_hands_norms_mst n
JOIN designation_mst d ON d.designation_id = n.designation_id
WHERE n.hands_norm_id IN (52, 109)
ORDER BY n.hands_norm_id;
-- expect 2 rows, both co 2 / branch 29, 'F', machine_type_id NULL, hands_per_machine NULL, active 1:
--    52 desig 564 SPINNING OILER    A=1.00 B=0.00 C=1.00 G=0.00  'A=1,B=1'   (not repaired, see OWNER TO DECIDE a)
--   109 desig 596 SHIFT ELECTRICIAL A=0.00 B=0.00 C=0.00 G=0.00  'A=1'       (step 3 sets A=1)

-- V3: size the repair with the exact REPAIR WHERE clauses
SELECT COUNT(*) AS will_deactivate
FROM jute_prod_hands_norms_mst
WHERE hands_norm_id IN (17, 47, 48, 49, 52) AND co_id = 2 AND branch_id = 29 AND active = 1;
-- expect 5
SELECT COUNT(*) AS will_set_shift_a
FROM jute_prod_hands_norms_mst
WHERE hands_norm_id = 109 AND co_id = 2 AND branch_id = 29
  AND shift_a = 0 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;
-- expect 1

-- V4: the survivors must be active (the report keeps using them after step 1)
SELECT COUNT(*) AS survivors_active
FROM jute_prod_hands_norms_mst
WHERE hands_norm_id IN (199, 74, 75, 76, 78) AND co_id = 2 AND branch_id = 29 AND active = 1;
-- expect 5

-- V5: duplicate groups as the Hands Norms page sees them (same key as mark_in_force)
SELECT n.designation_id, d.desig, n.effective_date, COUNT(*) AS active_rows,
       GROUP_CONCAT(n.hands_norm_id ORDER BY n.hands_norm_id) AS ids
FROM jute_prod_hands_norms_mst n
JOIN designation_mst d ON d.designation_id = n.designation_id
WHERE n.co_id = 2 AND n.branch_id = 29 AND n.active = 1
GROUP BY n.designation_id, d.desig, n.effective_date
HAVING COUNT(*) > 1
ORDER BY n.designation_id;
-- expect 5 groups before the repair: 190 (17,199) | 556 (47,74) | 561 (48,75) | 562 (49,76) | 564 (52,78)

-- === REPAIR ===

-- 1. Retire the older row of each duplicate pair. Guard active = 1 makes a re-run a no-op.
UPDATE jute_prod_hands_norms_mst SET active = 0
 WHERE hands_norm_id IN (17, 47, 48, 49, 52) AND co_id = 2 AND branch_id = 29 AND active = 1;   -- expect 5

-- 2. (hands_norm_id 52 shift columns) -- intentionally NOT applied. See OWNER TO DECIDE (a).

-- 3. SHIFT ELECTRICIAL: text 'A=1', columns all zero. Guard on all four zeros makes a re-run a no-op.
UPDATE jute_prod_hands_norms_mst SET shift_a = 1
 WHERE hands_norm_id = 109 AND co_id = 2 AND branch_id = 29
   AND shift_a = 0 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;                             -- expect 1

-- === POST-CHECK ===
-- Re-run V5: expect 0 groups.
-- Re-run V3: expect 0 and 0 (both guards now false).
SELECT hands_norm_id, active FROM jute_prod_hands_norms_mst
WHERE hands_norm_id IN (17, 47, 48, 49, 52) AND co_id = 2 AND branch_id = 29;
-- expect 5 rows, all active 0
SELECT hands_norm_id, shift_a, shift_b, shift_c, shift_g FROM jute_prod_hands_norms_mst
WHERE hands_norm_id = 109 AND co_id = 2 AND branch_id = 29;
-- expect 109: 1.00 / 0.00 / 0.00 / 0.00

-- === ROLLBACK (uncomment if needed) ===
-- All 5 rows were active = 1 before the repair (verified 2026-09-30, V1).
-- UPDATE jute_prod_hands_norms_mst SET active = 1
--  WHERE hands_norm_id IN (17, 47, 48, 49, 52) AND co_id = 2 AND branch_id = 29 AND active = 0;
-- expect 5
-- Row 109 was shift_a 0.00 / shift_b 0.00 / shift_c 0.00 / shift_g 0.00 before the repair (verified 2026-09-30, V2).
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 0
--  WHERE hands_norm_id = 109 AND co_id = 2 AND branch_id = 29
--    AND shift_a = 1 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;
-- expect 1

-- === OWNER TO DECIDE (all commented out; nothing below runs) ===
-- Every row here is active, co 2 / branch 29, variable_fixed 'F', machine_type_id NULL, hands_per_machine NULL,
-- so the report's target for the designation is exactly shift_a..shift_g. For each row two fixes are offered:
--   Option C  rewrite norms_text from the stored columns  -> display only, NO change to any report target.
--   Option T  rewrite the shift columns from the text     -> CHANGES the report target for that designation.
-- Each statement is guarded on the current value observed on 2026-09-30, so it is a no-op if the row has moved on.
-- Verify the block with:
--   SELECT n.hands_norm_id, d.desig, n.shift_a, n.shift_b, n.shift_c, n.shift_g, n.norms_text, n.active
--   FROM jute_prod_hands_norms_mst n JOIN designation_mst d ON d.designation_id = n.designation_id
--   WHERE n.hands_norm_id IN (52, 60,61,62,63,65,66,67,68,69,70,79,80,81,82,84,85,87,94,95,96,97,104,110,111,112,
--                             44,45,55,102, 191,207,215) AND n.co_id = 2 AND n.branch_id = 29
--   ORDER BY n.hands_norm_id;
--   -- expect 33 rows matching the tables below

-- (a) hands_norm_id 52 SPINNING OILER -- text 'A=1,B=1', stored A=1 B=0 C=1 G=0. Requested by the triage as
--     "columns <- text" (Option T). Retired by step 1 either way, so the report is unaffected by this choice;
--     it only matters if the owner ever re-activates 52 in place of 78. The sibling 78 ('A=1, C=1', 1/0/1) and
--     the other department oilers (45 Breaker 1/0/1, 56 Winding 'A=1,C=1' 1/0/1, 63 Drawing 1/0/1) all say
--     A + C, which makes the TEXT of 52 the likely typo. If the owner believes SPINNING OILER really is A + B,
--     the clean fix is Option T on 52 plus swapping the active flags of 52 and 78, or simply editing 78 in the
--     Hands Norms page.
-- Option T (as requested by the triage; rollback = SET shift_a = 1, shift_b = 0, shift_c = 1):
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 1, shift_c = 0
--  WHERE hands_norm_id = 52 AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 1;  -- expect 1
-- Option C (rollback = SET norms_text = 'A=1,B=1'):
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,C=1'
--  WHERE hands_norm_id = 52 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1,B=1';                      -- expect 1

-- (b) 25 rows whose text is a single 'A=n' but whose columns hold different, varied, plausible-looking values
--     (e.g. 61 DRAWING LINE FITTER 2/3/3, 65 HESSIAN LOOM FITTER 2/2/2). They are contiguous in the seed
--     (ids 60-112, the maintenance / general / electrical block), which suggests 'A=1' was typed as a
--     placeholder in the legacy NORMS column while the SHIFT_* columns were maintained. The report has used
--     the column values since the seed. Applying the text (Option T) would cut these designations' targets
--     to 1 hand in shift A only. Option C is the safe default if the owner confirms the columns.
--      id  designation                        text     stored A/B/C/G
--      60  DRAWING REPAIR FITTER              'A=1'    3/0/0/0
--      61  DRAWING LINE FITTER                'A=1'    2/3/3/0
--      62  DRAWING  CARRIAGE FITTER           'A=1'    2/0/0/0
--      63  DRAWING  OILER                     'A=1'    1/0/1/0
--      65  HESSIAN LOOM FITTER (N)            'A=1'    2/2/2/0
--      66  HESSIAN BREAK DOWN FITTER (N)      'A=1'    2/0/0/0
--      67  HESSIAN GENERAL VICE FITTER (N)    'A=1'    1/1/0/0
--      68  CARPENTER FOR HESSIAN LOOM (N)     'A=1'    3/1/1/0
--      69  HESSIAN MAZDOORS (N)               'A=1'    2/0/0/0
--      70  HESSIAN LOOM OILER (N)             'A=1'    2/0/0/0
--      79  SACKING LOOM FITTER                'A=1'    1/1/1/0
--      80  SACKING LOADER FITTER              'A=1'    1/1/1/0
--      81  S4 LOOM FITTER                     'A=1'    1/1/1/0
--      82  S4 MAJOR MAINTENANCE               'A=1'    2/0/0/0
--      84  FINISHING FITTER                   'A=1'    2/1/0/0
--      85  SWING FITTER                       'A=1'    3/2/0/0
--      87  GENERAL TURNER                     'A=1'    3/0/0/0
--      94  GENERAL WORKSHOP MAZDOOR           'A=1'    2/0/0/0
--      95  GENERAL CARPENTER (JOINER)         'A=1'    3/0/0/0
--      96  PINBOY (N)                         'A=1'    4/0/0/0
--      97  GENERAL VICE FITTER/  PLUMBER      'A=1'    2/1/0/0
--     104  MASON (GENERAL)                    'A=3'    2/0/0/0
--     110  ASST ELECTRICIAL                   'A=1'    1/1/1/0
--     111  ELECTICAL FITTER                   'A=1'    2/1/1/0
--     112  ELECTRICAL HELPER                  'A=1'    3/0/0/0
-- Option C (text <- columns; rollback = set norms_text back to the value in the guard):
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3'         WHERE hands_norm_id = 60  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- DRAWING REPAIR FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2,B=3,C=3' WHERE hands_norm_id = 61  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- DRAWING LINE FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 62  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- DRAWING  CARRIAGE FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,C=1'     WHERE hands_norm_id = 63  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- DRAWING  OILER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2,B=2,C=2' WHERE hands_norm_id = 65  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- HESSIAN LOOM FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 66  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- HESSIAN BREAK DOWN FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,B=1'     WHERE hands_norm_id = 67  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- HESSIAN GENERAL VICE FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3,B=1,C=1' WHERE hands_norm_id = 68  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- CARPENTER FOR HESSIAN LOOM (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 69  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- HESSIAN MAZDOORS (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 70  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- HESSIAN LOOM OILER (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,B=1,C=1' WHERE hands_norm_id = 79  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- SACKING LOOM FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,B=1,C=1' WHERE hands_norm_id = 80  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- SACKING LOADER FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,B=1,C=1' WHERE hands_norm_id = 81  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- S4 LOOM FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 82  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- S4 MAJOR MAINTENANCE
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2,B=1'     WHERE hands_norm_id = 84  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- FINISHING FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3,B=2'     WHERE hands_norm_id = 85  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- SWING FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3'         WHERE hands_norm_id = 87  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- GENERAL TURNER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 94  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- GENERAL WORKSHOP MAZDOOR
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3'         WHERE hands_norm_id = 95  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- GENERAL CARPENTER (JOINER)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=4'         WHERE hands_norm_id = 96  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- PINBOY (N)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2,B=1'     WHERE hands_norm_id = 97  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- GENERAL VICE FITTER/  PLUMBER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2'         WHERE hands_norm_id = 104 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=3';  -- MASON (GENERAL)
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,B=1,C=1' WHERE hands_norm_id = 110 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- ASST ELECTRICIAL
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=2,B=1,C=1' WHERE hands_norm_id = 111 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- ELECTICAL FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3'         WHERE hands_norm_id = 112 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=1';  -- ELECTRICAL HELPER
-- Option T (columns <- text; CHANGES report targets; rollback = set the columns back to the values in the guard):
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 60  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- DRAWING REPAIR FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0, shift_c = 0 WHERE hands_norm_id = 61  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 3 AND shift_c = 3 AND shift_g = 0;  -- DRAWING LINE FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 62  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- DRAWING  CARRIAGE FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_c = 0                         WHERE hands_norm_id = 63  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 1 AND shift_g = 0;  -- DRAWING  OILER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0, shift_c = 0 WHERE hands_norm_id = 65  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 2 AND shift_c = 2 AND shift_g = 0;  -- HESSIAN LOOM FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 66  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- HESSIAN BREAK DOWN FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 0                         WHERE hands_norm_id = 67  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 1 AND shift_c = 0 AND shift_g = 0;  -- HESSIAN GENERAL VICE FITTER (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0, shift_c = 0 WHERE hands_norm_id = 68  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- CARPENTER FOR HESSIAN LOOM (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 69  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- HESSIAN MAZDOORS (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 70  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- HESSIAN LOOM OILER (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 0, shift_c = 0            WHERE hands_norm_id = 79  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- SACKING LOOM FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 0, shift_c = 0            WHERE hands_norm_id = 80  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- SACKING LOADER FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 0, shift_c = 0            WHERE hands_norm_id = 81  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- S4 LOOM FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 82  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- S4 MAJOR MAINTENANCE
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0            WHERE hands_norm_id = 84  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 1 AND shift_c = 0 AND shift_g = 0;  -- FINISHING FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0            WHERE hands_norm_id = 85  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 2 AND shift_c = 0 AND shift_g = 0;  -- SWING FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 87  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- GENERAL TURNER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 94  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- GENERAL WORKSHOP MAZDOOR
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 95  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- GENERAL CARPENTER (JOINER)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 96  AND co_id = 2 AND branch_id = 29 AND shift_a = 4 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- PINBOY (N)
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0            WHERE hands_norm_id = 97  AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 1 AND shift_c = 0 AND shift_g = 0;  -- GENERAL VICE FITTER/  PLUMBER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 3                         WHERE hands_norm_id = 104 AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- MASON (GENERAL)
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 0, shift_c = 0            WHERE hands_norm_id = 110 AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- ASST ELECTRICIAL
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1, shift_b = 0, shift_c = 0 WHERE hands_norm_id = 111 AND co_id = 2 AND branch_id = 29 AND shift_a = 2 AND shift_b = 1 AND shift_c = 1 AND shift_g = 0;  -- ELECTICAL FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 1                         WHERE hands_norm_id = 112 AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- ELECTRICAL HELPER

-- (c) 4 rows with a multi-shift text that differs from the columns in one or two shifts. 45 BREAKER OILER carries
--     the same text as 44 BREAKER LINE FITTER ('A=3 , B=3 C=1HANDS'), which looks copy-pasted; its columns 1/0/1
--     match the other department oilers, so for 45 Option C is the likely fix. The other three are a coin toss.
--      id  designation                        text                    stored A/B/C/G
--      44  BREAKER LINE FITTER                'A=3 , B=3 C=1HANDS'    3/3/2/0   (text C=1, column C=2)
--      45  BREAKER OILER                      'A=3 , B=3 C=1HANDS'    1/0/1/0   (text is 44's)
--      55  WINDING FITTER /CLEANING/BMG       'A=2,B=2,C=2'           3/2/2/0   (text A=2, column A=3)
--     102  GENERAL MAZDOOR                    'A=4, B=4'              5/2/0/0   (text 4/4, columns 5/2)
-- Option C (text <- columns):
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3,B=3,C=2' WHERE hands_norm_id = 44  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=3 , B=3 C=1HANDS';  -- BREAKER LINE FITTER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=1,C=1'     WHERE hands_norm_id = 45  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=3 , B=3 C=1HANDS';  -- BREAKER OILER
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=3,B=2,C=2' WHERE hands_norm_id = 55  AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=2,B=2,C=2';         -- WINDING FITTER /CLEANING/BMG
-- UPDATE jute_prod_hands_norms_mst SET norms_text = 'A=5,B=2'     WHERE hands_norm_id = 102 AND co_id = 2 AND branch_id = 29 AND norms_text = 'A=4, B=4';            -- GENERAL MAZDOOR
-- Option T (columns <- text; CHANGES report targets):
-- UPDATE jute_prod_hands_norms_mst SET shift_c = 1             WHERE hands_norm_id = 44  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 3 AND shift_c = 2 AND shift_g = 0;  -- BREAKER LINE FITTER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 3, shift_b = 3 WHERE hands_norm_id = 45  AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 1 AND shift_g = 0;  -- BREAKER OILER
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 2             WHERE hands_norm_id = 55  AND co_id = 2 AND branch_id = 29 AND shift_a = 3 AND shift_b = 2 AND shift_c = 2 AND shift_g = 0;  -- WINDING FITTER /CLEANING/BMG
-- UPDATE jute_prod_hands_norms_mst SET shift_a = 4, shift_b = 4 WHERE hands_norm_id = 102 AND co_id = 2 AND branch_id = 29 AND shift_a = 5 AND shift_b = 2 AND shift_c = 0 AND shift_g = 0;  -- GENERAL MAZDOOR

-- (d) 3 rows with text '1 IN A+B' and columns 1/0/0/0. The text can be read as "one hand in A and one in B"
--     (-> shift_b should be 1) or "one hand covering A and B" (-> columns are right and only shift A is
--     targeted). No other '... IN A+B' row exists on sls to calibrate against. If the first reading is right:
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 1 WHERE hands_norm_id = 191 AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- BALE ARRANGING CLERK
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 1 WHERE hands_norm_id = 207 AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- DEPT MAZDOOR+HOOPS CUTTER
-- UPDATE jute_prod_hands_norms_mst SET shift_b = 1 WHERE hands_norm_id = 215 AND co_id = 2 AND branch_id = 29 AND shift_a = 1 AND shift_b = 0 AND shift_c = 0 AND shift_g = 0;  -- CRAIN MAN
--     If the second reading is right, nothing to do (optionally Option C: norms_text = '1 IN A').

-- (e) For the record, not a defect: 163 COOLIE RELIEVER (F) 'A+B=2, C=2 HANDS' is stored 1/1/2 and
--     164 SPINNER RELIVER F/S 'A+B=4, C=2 HANDS' is stored 2/2/2 -- consistent if "A+B=n" means n in total
--     across the two shifts. Left alone.
