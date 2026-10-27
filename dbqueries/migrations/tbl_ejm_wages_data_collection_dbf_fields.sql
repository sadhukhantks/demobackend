-- Add the VOWDATA.DBF hand-entry fields missing from tbl_ejm_wages_data_collection.
-- Source structure: e:\ejmhands\VOWDATA.DBF (legacy FoxPro hands file):
--   QCODE1..QCODE8 C(3) + PROD1..PROD8 N(10,3) — per-quality production pairs
--   (piece-rated workers can book production against up to 8 quality codes;
--   the table only had the aggregate `production` column).
-- Also widen act_eff decimal(7,2) -> decimal(10,5): DBF EFF is N(10,5)
-- (e.g. 91.19620) and decimal(7,2) silently rounds it. Lossless widening.
-- All other DBF fields already map: EBNO->eb_id, DEPTCODE->dept_code,
-- OCCUCODE->occu_code, SHIFT->shift, OCC_TYPE->t_p, MCNOS->mc_nos,
-- WRK_HRS->working_hours, OT_HRS->ot_hours, NS_HRS->ns_hours,
-- PROD_BAS->prod_basic, TIME_BASIC->time_basic, EFF->act_eff.
-- Target: dev3

ALTER TABLE tbl_ejm_wages_data_collection
  ADD COLUMN qcode1 char(3) DEFAULT NULL AFTER production,
  ADD COLUMN prod1 decimal(10,3) DEFAULT '0.000' AFTER qcode1,
  ADD COLUMN qcode2 char(3) DEFAULT NULL AFTER prod1,
  ADD COLUMN prod2 decimal(10,3) DEFAULT '0.000' AFTER qcode2,
  ADD COLUMN qcode3 char(3) DEFAULT NULL AFTER prod2,
  ADD COLUMN prod3 decimal(10,3) DEFAULT '0.000' AFTER qcode3,
  ADD COLUMN qcode4 char(3) DEFAULT NULL AFTER prod3,
  ADD COLUMN prod4 decimal(10,3) DEFAULT '0.000' AFTER qcode4,
  ADD COLUMN qcode5 char(3) DEFAULT NULL AFTER prod4,
  ADD COLUMN prod5 decimal(10,3) DEFAULT '0.000' AFTER qcode5,
  ADD COLUMN qcode6 char(3) DEFAULT NULL AFTER prod5,
  ADD COLUMN prod6 decimal(10,3) DEFAULT '0.000' AFTER qcode6,
  ADD COLUMN qcode7 char(3) DEFAULT NULL AFTER prod6,
  ADD COLUMN prod7 decimal(10,3) DEFAULT '0.000' AFTER qcode7,
  ADD COLUMN qcode8 char(3) DEFAULT NULL AFTER prod7,
  ADD COLUMN prod8 decimal(10,3) DEFAULT '0.000' AFTER qcode8,
  MODIFY COLUMN act_eff decimal(10,5) DEFAULT NULL;

-- Rollback:
-- ALTER TABLE tbl_ejm_wages_data_collection
--   DROP COLUMN qcode1, DROP COLUMN prod1, DROP COLUMN qcode2, DROP COLUMN prod2,
--   DROP COLUMN qcode3, DROP COLUMN prod3, DROP COLUMN qcode4, DROP COLUMN prod4,
--   DROP COLUMN qcode5, DROP COLUMN prod5, DROP COLUMN qcode6, DROP COLUMN prod6,
--   DROP COLUMN qcode7, DROP COLUMN prod7, DROP COLUMN qcode8, DROP COLUMN prod8,
--   MODIFY COLUMN act_eff decimal(7,2) DEFAULT NULL;
