-- =============================================================================
-- Migration: Jute Gain/Loss Sheet — masters + monthly sheet tables (+ seeds)
-- =============================================================================
-- Date: 2026-08-25
-- TARGET: tenant DB (dev3 first).
--
-- Replaces the mill's "Jute Gain / Loss" Excel workbook (FNG packed / WVG
-- weaving / WDG winding-to-weaving statements). One sheet per (co, branch,
-- month); everything is manual entry; the three statements are renderings of
-- the same sheet, differing only in which figure rows and which process-stock
-- departments they include — both of which are DATA (the two masters below),
-- not code, so a tenant can add rows/departments without a release.
--
-- All quantities are MT (the unit the statement is published in).
-- No FKs, matching the jute production module convention (see
-- create_jute_prod_winding_stock.sql). Soft delete via active.
--
-- Seeds resolve co_id from co_mst at run time — nothing hardcodes ids — and are
-- idempotent via NOT EXISTS. Companies created later need the two masters
-- re-seeded (re-run this script; it only fills gaps).
;

-- 1. Figure-row master: one row per named line of the statement
--    (Jute Issue, Yarn Purchase, Sale Yarn, ...). section groups it under
--    INPUT or OUTPUT; calc_sign is +1 for "Add" rows and -1 for "Less" rows;
--    in_fng / in_wvg / in_wdg say which statements include the row.
;
CREATE TABLE IF NOT EXISTS jute_gl_row_mst (
  gl_row_id         INT PRIMARY KEY AUTO_INCREMENT,
  co_id             INT NOT NULL,
  row_code          VARCHAR(40) NOT NULL,
  row_name          VARCHAR(150) NOT NULL,
  section           VARCHAR(10) NOT NULL,          -- 'INPUT' | 'OUTPUT'
  calc_sign         INT NOT NULL DEFAULT 1,        -- +1 Add, -1 Less
  in_fng            TINYINT NOT NULL DEFAULT 1,
  in_wvg            TINYINT NOT NULL DEFAULT 1,
  in_wdg            TINYINT NOT NULL DEFAULT 1,
  display_order     INT NOT NULL DEFAULT 0,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_row_co (co_id)
);

-- 2. Process-stock department master. FNG spans every active department;
--    in_wvg marks the Batching→Weaving subset; in_wdg the Winding→Weaving
--    subset. "Singur Shed" is just another row here — no constant in code.
;
CREATE TABLE IF NOT EXISTS jute_gl_dept_mst (
  gl_dept_id        INT PRIMARY KEY AUTO_INCREMENT,
  co_id             INT NOT NULL,
  dept_code         VARCHAR(40) NOT NULL,
  dept_name         VARCHAR(150) NOT NULL,
  in_wvg            TINYINT NOT NULL DEFAULT 1,
  in_wdg            TINYINT NOT NULL DEFAULT 0,
  display_order     INT NOT NULL DEFAULT 0,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_dept_co (co_id)
);

