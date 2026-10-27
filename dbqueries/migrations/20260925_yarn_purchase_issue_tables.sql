-- =============================================================================
-- Migration: Purchased yarn — Yarn Purchase, Yarn Issue/Return, godown view,
--            location + department on the winding stock count
-- =============================================================================
-- Date: 2026-09-25   Phase 1 of
-- docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md
-- TARGET: tenant DB (dev3 first, sls after phases 2-5 are deployed).
--
-- Four new tables carry the purchased-yarn ledger in KILOGRAMS at the mill's own
-- co/branch (the winding report's scope). No FKs, as on every jute_prod table.
-- Header statuses follow the standard lifecycle 21 draft -> 1 open ->
-- 20 pending -> 3 approved / 4 rejected / 6 cancelled.
--
-- Runner note: the run-migration skill splits on semicolons and skips chunks that start
-- with a comment marker, so every comment block ends with a bare semicolon line.
;

CREATE TABLE jute_yarn_purchase (
  yarn_purchase_id   INT PRIMARY KEY AUTO_INCREMENT,
  co_id              INT NOT NULL,
  branch_id          INT NOT NULL,
  tran_date          DATE NOT NULL,
  purchase_no        INT NULL,
  party_id           INT NULL,
  challan_no         VARCHAR(50) NULL,
  challan_date       DATE NULL,
  vehicle_no         VARCHAR(30) NULL,
  inward_id          INT NULL,
  status_id          INT NOT NULL DEFAULT 21,
  approval_level     INT NULL,
  remarks            VARCHAR(255) NULL,
  active             INT NOT NULL DEFAULT 1,
  updated_by         INT NULL,
  updated_date_time  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_yarn_purchase_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_yarn_purchase_status (status_id),
  INDEX idx_yarn_purchase_inward (inward_id)
);

CREATE TABLE jute_yarn_purchase_li (
  yarn_purchase_li_id INT PRIMARY KEY AUTO_INCREMENT,
  yarn_purchase_id    INT NOT NULL,
  item_id             INT NOT NULL,
  package_type        VARCHAR(20) NULL,
  no_of_packages      DECIMAL(12,3) NULL,
  wt_per_package      DECIMAL(10,3) NULL,
  weight_kg           DECIMAL(12,3) NOT NULL,
  rate_per_kg        DECIMAL(12,2) NULL,
  amount              DECIMAL(14,2) NULL,
  remarks             VARCHAR(255) NULL,
  active              INT NOT NULL DEFAULT 1,
  updated_by          INT NULL,
  updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_yarn_purchase_li_hdr (yarn_purchase_id),
  INDEX idx_yarn_purchase_li_item (item_id)
);

CREATE TABLE jute_yarn_issue (
  yarn_issue_id      INT PRIMARY KEY AUTO_INCREMENT,
  co_id              INT NOT NULL,
  branch_id          INT NOT NULL,
  tran_date          DATE NOT NULL,
  issue_no           INT NULL,
  issue_type         VARCHAR(10) NOT NULL DEFAULT 'ISSUE',
  dept_id            INT NOT NULL,
  status_id          INT NOT NULL DEFAULT 21,
  approval_level     INT NULL,
  remarks            VARCHAR(255) NULL,
  active             INT NOT NULL DEFAULT 1,
  updated_by         INT NULL,
  updated_date_time  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_yarn_issue_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_yarn_issue_dept (dept_id),
  INDEX idx_yarn_issue_status (status_id)
);

CREATE TABLE jute_yarn_issue_li (
  yarn_issue_li_id    INT PRIMARY KEY AUTO_INCREMENT,
  yarn_issue_id       INT NOT NULL,
  item_id             INT NOT NULL,
  yarn_purchase_li_id INT NOT NULL,
  no_of_packages      DECIMAL(12,3) NULL,
  weight_kg           DECIMAL(12,3) NOT NULL,
  remarks             VARCHAR(255) NULL,
  active              INT NOT NULL DEFAULT 1,
  updated_by          INT NULL,
  updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_yarn_issue_li_hdr (yarn_issue_id),
  INDEX idx_yarn_issue_li_lot (yarn_purchase_li_id),
  INDEX idx_yarn_issue_li_item (item_id)
);

ALTER TABLE jute_prod_winding_stock
  ADD COLUMN stock_loc VARCHAR(10) NOT NULL DEFAULT 'FLOOR' AFTER wastage_area_id,
  ADD COLUMN dept_id INT NULL AFTER stock_loc,
  ADD INDEX idx_winding_stock_loc_dept (stock_loc, dept_id);

CREATE OR REPLACE VIEW vw_yarn_godown_outstanding AS
SELECT
    pl.yarn_purchase_li_id,
    p.yarn_purchase_id,
    p.co_id,
    p.branch_id,
    p.tran_date,
    p.purchase_no,
    p.party_id,
    p.status_id                                                       AS purchase_status_id,
    pl.item_id,
    pl.package_type,
    pl.no_of_packages,
    pl.wt_per_package,
    pl.weight_kg                                                      AS received_kg,
    IFNULL(mv.issued_kg, 0)                                           AS issued_kg,
    IFNULL(mv.returned_kg, 0)                                         AS returned_kg,
    ROUND(pl.weight_kg - IFNULL(mv.issued_kg, 0) + IFNULL(mv.returned_kg, 0), 3) AS bal_kg,
    IFNULL(mv.issued_pkgs, 0)                                         AS issued_pkgs,
    IFNULL(mv.returned_pkgs, 0)                                       AS returned_pkgs,
    ROUND(IFNULL(pl.no_of_packages, 0) - IFNULL(mv.issued_pkgs, 0) + IFNULL(mv.returned_pkgs, 0), 3) AS bal_pkgs
FROM jute_yarn_purchase p
JOIN jute_yarn_purchase_li pl ON pl.yarn_purchase_id = p.yarn_purchase_id
LEFT JOIN (
    SELECT il.yarn_purchase_li_id,
           SUM(CASE WHEN i.issue_type = 'ISSUE'  THEN il.weight_kg ELSE 0 END)            AS issued_kg,
           SUM(CASE WHEN i.issue_type = 'RETURN' THEN il.weight_kg ELSE 0 END)            AS returned_kg,
           SUM(CASE WHEN i.issue_type = 'ISSUE'  THEN IFNULL(il.no_of_packages, 0) ELSE 0 END) AS issued_pkgs,
           SUM(CASE WHEN i.issue_type = 'RETURN' THEN IFNULL(il.no_of_packages, 0) ELSE 0 END) AS returned_pkgs
    FROM jute_yarn_issue_li il
    JOIN jute_yarn_issue i ON i.yarn_issue_id = il.yarn_issue_id
    WHERE i.active = 1 AND il.active = 1
      AND i.status_id NOT IN (4, 6, 21)
    GROUP BY il.yarn_purchase_li_id
) mv ON mv.yarn_purchase_li_id = pl.yarn_purchase_li_id
WHERE p.active = 1 AND pl.active = 1
  AND p.status_id NOT IN (4, 6, 21);

-- =============================================================================
-- NOTES
-- =============================================================================
-- * The view is deliberately WIDE (every purchase that physically arrived:
--   open / pending / approved) and exposes purchase_status_id so the issue
--   picker and the save-time guard narrow to YARN_PURCHASE_ISSUABLE_STATUSES
--   in src/juteProcurement/constants.py — the same shape as
--   vw_jute_stock_outstanding + JUTE_MR_ISSUABLE_STATUSES.
-- * An issue or return moves stock unless rejected, cancelled or still draft.
-- * jute_yarn_issue_li.yarn_purchase_li_id is NOT NULL: the lot is mandatory
--   (owner ruling 5, 2026-09-25).
-- * stock_loc on jute_prod_winding_stock is GODOWN or FLOOR, existing rows
--   default to FLOOR. dept_id is only meaningful for FLOOR rows.
-- * No triggers exist on jute_prod_winding_stock (checked on dev3 2026-09-25).
--
-- VERIFICATION
--   SHOW TABLES LIKE 'jute_yarn%';
--   SHOW COLUMNS FROM jute_prod_winding_stock;   -- stock_loc, dept_id present
--   SELECT * FROM vw_yarn_godown_outstanding LIMIT 1;   -- empty result, no error
--
-- ROLLBACK
--   DROP VIEW IF EXISTS vw_yarn_godown_outstanding;
--   ALTER TABLE jute_prod_winding_stock DROP INDEX idx_winding_stock_loc_dept,
--     DROP COLUMN dept_id, DROP COLUMN stock_loc;
--   DROP TABLE IF EXISTS jute_yarn_issue_li;
--   DROP TABLE IF EXISTS jute_yarn_issue;
--   DROP TABLE IF EXISTS jute_yarn_purchase_li;
--   DROP TABLE IF EXISTS jute_yarn_purchase;
-- =============================================================================
