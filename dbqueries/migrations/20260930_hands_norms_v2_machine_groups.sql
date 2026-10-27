-- =============================================================================
-- Migration: Hands Norms v2 — machine groups, production norms, no-norm kinds
-- =============================================================================
-- Date: 2026-09-30   Spec: docs/superpowers/specs/2026-09-30-hands-norms-v2-machine-groups.md
-- (§3.1-§3.4). Owner decisions of 2026-09-30 (TKT-000165 / 167 / 202).
-- TARGET: tenant DB only — dev3 first, then sls ONLY when the owner names the DB
-- (sls holds production data). Order per tenant (spec §8):
--   (1) 20260930_hands_norms_menu.sql            menu row + role grants
--   (2) dbqueries/repairs/tkt165_sls_hands_norm_machine_type_link_20260930.sql  (sls only)
--   (3) THIS FILE
-- Deploy the v2 code after (3): the current report reads only the old columns,
-- so it keeps working between (3) and the deploy.
--
-- WHAT. The Hands Report targets a machine-based norm as "machines of
-- machine_type_id that ran in the shift x hands_per_machine". Tenant machine
-- types are too coarse for that (sls: all 334 looms are one type), drawing norms
-- are per POINT not per frame, and 41 sls norms are production-based ("315 KGS /
-- WINDER") or have no norm at all ("NO NORMS", "AS ACTUAL"). This migration adds:
--
--   jute_prod_machine_group_mst   a named set of machines of one branch (may mix
--                                 machine types), with a unit label
--   jute_prod_machine_group_dtl   members with per-member units (loom 1, drawing
--                                 frame 10 points, heavy spinning frame 2)
--   jute_prod_hands_norms_mst     + norm_kind (F fixed | M machine | P production |
--                                 N no norm | A as actual), machine_group_id,
--                                 scope_dept_id, units_per_hand, qty_per_hand, qty_uom
--
-- variable_fixed and hands_per_machine are KEPT and mirrored by the API on every
-- save (M,P -> 'V', F,N,A -> 'F' and hands_per_machine = ROUND(1/units_per_hand, 4)
-- for M, NULL otherwise). Only pre-v2 code reads them — drop them in a later
-- cleanup migration once v2 is on every tenant.
--
-- House DDL style of 20260928_loom_modification_tables.sql: co_id/branch_id NOT
-- NULL on the header, active TINYINT DEFAULT 1, updated_by / updated_date_time ON
-- UPDATE, child table = own PK + parent FK + active. No created_* columns (DB
-- triggers). No DB UNIQUE on code columns — dup guards live in code among
-- active = 1: group (co_id, branch_id, group_code), member (machine_group_id,
-- machine_id). A machine may belong to several groups ("All looms" and "Hessian
-- looms"), no cross-group uniqueness.
--
-- SHOW TRIGGERS LIKE 'jute_prod_hands_norms_mst' is empty on sls and dev3
-- (checked 2026-09-30), so the ALTER and the UPDATEs fire nothing.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that
-- start with a comment marker, so every comment block ends with a bare semicolon
-- line and contains no semicolon of its own. Re-running after success fails on
-- the ALTER with "Duplicate column name" — harmless, check VERIFICATION instead.
;

-- 1. Machine group header (§3.1)
;
CREATE TABLE IF NOT EXISTS jute_prod_machine_group_mst (
  machine_group_id  INT          NOT NULL AUTO_INCREMENT,
  co_id             INT          NOT NULL,
  branch_id         INT          NOT NULL,                   -- members are machines of this branch's departments
  group_code        VARCHAR(30)  NOT NULL,                   -- dup guard in code on (co_id, branch_id, group_code) among active = 1
  group_name        VARCHAR(100) NOT NULL,
  unit_label        VARCHAR(20)  NOT NULL DEFAULT 'machine', -- machine | point | spindle | ... (free text, shown in the UI)
  remarks           VARCHAR(255) NULL,
  active            TINYINT      NOT NULL DEFAULT 1,
  updated_by        INT          NULL,
  updated_date_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (machine_group_id),
  KEY idx_mg_co_branch (co_id, branch_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Machine group members with units (§3.2)
;
CREATE TABLE IF NOT EXISTS jute_prod_machine_group_dtl (
  machine_group_dtl_id INT          NOT NULL AUTO_INCREMENT,
  machine_group_id     INT          NOT NULL,
  machine_id           INT          NOT NULL,                -- active machine whose dept_mst.branch_id = group branch (checked on write)
  units                DECIMAL(9,3) NOT NULL DEFAULT 1,      -- > 0 (checked on write): loom 1, drawing frame 10 points, heavy spinning frame 2
  active               TINYINT      NOT NULL DEFAULT 1,      -- soft-delete + reinsert on edit, one active row per machine per group (code)
  PRIMARY KEY (machine_group_dtl_id),
  KEY idx_mgd_group   (machine_group_id, active),
  KEY idx_mgd_machine (machine_id),
  CONSTRAINT fk_mgd_group   FOREIGN KEY (machine_group_id) REFERENCES jute_prod_machine_group_mst (machine_group_id),
  CONSTRAINT fk_mgd_machine FOREIGN KEY (machine_id)       REFERENCES machine_mst (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. New columns on the norms master (§3.3). Exactly one of machine_group_id /
--    machine_type_id / scope_dept_id is set on a complete M or P row (the
--    "scope"), the API clears the other two on save.
;
ALTER TABLE jute_prod_hands_norms_mst
  ADD COLUMN norm_kind        CHAR(1)       NOT NULL DEFAULT 'F' AFTER variable_fixed,   -- F | M | P | N | A
  ADD COLUMN machine_group_id INT           NULL AFTER machine_type_id,
  ADD COLUMN scope_dept_id    INT           NULL AFTER machine_group_id,                  -- M/P: whole department instead of group/type (dept_mst.dept_id)
  ADD COLUMN units_per_hand   DECIMAL(9,3)  NULL AFTER hands_per_machine,                -- M: group/type units one hand covers
  ADD COLUMN qty_per_hand     DECIMAL(12,3) NULL AFTER units_per_hand,                   -- P: production per hand per shift
  ADD COLUMN qty_uom          VARCHAR(10)   NULL AFTER qty_per_hand,                     -- P: 'kg' | 'cuts' (PROD_UOMS)
  ADD KEY idx_hands_norm_group (machine_group_id),
  ADD CONSTRAINT fk_hands_norm_group FOREIGN KEY (machine_group_id) REFERENCES jute_prod_machine_group_mst (machine_group_id),
  ADD CONSTRAINT fk_hands_norm_dept  FOREIGN KEY (scope_dept_id)    REFERENCES dept_mst (dept_id);

-- 4. Map the existing rows (§3.4). ORDER MATTERS — every statement only touches
--    rows still at the default 'F'. Sized on 2026-09-30 with SELECT only (the
--    same predicates simulated in order), counts below are for the CURRENT data
--    of each tenant (before the tkt165 repair, which changes nothing here: its 9
--    rows carry a rate and land in M either way).
--
--    4a  'AS ACTUAL' text                                -> A   sls 2 (13, 116)      dev3 0
--    4b  a per-machine rate (hands_per_machine > 0)      -> M   sls 50 (47 F + 3 V)  dev3 1 (id 5)
--        units_per_hand = ROUND(1 / hands_per_machine, 3)
--    4c  zero-shift, no rate, production text            -> P   sls 8               dev3 0
--        (KG | 'MT ' | BALE | BNDL), qty_uom 'kg', no
--        qty_per_hand = INCOMPLETE, listed by the page
--    4d  variable_fixed = 'V' with neither rate nor
--        production text                                 -> M   sls 1 (113)         dev3 0
--    4e  zero-shift, no rate, machine text               -> M   sls 13              dev3 0
--        (POINT | MC | FRM | FRAME | SPINDLE | '/ HAND'),
--        no scope and no units_per_hand = INCOMPLETE
--    4f  every other zero-shift, no-rate row             -> N   sls 34              dev3 0
--    -   everything else stays                              F   sls 134             dev3 2
--
--    4c runs BEFORE 4d on purpose (spec §3.4 reordered while sizing): sls 114
--    '15 BALES OR 2.5 MT / HAND', 115 '13 BALES OR 2 MT / HAND' and 206
--    '140 BNDLS / HAND' are flagged 'V' with zero shifts and no rate, and their
--    text is a production norm. With 4d first they would land in M.
;
UPDATE jute_prod_hands_norms_mst SET norm_kind = 'A'
 WHERE UPPER(TRIM(COALESCE(norms_text,''))) = 'AS ACTUAL';

UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M', units_per_hand = ROUND(1 / hands_per_machine, 3)
 WHERE norm_kind = 'F' AND hands_per_machine > 0;

UPDATE jute_prod_hands_norms_mst SET norm_kind = 'P', qty_uom = 'kg'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0
   AND UPPER(COALESCE(norms_text,'')) REGEXP 'KG|MT |BALE|BNDL';

UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M'
 WHERE norm_kind = 'F' AND UPPER(COALESCE(variable_fixed,'')) = 'V';

UPDATE jute_prod_hands_norms_mst SET norm_kind = 'M'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0
   AND UPPER(COALESCE(norms_text,'')) REGEXP 'POINT|MC|FRM|FRAME|SPINDLE|/ HAND';

UPDATE jute_prod_hands_norms_mst SET norm_kind = 'N'
 WHERE norm_kind = 'F' AND hands_per_machine IS NULL AND shift_a + shift_b + shift_c + shift_g = 0;

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SHOW TABLES LIKE 'jute_prod_machine_group%'
--     expected 2 rows: jute_prod_machine_group_mst, jute_prod_machine_group_dtl
--
--   SHOW CREATE TABLE jute_prod_hands_norms_mst
--     expected columns norm_kind, machine_group_id, scope_dept_id, units_per_hand,
--     qty_per_hand, qty_uom, key idx_hands_norm_group, constraints
--     fk_hands_norm_group, fk_hands_norm_dept
--
--   SELECT norm_kind, COUNT(*) FROM jute_prod_hands_norms_mst GROUP BY norm_kind
--     expected sls  (242 rows): A 2, M 64, P 8, N 34, F 134
--     expected dev3 (3 rows):   M 1, F 2
--     (sls: N 33 and F 135 if dbqueries/repairs/tkt202_sls_hands_norms_cleanup_20260930.sql
--      ran first — it sets shift_a = 1 on id 109 'A=1', the only zero-shift row
--      whose text is a shift count, so 109 then stays F instead of landing in N)
--
--   SELECT hands_norm_id, norm_kind, norms_text FROM jute_prod_hands_norms_mst
--    WHERE hands_norm_id IN (13, 116, 50, 173, 174, 175, 218, 114, 115, 206, 113,
--          140, 141, 142, 143, 144, 145, 146, 149, 152, 153, 154, 155, 162, 109)
--    ORDER BY hands_norm_id
--     expected on sls:
--       A  13, 116                          'AS ACTUAL'
--       P  50, 173, 174, 175, 218           'n KGS / WINDER'          (4c, KG)
--       P  114, 115                         'n BALES OR x MT / HAND'  (4c, BALE, were 'V')
--       P  206                              '140 BNDLS / HAND'        (4c, BNDL, was 'V')
--       M  113                              '10 MT / HAND, 5 IN A'    (4d, 'V' with shift_a 5)
--       M  140-146, 149, 152-155            'n POINTS'                (4e, 12 rows)
--       M  162                              '1 / 8 MC FOR 120 SPINDLE' (4e)
--       N  109                              'A=1'                     (4f, see the note above)
--     the other 33 N rows are all 'NO NORMS': 4, 34, 35, 37, 38, 39, 41, 51, 91,
--       99, 100, 118, 131, 151, 161, 166, 172, 178, 179, 180, 190, 195, 197, 208,
--       209, 219, 227, 231, 236, 237, 238, 239, 240
--
--   SELECT hands_norm_id, hands_per_machine, units_per_hand
--     FROM jute_prod_hands_norms_mst WHERE norm_kind = 'M' AND hands_per_machine > 0
--     expected every row units_per_hand = ROUND(1 / hands_per_machine, 3)
--     (sls 50 rows, e.g. 0.5 -> 2.000, 0.6667 -> 1.500, 2 -> 0.500)
--
--   INCOMPLETE M / P rows for the owner to complete on the Hands Norms page
--   (no scope, and/or no rate):
--   SELECT n.hands_norm_id, n.norm_kind, d.desig, n.norms_text
--     FROM jute_prod_hands_norms_mst n
--     JOIN designation_mst d ON d.designation_id = n.designation_id
--    WHERE n.norm_kind IN ('M','P')
--      AND n.machine_group_id IS NULL AND n.machine_type_id IS NULL AND n.scope_dept_id IS NULL
--    ORDER BY n.norm_kind, n.hands_norm_id
--     expected sls: every M / P row except the 9 the tkt165 repair linked
--     (19, 20, 176, 177, 200, 202, 203, 204, 205) = 63 rows (72 if the repair
--     has not run), dev3: 0 rows (id 5 already has machine_type_id)
-- =============================================================================
-- ROLLBACK (norms first, then children, then the header — FK order)
-- =============================================================================
--   ALTER TABLE jute_prod_hands_norms_mst
--     DROP FOREIGN KEY fk_hands_norm_group,
--     DROP FOREIGN KEY fk_hands_norm_dept,
--     DROP KEY idx_hands_norm_group,
--     DROP COLUMN norm_kind,
--     DROP COLUMN machine_group_id,
--     DROP COLUMN scope_dept_id,
--     DROP COLUMN units_per_hand,
--     DROP COLUMN qty_per_hand,
--     DROP COLUMN qty_uom
--   DROP TABLE IF EXISTS jute_prod_machine_group_dtl
--   DROP TABLE IF EXISTS jute_prod_machine_group_mst
--   variable_fixed / hands_per_machine are untouched by this file, so the
--   pre-v2 report reads exactly what it read before.
-- =============================================================================
;