-- 3. Monthly sheet header. month_date is always the 1st of the month.
--    is_locked freezes the sheet (Draft -> Lock lifecycle; unlock allowed for
--    corrections, covering the mill's "revised" months).
;
CREATE TABLE IF NOT EXISTS jute_gain_loss_hdr (
  gain_loss_id      INT PRIMARY KEY AUTO_INCREMENT,
  co_id             INT NOT NULL,
  branch_id         INT NOT NULL,
  month_date        DATE NOT NULL,
  remarks           VARCHAR(1000) NULL,
  is_locked         TINYINT NOT NULL DEFAULT 0,
  locked_by         INT NULL,
  locked_date_time  TIMESTAMP NULL,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_hdr_unit (co_id, branch_id, month_date)
);

-- 4. Figure values: Hess/Sack MT per master row per sheet.
;
CREATE TABLE IF NOT EXISTS jute_gain_loss_dtl (
  gain_loss_dtl_id  INT PRIMARY KEY AUTO_INCREMENT,
  gain_loss_id      INT NOT NULL,
  gl_row_id         INT NOT NULL,
  hess_qty          DOUBLE NOT NULL DEFAULT 0,
  sack_qty          DOUBLE NOT NULL DEFAULT 0,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_dtl_hdr (gain_loss_id)
);

-- 5. Quality-wise production lines. statement 'FNG' = packed production
--    (bale press), 'WVG' = weaving production. quality_type mirrors
--    jute_prod_finishing_quality.quality_type (1 = Hessian, 2 = Sacking).
--    quality_name is free text so the sheet can carry qualities that have no
--    master row yet (the FE offers finishing-quality names as suggestions).
;
CREATE TABLE IF NOT EXISTS jute_gain_loss_prod_dtl (
  gain_loss_prod_id INT PRIMARY KEY AUTO_INCREMENT,
  gain_loss_id      INT NOT NULL,
  statement         VARCHAR(3) NOT NULL,            -- 'FNG' | 'WVG'
  quality_type      INT NOT NULL,                   -- 1 = Hessian, 2 = Sacking
  quality_name      VARCHAR(255) NOT NULL,
  qty               DOUBLE NOT NULL DEFAULT 0,
  display_order     INT NOT NULL DEFAULT 0,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_prod_hdr (gain_loss_id)
);

-- 6. Month-end process stock per department. Opening is stored (not derived)
--    so a locked sheet is self-contained; the FE prefills it from the previous
--    month's closing and the report flags any mismatch.
;
CREATE TABLE IF NOT EXISTS jute_gain_loss_process_stock (
  gain_loss_ps_id   INT PRIMARY KEY AUTO_INCREMENT,
  gain_loss_id      INT NOT NULL,
  gl_dept_id        INT NOT NULL,
  opening_hess      DOUBLE NOT NULL DEFAULT 0,
  opening_sack      DOUBLE NOT NULL DEFAULT 0,
  closing_hess      DOUBLE NOT NULL DEFAULT 0,
  closing_sack      DOUBLE NOT NULL DEFAULT 0,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gl_ps_hdr (gain_loss_id)
);

-- =============================================================================
-- Seeds — figure rows (per company, gap-filling, mirrors the Excel rows)
-- =============================================================================
;
INSERT INTO jute_gl_row_mst
  (co_id, row_code, row_name, section, calc_sign, in_fng, in_wvg, in_wdg, display_order, updated_by)
SELECT c.co_id, d.row_code, d.row_name, d.section, d.calc_sign, d.in_fng, d.in_wvg, d.in_wdg, d.display_order, 1
FROM co_mst c
CROSS JOIN (
  SELECT 'JUTE_ISSUE' AS row_code, 'Jute Issue' AS row_name, 'INPUT' AS section, 1 AS calc_sign, 1 AS in_fng, 1 AS in_wvg, 0 AS in_wdg, 10 AS display_order
  UNION ALL SELECT 'WINDING_PRODUCTION', 'Winding Production', 'INPUT', 1, 0, 0, 1, 15
  UNION ALL SELECT 'YARN_PURCHASE', 'Yarn Purchase', 'INPUT', 1, 1, 1, 1, 20
  UNION ALL SELECT 'BALE_PURCHASE', 'Bale Purchase', 'INPUT', 1, 1, 1, 0, 30
  UNION ALL SELECT 'CLOTH_PURCHASE', 'Cloth Purchase', 'INPUT', 1, 1, 1, 1, 40
  UNION ALL SELECT 'LEAD_ROLL', 'Lead Roll (+/-)', 'INPUT', 1, 0, 1, 0, 50
  UNION ALL SELECT 'FRESH_COPS_SOLD', 'Fresh Cops Sold', 'INPUT', -1, 1, 1, 1, 60
  UNION ALL SELECT 'BROKEN_COPS_SOLD', 'Others (Broken Cops Sold)', 'INPUT', -1, 1, 1, 1, 70
  UNION ALL SELECT 'PACK_SHEET_CONSUMED', 'Pack Sheet Consumed', 'OUTPUT', 1, 1, 1, 1, 10
  UNION ALL SELECT 'SALE_YARN', 'Sale Yarn', 'OUTPUT', 1, 1, 1, 1, 20
  UNION ALL SELECT 'HESSIAN_ROLL', 'Hessian Roll', 'OUTPUT', 1, 1, 0, 0, 30
  UNION ALL SELECT 'RAW_JUTE_FIBRE_SALE', 'Raw Jute / Jute Fibre Sale', 'OUTPUT', 1, 1, 1, 1, 40
  UNION ALL SELECT 'JUTE_FOR_JUCO', 'Damaged Yarn / Jute for JUCO', 'OUTPUT', 1, 1, 1, 0, 50
  UNION ALL SELECT 'LENO_SELVEDGE', '129 CM-12.34 OZ / 51x51 Leno Selvedge', 'OUTPUT', 1, 1, 1, 1, 60
  UNION ALL SELECT 'HESSIAN_SHORT_PIECES', 'Hessian Short Pieces / Old Damage Mix Hess Cloth', 'OUTPUT', 1, 1, 1, 1, 70
  UNION ALL SELECT 'THREAD_WASTE', 'Thread Waste / Gunny Cutting / Rags & Chindis', 'OUTPUT', 1, 1, 1, 0, 80
) d
WHERE NOT EXISTS (
  SELECT 1 FROM jute_gl_row_mst x
  WHERE x.co_id = c.co_id AND x.row_code = d.row_code
);

-- =============================================================================
-- Seeds — process-stock departments (per company, gap-filling)
-- =============================================================================
;
INSERT INTO jute_gl_dept_mst
  (co_id, dept_code, dept_name, in_wvg, in_wdg, display_order, updated_by)
SELECT c.co_id, d.dept_code, d.dept_name, d.in_wvg, d.in_wdg, d.display_order, 1
FROM co_mst c
CROSS JOIN (
  SELECT 'BUFFER' AS dept_code, 'Buffer Stock in Jute Godown / Issued Jute in Selection Floor' AS dept_name, 1 AS in_wvg, 0 AS in_wdg, 10 AS display_order
  UNION ALL SELECT 'SAMPLE', 'Sample, Coloured Yarn / Bleach etc', 1, 0, 20
  UNION ALL SELECT 'BATCHING', 'Batching', 1, 0, 30
  UNION ALL SELECT 'PREPARING', 'Preparing', 1, 0, 40
  UNION ALL SELECT 'SPINNING', 'Spinning', 1, 0, 50
  UNION ALL SELECT 'WINDING', 'Winding', 1, 1, 60
  UNION ALL SELECT 'BEAMING', 'Beaming', 1, 1, 70
  UNION ALL SELECT 'WEAVING', 'Weaving', 1, 1, 80
  UNION ALL SELECT 'FINISHING', 'Finishing', 0, 0, 90
  UNION ALL SELECT 'SINGUR_SHED', 'Singur Shed', 0, 0, 100
) d
WHERE NOT EXISTS (
  SELECT 1 FROM jute_gl_dept_mst x
  WHERE x.co_id = c.co_id AND x.dept_code = d.dept_code
);

-- =============================================================================
-- VERIFICATION — run manually after applying
-- =============================================================================
--   SELECT co_id, COUNT(*) FROM jute_gl_row_mst GROUP BY co_id;    -- 16 per co
--   SELECT co_id, COUNT(*) FROM jute_gl_dept_mst GROUP BY co_id;   -- 10 per co
-- =============================================================================
-- ROLLBACK
-- =============================================================================
--   DROP TABLE jute_gain_loss_process_stock;
--   DROP TABLE jute_gain_loss_prod_dtl;
--   DROP TABLE jute_gain_loss_dtl;
--   DROP TABLE jute_gain_loss_hdr;
--   DROP TABLE jute_gl_dept_mst;
--   DROP TABLE jute_gl_row_mst;
-- =============================================================================
