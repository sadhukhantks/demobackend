-- MRLINEWT-20260912 rollback (sls, branch 29) — restores pre-repair line weights
UPDATE jute_mr_li SET actual_weight = 0.0, shortage_kgs = 0, accepted_weight = 0.0 WHERE jute_mr_li_id = 45599;
UPDATE jute_mr_li SET actual_weight = 0.0, shortage_kgs = 0, accepted_weight = 0.0 WHERE jute_mr_li_id = 45600;
UPDATE jute_mr_li SET actual_weight = 0.0, shortage_kgs = 0, accepted_weight = 0.0 WHERE jute_mr_li_id = 45608;
UPDATE jute_mr_li SET actual_weight = 0.0, shortage_kgs = 0, accepted_weight = 0.0 WHERE jute_mr_li_id = 45609;
