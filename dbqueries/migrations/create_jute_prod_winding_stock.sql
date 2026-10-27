CREATE TABLE jute_prod_winding_stock (
  winding_stock_id  INT PRIMARY KEY AUTO_INCREMENT,
  co_id             INT NOT NULL,
  branch_id         INT NOT NULL,
  tran_date         DATE NOT NULL,
  bucket            VARCHAR(20) NOT NULL,
  item_id           INT NULL,
  package_label     VARCHAR(60) NOT NULL,
  wastage_area_id   INT NULL,
  no_of_samples     DECIMAL(12,3) NOT NULL,
  wt_per_sample     DECIMAL(10,3) NOT NULL,
  remarks           VARCHAR(255) NULL,
  active            INT NOT NULL DEFAULT 1,
  updated_by        INT NULL,
  updated_date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_winding_stock_co_branch_date (co_id, branch_id, tran_date),
  INDEX idx_winding_stock_item (item_id),
  INDEX idx_winding_stock_area (wastage_area_id)
);

CREATE TABLE jute_prod_winding_stock_lock (
  winding_stock_lock_id INT PRIMARY KEY AUTO_INCREMENT,
  co_id               INT NOT NULL,
  branch_id           INT NULL,
  tran_date           DATE NOT NULL,
  is_locked           TINYINT NOT NULL DEFAULT 1,
  processed_by        INT NULL,
  processed_date_time TIMESTAMP NULL,
  active              TINYINT NOT NULL DEFAULT 1,
  updated_by          INT NULL,
  updated_date_time   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_wstocklock_unit (co_id, tran_date)
);

-- Rollback:
--   DROP TABLE jute_prod_winding_stock_lock;
--   DROP TABLE jute_prod_winding_stock;
--
-- No FKs and no UNIQUE key on jute_prod_winding_stock, both deliberate:
--   * The module carries no FKs on production tables (see jute_prod_waste_entry).
--   * MySQL treats NULLs as distinct, so a UNIQUE over the nullable item_id /
--     wastage_area_id would not catch the duplicate lines that matter. The
--     duplicate check lives in the create/edit handler instead.
-- stock_mt is intentionally absent: MT = no_of_samples * wt_per_sample / 1000,
-- computed at read so a corrected weight fixes history.
