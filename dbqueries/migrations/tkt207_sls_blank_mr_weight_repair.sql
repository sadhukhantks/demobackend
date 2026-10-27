-- TKT-000207 — sls (reporter: co 2 EJM, branch 29 FACTORY): "MR weight not displaying for
-- some MRs" on Jute Purchase > MR (/dashboardportal/jutePurchase/mr).
--
-- Cause (code + data): jute_mr.mr_weight, the header figure the MR list and its Excel show,
-- was written only by MR save. An MR that went Gate IN -> QC -> Gate OUT -> Pending/Approved
-- without a Save kept NULL (or 0), while its lines hold the real weight. 22 sls rows today:
-- 8 on co 2 / branch 29 (28370, 28371, 28374, 28376, 28378, 28388, 28389, 28473) and
-- 14 on co 106 LCPL / branch 87 (Open MRs of 2026-09-28..10-01).
--
-- The code fix already makes the list show the right figure WITHOUT this file (the list
-- falls back to the line sum), and new MRs now get the header filled at gate-out / Pending /
-- Approve. This script only brings the 22 stored headers in line, so anything else that reads
-- jute_mr.mr_weight directly (the juteTransfer app copies it onto transfer hops) is right too.
--
-- Same rule as the code: mr_weight = ROUND(SUM(COALESCE(accepted_weight, actual_weight, 0)))
-- over active lines; only blank (NULL/0) headers whose lines carry weight are touched, so a
-- stored non-blank weight is never overwritten. Safe to re-run (idempotent).
--
-- DO NOT RUN BLIND. sls is production: the owner runs this. dev3 may be checked first.
-- (dev3 also has blank headers, eg 27893 / 27882 / 27825 — same statement works there.)
--
-- APPLIED 2026-10-02 (owner-approved): sls 22 rows, dev3 6 rows (27779, 27825, 27882, 27883,
-- 27893, 27938 — all 0.0 before). Re-check afterwards: 0 rows left on both. Re-running is a no-op.
--
-- ---------------------------------------------------------------------------
-- VERIFY (SELECT only) — expect 22 rows on sls as of 2026-10-02
-- ---------------------------------------------------------------------------
SELECT jm.jute_mr_id, bm.co_id, jm.branch_id, jm.status_id, jm.mr_weight AS old_mr_weight,
       ROUND(s.line_kg) AS new_mr_weight, jm.actual_weight AS gate_net_weight
FROM jute_mr jm
JOIN branch_mst bm ON bm.branch_id = jm.branch_id
JOIN (SELECT jute_mr_id, SUM(COALESCE(accepted_weight, actual_weight, 0)) AS line_kg
      FROM jute_mr_li WHERE (active = 1 OR active IS NULL) GROUP BY jute_mr_id) s
  ON s.jute_mr_id = jm.jute_mr_id
WHERE COALESCE(jm.mr_weight, 0) = 0 AND s.line_kg > 0
ORDER BY jm.jute_mr_id;

-- Row count the repair below would touch (run this instead of UPDATE+ROLLBACK):
SELECT COUNT(*)
FROM jute_mr jm
JOIN (SELECT jute_mr_id, SUM(COALESCE(accepted_weight, actual_weight, 0)) AS line_kg
      FROM jute_mr_li WHERE (active = 1 OR active IS NULL) GROUP BY jute_mr_id) s
  ON s.jute_mr_id = jm.jute_mr_id
WHERE COALESCE(jm.mr_weight, 0) = 0 AND s.line_kg > 0;
-- expect: 22

-- Blank headers this script deliberately leaves alone (no line weight to derive from):
--   28427 (co 2, status 3 Approved, NO lines at all, gross 19990 / tare 0 — looks like a
--          half-entered back-dated gate entry of 2024-10-25; the list shows the gate net 19990),
--   28253 (co 25), 28034 / 28037 (co 99), 27980 (co 106): lines at 0 kg.
-- Those need an owner decision, not a bulk repair.

-- ---------------------------------------------------------------------------
-- REPAIR
-- ---------------------------------------------------------------------------
-- UPDATE jute_mr jm
-- JOIN (SELECT jute_mr_id, SUM(COALESCE(accepted_weight, actual_weight, 0)) AS line_kg
--       FROM jute_mr_li WHERE (active = 1 OR active IS NULL) GROUP BY jute_mr_id) s
--   ON s.jute_mr_id = jm.jute_mr_id
-- SET jm.mr_weight = ROUND(s.line_kg)
-- WHERE COALESCE(jm.mr_weight, 0) = 0 AND s.line_kg > 0;

-- ---------------------------------------------------------------------------
-- ROLLBACK (puts back what is there today; updated_date_time / updated_by are not touched
-- by the repair, so nothing else needs restoring)
-- ---------------------------------------------------------------------------
-- UPDATE jute_mr SET mr_weight = NULL WHERE jute_mr_id IN (
--   28272, 28304, 28370, 28371, 28374, 28376, 28389, 28390, 28391, 28473, 28512, 28523,
--   28524, 28525, 28530, 28534, 28535, 28536, 28538, 28539);
-- UPDATE jute_mr SET mr_weight = 0 WHERE jute_mr_id IN (28378, 28388);
