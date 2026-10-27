-- TKT-000142 (3): "drawhead no. 4 missing from sqc sliver weight report"
-- Target: sls ONLY. DO NOT run automatically — sls is production (SELECT-only for agents).
--
-- Root cause: sls.machine_mst has DRAW HEAD MC NO 1,2,3,5..15 (machine_type 13 'Carding
-- Machine', dept 15 BATCHING(EJM), branch 29) but NO row for "DRAW HEAD MC NO 4" — the
-- legacy mech_code block 15030..15043 skips it entirely (NO 3 = 15032, NO 5 = 15033).
-- The Drawhead SQC machine picker (DRAWHEAD_MACHINE_TYPE_NAMES → get_machines_by_type_names_query)
-- correctly returns only existing machines, so no reading can ever be entered for
-- drawhead 4. This is a machine-master data gap, not a code bug.
--
-- mech_code '15044' is the next free code in the 150xx drawhead block (verified unused in
-- dept 15). If the mill has an official mech code for this machine, substitute it before running.
-- Alternative: the tenant can add the machine via the Machine Master page instead of SQL.

-- 1) VERIFY (before): expect 0 rows
SELECT machine_id, machine_name, mech_code
FROM machine_mst
WHERE dept_id = 15 AND machine_name = 'DRAW HEAD MC NO 4';

-- 2) REPAIR: insert the missing machine (mirrors DRAW HEAD MC NO 3 / NO 5, machine_id 1457/1458)
INSERT INTO machine_mst
    (dept_id, machine_name, machine_type_id, updated_by, remarks,
     active, mech_posting_code, mech_code, mech_shr_code, line_no)
VALUES
    (15, 'DRAW HEAD MC NO 4', 13, 1, 'TKT-000142: missing drawhead added',
     1, 1, '15044', NULL, '0');

-- 3) VERIFY (after): expect 1 row, active = 1, machine_type_id = 13
SELECT machine_id, machine_name, mech_code, machine_type_id, active
FROM machine_mst
WHERE dept_id = 15 AND machine_name = 'DRAW HEAD MC NO 4';

-- ROLLBACK:
-- DELETE FROM machine_mst WHERE dept_id = 15 AND machine_name = 'DRAW HEAD MC NO 4';
