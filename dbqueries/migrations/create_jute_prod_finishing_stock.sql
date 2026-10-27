-- Migration: Finishing Stock — daily physical count of pressed bales and loose
--            finishing WIP (cuts / bundles at each stage).
-- Date: 2026-09-17
-- Spec: docs/superpowers/specs/2026-09-17-finishing-stock-design.md §2
-- Applies to: tenant DBs (dev3 first; sls before the FE deploy).
--
-- A flat counted-snapshot table like jute_prod_winding_stock — no header/detail,
-- no approval workflow, no day lock. One row per counted lot; several rows per
-- (stage, quality) are legitimate (the shop-floor sheet lists 19, 45, 27, 2 …)
-- and the report sums them.
-- APPLIED: dev3 2026-09-17; sls 2026-09-17.

CREATE TABLE jute_prod_finishing_stock (
  finishing_stock_id     INT AUTO_INCREMENT PRIMARY KEY,
  co_id                  INT NOT NULL,
  branch_id              INT NOT NULL,
  tran_date              DATE NOT NULL,
  bucket                 VARCHAR(10) NOT NULL,           -- 'BALE' | 'LOOSE'
  finishing_quality_id   INT NOT NULL,                   -- jute_prod_finishing_quality (item = identity; quality_type 1 hessian / 2 sacking)
  stage                  VARCHAR(20) NULL,               -- LOOSE only; constants.FINISHING_STOCK_STAGES code or NULL
  work_by                TINYINT NOT NULL DEFAULT 0,     -- 0 Dept, 1 Contract (LOOSE only)
  owner_co_id            INT NULL,                       -- BALE only; co_mst.co_id; NULL = own company
  qty                    DECIMAL(12,3) NOT NULL,         -- bales (BALE) / cuts, bundles, pcs (LOOSE)
  qty_uom                VARCHAR(10) NOT NULL,           -- 'BALE' | 'CUTS' | 'BDL' | 'PCS'
  wt_per_unit_kg         DECIMAL(10,3) NULL,             -- BALE: kg per bale (default = quality std_bale_weight, editable per line)
  wt_kg                  DECIMAL(14,3) NULL,             -- LOOSE: weighed total kg
  remarks                VARCHAR(255) NULL,
  active                 TINYINT NOT NULL DEFAULT 1,
  updated_by             INT NULL,
  updated_date_time      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_finstock_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_finstock_quality (finishing_quality_id),
  INDEX idx_finstock_owner (owner_co_id)
);

-- =============================================================================
-- ROLLBACK:
--   DROP TABLE jute_prod_finishing_stock;
-- =============================================================================
--
-- total_kg is deliberately NOT a column: it is wt_kg when not NULL, else
-- qty * wt_per_unit_kg, computed at read — so correcting a weight fixes history
-- instead of stranding a derived value. MT = kg / 1000 at 3 dp, also at read.
--
-- No FKs (the jute production tables carry none) and no UNIQUE key (duplicate
-- (stage, quality) lines are lots, not mistakes). Audit is by trigger, so there
-- are no created_by / created_date columns.
