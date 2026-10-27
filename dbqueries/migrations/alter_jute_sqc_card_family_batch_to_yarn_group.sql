-- Carding/drawing SQC quality linkage: BATCH (jute_batch_plan) -> YARN GROUP
-- (item_grp_mst, item_type_id 4 = Jute Yarn). Drops batch_plan_id (QA-only data,
-- user-approved) and adds item_grp_id on the 4 report tables:
--   jute_sqc_breaker_card_swt, jute_sqc_card_sliver_wt,
--   jute_sqc_fin_draw_sliver_wt, jute_sqc_draw_sliver_wt
-- Target tenants: dev3 + sls (others BEFORE code deploy).
--
-- Rollback:
--   ALTER TABLE jute_sqc_breaker_card_swt   DROP COLUMN item_grp_id, ADD COLUMN batch_plan_id BIGINT NULL;
--   ALTER TABLE jute_sqc_card_sliver_wt     DROP COLUMN item_grp_id, ADD COLUMN batch_plan_id BIGINT NULL;
--   ALTER TABLE jute_sqc_fin_draw_sliver_wt DROP COLUMN item_grp_id, ADD COLUMN batch_plan_id BIGINT NULL;
--   ALTER TABLE jute_sqc_draw_sliver_wt     DROP COLUMN item_grp_id, ADD COLUMN batch_plan_id BIGINT NULL;

ALTER TABLE jute_sqc_breaker_card_swt
    DROP COLUMN batch_plan_id,
    ADD COLUMN item_grp_id BIGINT NULL;

ALTER TABLE jute_sqc_card_sliver_wt
    DROP COLUMN batch_plan_id,
    ADD COLUMN item_grp_id BIGINT NULL;

ALTER TABLE jute_sqc_fin_draw_sliver_wt
    DROP COLUMN batch_plan_id,
    ADD COLUMN item_grp_id BIGINT NULL;

ALTER TABLE jute_sqc_draw_sliver_wt
    DROP COLUMN batch_plan_id,
    ADD COLUMN item_grp_id BIGINT NULL;
