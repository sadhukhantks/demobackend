-- Spreader dual-quality rolls: up to 2 qualities + integer ratio per entry
-- Also replays dev3 band_edge drift onto sls (spreader_machine_attr)
-- Rollback (run manually, statements separated by newlines):
--   ALTER TABLE spreader_prod_entry DROP KEY idx_spe_item2
--   ALTER TABLE spreader_prod_entry DROP COLUMN ratio_2
--   ALTER TABLE spreader_prod_entry DROP COLUMN ratio_1
--   ALTER TABLE spreader_prod_entry DROP COLUMN item_id_2

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN item_id_2 INT NULL AFTER item_id',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'item_id_2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN ratio_1 INT NOT NULL DEFAULT 1 AFTER item_id_2',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'ratio_1');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD COLUMN ratio_2 INT NULL AFTER ratio_1',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND COLUMN_NAME = 'ratio_2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_prod_entry ADD KEY idx_spe_item2 (item_id_2)',
  'SELECT 1')
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_prod_entry' AND INDEX_NAME = 'idx_spe_item2');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE spreader_machine_attr ADD COLUMN band_edge_1 DECIMAL(10,3) NULL, ADD COLUMN band_edge_2 DECIMAL(10,3) NULL, ADD COLUMN band_edge_3 DECIMAL(10,3) NULL, ADD COLUMN band_edge_4 DECIMAL(10,3) NULL, ADD COLUMN band_edge_5 DECIMAL(10,3) NULL',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'spreader_machine_attr' AND COLUMN_NAME = 'band_edge_1');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
