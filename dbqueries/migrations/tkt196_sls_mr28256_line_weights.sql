-- TKT-000196 — sls, branch 29 (FACTORY): Jute Stock (Production) read D TD-5 -2,479 kg
-- and D TD-6 -3,747 kg on 2026-09-23.
--
-- Cause (data, not the report): MR 28256 (gate entry 46, lorry WB-25J-8701, gate-out
-- 2026-09-20, header net 9,110 kg / actual 9,040 kg) was fully issued on 2026-09-21 by
-- nine DRAFT issues at the line weights of the time — 3,600 kg (line 45727, D TD-5,
-- 90 units) and 5,440 kg (line 45728, D TD-6, 136 units), i.e. the header's 9,040 kg
-- split by quantity. On 2026-09-24 16:23:42 both lines were re-saved at 1,121 / 1,693 kg
-- (= 2,814 kg split by the same quantities) while the header still says 9,040 kg and
-- every sibling September MR has lines == header. Quantities were left at 90 / 136.
--
-- DO NOT RUN BLIND. sls is production: the owner decides which figure is physically
-- true and runs the matching option. Preferred route is the UI, not this file:
--   Option A (lines wrong, issues right — recommended by the numbers):
--     Jute MR -> MR 28256 -> Reopen -> set D TD-5 = 3600, D TD-6 = 5440 -> Save -> Pending.
--     (pending_mr recomputes total_price / header money.)
--   Option B (2,814 kg really is what came in):
--     Jute MR Wise Issue Detail report -> MR 28256 -> "Sync issued weights" (scales the
--     nine draft issues down to 1,121 / 1,693 so both lines close at 0).
--
-- ---------------------------------------------------------------------------
-- VERIFY (SELECT only) — expect 2 rows, bal_weight -2479 and -3747
-- ---------------------------------------------------------------------------
SELECT v.jute_mr_li_id, v.actual_item_id, v.actual_weight, v.bal_weight, v.mr_status_id
FROM vw_jute_stock_outstanding v
WHERE v.branch_id = 29 AND v.jute_mr_li_id IN (45727, 45728);

SELECT jute_mr_li_id, actual_item_id, actual_qty, actual_weight, accepted_weight, total_price
FROM jute_mr_li WHERE jute_mr_id = 28256;
-- expect: 45727 | 56 | 90  | 1121 | 1121 | 140125.00
--         45728 | 57 | 136 | 1693 | 1693 | 208239.00

SELECT COUNT(*) AS n_issues, ROUND(SUM(weight), 0) AS issued_kg
FROM jute_issue WHERE jute_mr_li_id IN (45727, 45728) AND status_id <> 4;
-- expect: 9 | 9040

-- Row count the repair below would touch (run this instead of UPDATE+ROLLBACK):
SELECT COUNT(*) FROM jute_mr_li WHERE jute_mr_id = 28256 AND jute_mr_li_id IN (45727, 45728);
-- expect: 2

-- ---------------------------------------------------------------------------
-- REPAIR — Option A as SQL, only if the owner prefers it over the UI route.
-- Restores the gate-out split (9,040 kg over 90 : 136 units) and the money the
-- MR page would compute (accepted = actual, no moisture claim: 20.00 vs 20 allowed).
-- ---------------------------------------------------------------------------
-- UPDATE jute_mr_li
-- SET actual_weight = 3600, accepted_weight = 3600, shortage_kgs = 0,
--     total_price = ROUND(3600 / 100 * rate, 2), updated_date_time = NOW()
-- WHERE jute_mr_li_id = 45727 AND jute_mr_id = 28256;
-- UPDATE jute_mr_li
-- SET actual_weight = 5440, accepted_weight = 5440, shortage_kgs = 0,
--     total_price = ROUND(5440 / 100 * rate, 2), updated_date_time = NOW()
-- WHERE jute_mr_li_id = 45728 AND jute_mr_id = 28256;
-- UPDATE jute_mr
-- SET mr_weight = 9040,
--     total_amount = 1119120.00, net_total = 1119120.00, claim_amount = 0, roundoff = 0,
--     updated_date_time = NOW()
-- WHERE jute_mr_id = 28256;
-- -- total_amount = 3600/100*12500 + 5440/100*12300 = 450000 + 669120

-- ---------------------------------------------------------------------------
-- ROLLBACK (puts back what is there today)
-- ---------------------------------------------------------------------------
-- UPDATE jute_mr_li SET actual_weight = 1121, accepted_weight = 1121, shortage_kgs = 0,
--     total_price = 140125.00 WHERE jute_mr_li_id = 45727 AND jute_mr_id = 28256;
-- UPDATE jute_mr_li SET actual_weight = 1693, accepted_weight = 1693, shortage_kgs = 0,
--     total_price = 208239.00 WHERE jute_mr_li_id = 45728 AND jute_mr_id = 28256;
-- UPDATE jute_mr SET mr_weight = 2814, total_amount = 348364.00, net_total = 348364.00
-- WHERE jute_mr_id = 28256;
