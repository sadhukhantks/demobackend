-- Migration: Jute Production Planning — daily plan document + planning norms master.
-- Date: 2026-08-08
-- Spec: ../vowerp3ui/docs/claude/modules/jute-production/production-planning/SPEC.md (§2)
-- Applies to: tenant database dev3 (QA target). Promote to other tenants later.
--
-- Context: "Production Planning" is a DAILY plan document for a jute mill, modeled on
-- the mill's "S4 LOOM PRODUCTION PLANNING" sheet. One document per
-- (co_id, branch_id, plan_date) carries three line sections:
--   1. jute_prod_planning_loom  — loom/weaving plan (shift-wise loom counts + kg/loom)
--   2. jute_prod_planning_yarn  — yarn requirement + spinning-frame allocation
--   3. jute_prod_planning_dept  — dept-wise breakup / machines required (7 stages)
-- plus a norms master (jute_prod_planning_norm) holding the effective-dated standards
-- the plan defaults from (same EAV shape + LAST-DATE resolution as *_target_map).
--
-- NB: All machine rates in these tables are PER MACHINE PER SHIFT, so every
-- *_required column is expressed in machine-shifts per day, and the A/B/C columns are
-- machines running in that shift. No approval workflow — direct CRUD, soft delete,
-- status_id = 1 (Open).
--
-- ---------------------------------------------------------------------------
-- HOW TO RUN — there is no mysql CLI on this machine; use the pymysql runner
-- (CLAUDE.md → "Running migrations"). Read credentials from env/database.env.
-- Target database: dev3.
--
--   source .venv/Scripts/activate && python -c "
--   import pymysql
--   conn = pymysql.connect(host='<HOST>', port=3306, user='<USER>', password='<PASS>', database='dev3')
--   cursor = conn.cursor()
--   with open('dbqueries/migrations/20260808_jute_production_planning.sql', 'r') as f:
--       for stmt in f.read().split(';'):
--           stmt = stmt.strip()
--           if stmt and not stmt.startswith('--'):
--               cursor.execute(stmt)
--   conn.commit()
--   conn.close()
--   print('Migration applied successfully')
--   "
--
-- ---------------------------------------------------------------------------
-- ROLLBACK (drop children first — no FKs are declared, but keep the order anyway):
--   DROP TABLE IF EXISTS `jute_prod_planning_norm`;
--   DROP TABLE IF EXISTS `jute_prod_planning_dept`;
--   DROP TABLE IF EXISTS `jute_prod_planning_yarn`;
--   DROP TABLE IF EXISTS `jute_prod_planning_loom`;
--   DROP TABLE IF EXISTS `jute_prod_planning`;
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. jute_prod_planning — plan header (SPEC §2.1)
--    One active header per (co_id, branch_id, plan_date); enforced in the API
--    (planning_save) rather than by a unique index, because branch_id is NULL-able
--    and soft-deleted rows (active=0) must be allowed to repeat the same date.
-- ---------------------------------------------------------------------------
CREATE TABLE `jute_prod_planning` (
    `planning_id`       INT          NOT NULL AUTO_INCREMENT,
    `co_id`             INT          NOT NULL,
    `branch_id`         INT          NULL,
    `plan_date`         DATE         NOT NULL,
    `plan_no`           VARCHAR(30)  NULL COMMENT 'JPP-00001 — CONCAT(''JPP-'', LPAD(planning_id,5,''0'')) after insert',
    `plan_name`         VARCHAR(100) NULL,
    `remarks`           VARCHAR(500) NULL,
    `status_id`         INT          NOT NULL DEFAULT 1 COMMENT '1 = Open; no approval workflow in v1',
    `active`            INT          NOT NULL DEFAULT 1,
    `updated_by`        INT          NULL,
    `updated_date_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`planning_id`),
    KEY `idx_jpp_co_date` (`co_id`, `plan_date`),
    KEY `idx_jpp_branch` (`branch_id`)
);

-- ---------------------------------------------------------------------------
-- 2. jute_prod_planning_loom — Section 1, loom / weaving plan lines (SPEC §2.2)
--    kg_per_loom_100 is the 100%-eff norm; kg_per_loom = kg_per_loom_100 * eff_pct/100
--    (per loom PER SHIFT) and prod_x = looms_x * kg_per_loom.
-- ---------------------------------------------------------------------------
CREATE TABLE `jute_prod_planning_loom` (
    `planning_loom_id`   INT           NOT NULL AUTO_INCREMENT,
    `planning_id`        INT           NOT NULL,
    `product_group`      VARCHAR(20)   NOT NULL COMMENT 'SACKING | HESSIAN | PACKSHEET | OTHER',
    `quality_item_id`    INT           NULL COMMENT 'item_mst.item_id of the cloth quality (item_type_id=5); NULL = group-level row',
    `quality_desc`       VARCHAR(120)  NULL COMMENT 'denormalized label as printed',
    `looms_a`            DECIMAL(8,2)  NOT NULL DEFAULT 0,
    `looms_b`            DECIMAL(8,2)  NOT NULL DEFAULT 0,
    `looms_c`            DECIMAL(8,2)  NOT NULL DEFAULT 0,
    `kg_per_loom_100`    DECIMAL(10,3) NOT NULL DEFAULT 0 COMMENT 'per loom per shift @100% eff',
    `eff_pct`            DECIMAL(5,2)  NOT NULL DEFAULT 0,
    `kg_per_loom`        DECIMAL(10,3) NOT NULL DEFAULT 0 COMMENT 'effective per loom per shift',
    `prod_a`             DECIMAL(12,3) NOT NULL DEFAULT 0,
    `prod_b`             DECIMAL(12,3) NOT NULL DEFAULT 0,
    `prod_c`             DECIMAL(12,3) NOT NULL DEFAULT 0,
    `warp_yarn_item_id`  INT           NULL COMMENT 'item_mst.item_id of the warp yarn (item_type_id=4)',
    `weft_yarn_item_id`  INT           NULL COMMENT 'item_mst.item_id of the weft yarn (item_type_id=4)',
    `warp_pct`           DECIMAL(5,2)  NULL,
    `weft_pct`           DECIMAL(5,2)  NULL,
    `line_order`         INT           NOT NULL DEFAULT 0,
    `remarks`            VARCHAR(255)  NULL,
    `active`             INT           NOT NULL DEFAULT 1,
    `updated_by`         INT           NULL,
    `updated_date_time`  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`planning_loom_id`),
    KEY `idx_jppl_plan` (`planning_id`),
    KEY `idx_jppl_quality` (`quality_item_id`)
);

-- ---------------------------------------------------------------------------
-- 3. jute_prod_planning_yarn — Section 2, yarn requirement + frame allocation (SPEC §2.3)
--    frames_required is FRAME-SHIFTS per day (req_kg_day / kg_per_frame); frames_a/b/c
--    are frames running in that shift.
-- ---------------------------------------------------------------------------
CREATE TABLE `jute_prod_planning_yarn` (
    `planning_yarn_id`   INT           NOT NULL AUTO_INCREMENT,
    `planning_id`        INT           NOT NULL,
    `line_kind`          VARCHAR(10)   NOT NULL COMMENT 'CLOTH (derived from the loom plan) | SALE (order-driven)',
    `section`            VARCHAR(20)   NOT NULL COMMENT 'SACKING | HESSIAN | PACKSHEET | TWINE | SALE | OTHER',
    `yarn_item_id`       INT           NULL COMMENT 'item_mst.item_id of the yarn (item_type_id=4)',
    `yarn_desc`          VARCHAR(120)  NULL,
    `warp_weft`          VARCHAR(2)    NULL COMMENT 'WP | WT | NULL',
    `pct_of_prod`        DECIMAL(5,2)  NULL COMMENT '% of section cloth production (sacking-style group rows)',
    `req_kg_a`           DECIMAL(12,3) NOT NULL DEFAULT 0,
    `req_kg_b`           DECIMAL(12,3) NOT NULL DEFAULT 0,
    `req_kg_c`           DECIMAL(12,3) NOT NULL DEFAULT 0,
    `req_kg_day`         DECIMAL(12,3) NOT NULL DEFAULT 0,
    `kg_per_frame_100`   DECIMAL(10,3) NOT NULL DEFAULT 0 COMMENT 'per frame per shift @100% eff',
    `eff_pct`            DECIMAL(5,2)  NOT NULL DEFAULT 0,
    `kg_per_frame`       DECIMAL(10,3) NOT NULL DEFAULT 0 COMMENT 'effective per frame per shift',
    `frames_required`    DECIMAL(8,2)  NOT NULL DEFAULT 0 COMMENT 'frame-shifts/day = req_kg_day / kg_per_frame',
    `frames_a`           DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `frames_b`           DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `frames_c`           DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `priority`           INT           NULL,
    `order_mt`           DECIMAL(10,3) NULL COMMENT 'SALE lines: order quantity (MT)',
    `days_needed`        DECIMAL(8,2)  NULL COMMENT 'SALE lines: (order_mt*1000/kg_per_frame) / (frames_a+b+c)',
    `delivery_date`      DATE          NULL COMMENT 'SALE lines',
    `line_order`         INT           NOT NULL DEFAULT 0,
    `remarks`            VARCHAR(255)  NULL,
    `active`             INT           NOT NULL DEFAULT 1,
    `updated_by`         INT           NULL,
    `updated_date_time`  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`planning_yarn_id`),
    KEY `idx_jppy_plan` (`planning_id`),
    KEY `idx_jppy_yarn` (`yarn_item_id`)
);

-- ---------------------------------------------------------------------------
-- 4. jute_prod_planning_dept — Section 3, dept-wise breakup / machines required (SPEC §2.4)
--    Backward explosion, stage_seq 1..7:
--      BATCHING(1) -> CARDING(2) -> DRAWING(3) -> SPINNING(4) -> WINDING(5)
--      -> BEAMING(6) -> WEAVING(7)
--    input_kg_day = output_kg_day * (1 + waste_pct/100); BATCHING.input_kg_day is the
--    headline raw-jute requirement per day. For WINDING the "machines" are winders
--    (winding is person-keyed in this ERP) — the FE labels it "Machines / Workers".
-- ---------------------------------------------------------------------------
CREATE TABLE `jute_prod_planning_dept` (
    `planning_dept_id`    INT           NOT NULL AUTO_INCREMENT,
    `planning_id`         INT           NOT NULL,
    `stage_seq`           INT           NOT NULL COMMENT '1..7 in flow order',
    `stage_code`          VARCHAR(20)   NOT NULL COMMENT 'BATCHING|CARDING|DRAWING|SPINNING|WINDING|BEAMING|WEAVING',
    `stage_label`         VARCHAR(50)   NULL,
    `output_kg_day`       DECIMAL(14,3) NOT NULL DEFAULT 0 COMMENT 'required output/day',
    `waste_pct`           DECIMAL(5,2)  NOT NULL DEFAULT 0,
    `input_kg_day`        DECIMAL(14,3) NOT NULL DEFAULT 0 COMMENT 'output_kg_day * (1 + waste_pct/100)',
    `kg_per_machine`      DECIMAL(12,3) NOT NULL DEFAULT 0 COMMENT 'per machine per shift',
    `machines_required`   DECIMAL(8,2)  NOT NULL DEFAULT 0 COMMENT 'machine-shifts/day = output_kg_day / kg_per_machine',
    `machines_a`          DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `machines_b`          DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `machines_c`          DECIMAL(6,1)  NOT NULL DEFAULT 0,
    `machines_installed`  INT           NULL,
    `machines_maint`      INT           NULL,
    `machines_ready`      INT           NULL,
    `line_order`          INT           NOT NULL DEFAULT 0,
    `remarks`             VARCHAR(255)  NULL,
    `active`              INT           NOT NULL DEFAULT 1,
    `updated_by`          INT           NULL,
    `updated_date_time`   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`planning_dept_id`),
    KEY `idx_jppd_plan` (`planning_id`),
    KEY `idx_jppd_stage` (`planning_id`, `stage_seq`)
);

-- ---------------------------------------------------------------------------
-- 5. jute_prod_planning_norm — planning norms master (SPEC §2.5)
--    Same EAV shape as the *_target_map tables, with LAST-DATE resolution: among
--    active=1 rows matching (co_id, id_type, ref_id, param), take MAX(effective_date)
--    <= :on_date. value_role is fixed 'standard' in v1 (shape parity only).
--    Params — id_type='qid'  (ref_id = cloth item_id): kg_per_loom_100, warp_pct,
--                             weft_pct, warp_yarn_item, weft_yarn_item, std_eff
--             id_type='yid'  (ref_id = yarn item_id) : kg_per_frame_100, std_eff
--             id_type='stage'(ref_id = stage_seq 1..7): kg_per_machine, waste_pct
-- ---------------------------------------------------------------------------
CREATE TABLE `jute_prod_planning_norm` (
    `planning_norm_id`  INT           NOT NULL AUTO_INCREMENT,
    `co_id`             INT           NOT NULL,
    `branch_id`         INT           NULL,
    `effective_date`    DATE          NOT NULL,
    `ref_id`            INT           NOT NULL COMMENT 'qid: cloth item_id | yid: yarn item_id | stage: stage_seq (1..7)',
    `id_type`           VARCHAR(8)    NOT NULL COMMENT 'qid | yid | stage',
    `value_role`        VARCHAR(10)   NOT NULL DEFAULT 'standard' COMMENT 'fixed ''standard'' in v1',
    `param`             VARCHAR(30)   NOT NULL,
    `value`             DECIMAL(14,4) NOT NULL DEFAULT 0,
    `active`            INT           NOT NULL DEFAULT 1,
    `updated_by`        INT           NULL,
    `updated_date_time` TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`planning_norm_id`),
    KEY `idx_jppn` (`co_id`, `id_type`, `ref_id`, `param`, `effective_date`)
);
