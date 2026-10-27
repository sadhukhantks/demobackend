-- TKT-165 (TKT-000165): Hands Report on sls shows no targets for machine-based occupations.
-- Tenant sls, co_id 2, branch 29. Reported 14-09-2026 by slsuser.
--
-- Cause (data): tools/seed_hands_norm_from_empmill12.py seeded 242 norms on sls and left
-- machine_type_id NULL on all of them ("configured later by SQL", never done). 50 of those
-- norms carry a per-machine rate (hands_per_machine, e.g. '2 MC = 1 HAND' -> 0.5). With no
-- machine type the report cannot count the machines that ran, so it has no target for them.
-- (The code fix in src/juteProduction/hands_report.py now shows those targets as blank with a
-- note. Before that fix they showed 0 and every hand on them counted as excess.)
--
-- SCOPE OF THIS SCRIPT: only the 9 norms whose sls machine type holds exactly the machines
-- the norm talks about (finishing / sack sewing). The other 41 are NOT linked here because the
-- sls machine types are too coarse for them. For example, type 7 'Loom' holds all 334 looms:
-- Hessian 200, S4 114, Sacking 12, Rapier 4, Leno 4. WEAVER(H) '2 MC = 1 HAND' linked to it
-- would be targeted off every loom that ran. Beaming (hessian/S4/sacking), spinning (fine/coarse),
-- spreader vs inter-spreader, tow teaser/breaker, breaker/finisher cards, bale press and lapping
-- (no machine-keyed entries at all, so the target would be 0) all need an owner decision first.
-- See the TKT-165 triage report.
--
-- CAVEAT: a linked target is only as good as the machine-keyed production entries. On
-- 10-Sep-2026 only 1 hemming and 0 herackle machine-shifts were recorded against ~6 and ~10
-- hands present. Those days will show a large excess until finishing entries carry machine_id.
--
-- Sized 2026-09-30 with SELECT only: the REPAIR WHERE matches 9 rows. No triggers on the table.
-- sls is SELECT-only for the agent: the owner must approve and run this. NOT EXECUTED.

-- === VERIFY (run first, confirm current state before applying) ===
SELECT n.hands_norm_id, n.designation_id, d.desig, n.variable_fixed, n.hands_per_machine,
       n.machine_type_id, n.norms_text, n.effective_date, n.active
FROM jute_prod_hands_norms_mst n
JOIN designation_mst d ON d.designation_id = n.designation_id
WHERE n.hands_norm_id IN (19, 20, 176, 177, 200, 202, 203, 204, 205)
ORDER BY n.hands_norm_id;
-- expect 9 rows, all co 2 / branch 29, active 1, machine_type_id NULL:
--   19 CALENDER FEEDER 1.0 | 20 CALENDER RECEIVER 1.0 | 176 DAMPING FEEDER 1.0 | 177 DAMPING RECEIVER 1.0
--   200 CUTTING FEEDER/RECEIVER 2.0 | 202 HEMMING OPERATOR 1.0 | 203 HEMMING HELPER 0.6667
--   204 HERACLE OPERATOR 1.0 | 205 HERACLE HELPER 0.6667
SELECT COUNT(*) AS will_update
FROM jute_prod_hands_norms_mst
WHERE co_id = 2 AND branch_id = 29 AND active = 1
  AND machine_type_id IS NULL AND hands_per_machine IS NOT NULL
  AND hands_norm_id IN (19, 20, 176, 177, 200, 202, 203, 204, 205);
-- expect 9
SELECT machine_type_id, machine_type_name FROM machine_type_mst WHERE machine_type_id IN (18, 19, 24, 25, 26);
-- expect 18 Damping, 19 Calendering, 24 Cutting, 25 Hemming, 26 Herackle

-- === REPAIR ===
-- variable_fixed also goes F -> V. The report itself only needs machine_type_id +
-- hands_per_machine, but the TKT-202 Hands Norms master drops the machine link when a row
-- flagged 'F' is saved. Left as 'F', the first edit in that page would undo this repair.
UPDATE jute_prod_hands_norms_mst SET machine_type_id = 19, variable_fixed = 'V'
 WHERE hands_norm_id IN (19, 20) AND co_id = 2 AND branch_id = 29 AND machine_type_id IS NULL;     -- Calendering, expect 2
UPDATE jute_prod_hands_norms_mst SET machine_type_id = 18, variable_fixed = 'V'
 WHERE hands_norm_id IN (176, 177) AND co_id = 2 AND branch_id = 29 AND machine_type_id IS NULL;   -- Damping, expect 2
UPDATE jute_prod_hands_norms_mst SET machine_type_id = 24, variable_fixed = 'V'
 WHERE hands_norm_id = 200 AND co_id = 2 AND branch_id = 29 AND machine_type_id IS NULL;           -- Cutting, expect 1
UPDATE jute_prod_hands_norms_mst SET machine_type_id = 25, variable_fixed = 'V'
 WHERE hands_norm_id IN (202, 203) AND co_id = 2 AND branch_id = 29 AND machine_type_id IS NULL;   -- Hemming, expect 2
UPDATE jute_prod_hands_norms_mst SET machine_type_id = 26, variable_fixed = 'V'
 WHERE hands_norm_id IN (204, 205) AND co_id = 2 AND branch_id = 29 AND machine_type_id IS NULL;   -- Herackle, expect 2

-- === ROLLBACK (uncomment if needed) ===
-- All 9 rows were variable_fixed = 'F' before the repair (verified 2026-09-30).
-- UPDATE jute_prod_hands_norms_mst SET machine_type_id = NULL, variable_fixed = 'F'
--  WHERE hands_norm_id IN (19, 20, 176, 177, 200, 202, 203, 204, 205) AND co_id = 2 AND branch_id = 29;
-- expect 9
