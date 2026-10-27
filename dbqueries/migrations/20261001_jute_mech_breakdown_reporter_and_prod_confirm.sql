-- Migration: Breakdown Entry — who reported / who fixed / production reconfirmation
-- Date: 2026-10-01
-- Database: tenant DB (dev3, sls)
-- Author: VOW dev
--
-- The breakdown is now FIRST reported by the production team on the same Breakdown
-- Entry screen; mechanical then enters the fix; production may optionally reconfirm
-- the fix or dispute the fixed time. One row per incident still — these columns only
-- record WHO did each step and what production said about the fix.
--
--   reported_by_user_id / reported_date_time   set once on create, never edited
--   fixed_by_user_id    / fixed_date_time      the user who entered the restart /
--                                              duration (cleared when reopened)
--   prod_confirm_status                        NULL = not checked | CONFIRMED | DISPUTED
--   prod_confirm_by / prod_confirm_date_time   the production user and when
--   prod_restart_datetime                      production's own restart time (DISPUTED)
--   prod_confirm_remarks                       production's remark
--
-- The existing free-text `reported_by` column is untouched. All columns are NULLable,
-- so rows logged before this migration simply show no reporter / no confirmation.
-- duration_min and every report figure keep using mechanical's restart time;
-- production's time is recorded alongside it, never substituted.
--
-- ROLLBACK:
--   ALTER TABLE jute_mech_breakdown
--     DROP COLUMN prod_confirm_remarks,
--     DROP COLUMN prod_restart_datetime,
--     DROP COLUMN prod_confirm_date_time,
--     DROP COLUMN prod_confirm_by,
--     DROP COLUMN prod_confirm_status,
--     DROP COLUMN fixed_date_time,
--     DROP COLUMN fixed_by_user_id,
--     DROP COLUMN reported_date_time,
--     DROP COLUMN reported_by_user_id;

ALTER TABLE jute_mech_breakdown
    ADD COLUMN reported_by_user_id    INT          NULL AFTER reported_by,
    ADD COLUMN reported_date_time     DATETIME     NULL AFTER reported_by_user_id,
    ADD COLUMN fixed_by_user_id       INT          NULL AFTER reported_date_time,
    ADD COLUMN fixed_date_time        DATETIME     NULL AFTER fixed_by_user_id,
    ADD COLUMN prod_confirm_status    VARCHAR(20)  NULL AFTER status,
    ADD COLUMN prod_confirm_by        INT          NULL AFTER prod_confirm_status,
    ADD COLUMN prod_confirm_date_time DATETIME     NULL AFTER prod_confirm_by,
    ADD COLUMN prod_restart_datetime  DATETIME     NULL AFTER prod_confirm_date_time,
    ADD COLUMN prod_confirm_remarks   VARCHAR(500) NULL AFTER prod_restart_datetime;

-- VERIFICATION
-- SELECT COUNT(*) FROM information_schema.columns
--  WHERE table_schema = DATABASE() AND table_name = 'jute_mech_breakdown'
--    AND column_name IN ('reported_by_user_id','reported_date_time','fixed_by_user_id',
--        'fixed_date_time','prod_confirm_status','prod_confirm_by',
--        'prod_confirm_date_time','prod_restart_datetime','prod_confirm_remarks');  -- expect 9
