-- Tenant parity fixes found by tenant-schema-check sls vs dev3 (2026-08-16). Rerunnable, guarded.
-- 1 sales_order_govtskg.mode_of_transport + sales_invoice_govtskg.mode_of_transport (add_govtskg_mode_of_transport.sql was half-applied on sls)
-- 2 sales_invoice_dtl perf indexes idx_sid_delivery_order_dtl_id + idx_sid_sales_order_dtl_id (add_sales_invoice_dtl_indexes.sql)
-- 3 sales_invoice FK fk_sales_invoice_bank_detail on bank_detail_id (add_bank_detail_id_to_sales_invoice.sql)
-- Rollback (statements one per line, no semicolons here):
--   ALTER TABLE sales_invoice DROP FOREIGN KEY fk_sales_invoice_bank_detail
--   ALTER TABLE sales_invoice_dtl DROP INDEX idx_sid_sales_order_dtl_id
--   ALTER TABLE sales_invoice_dtl DROP INDEX idx_sid_delivery_order_dtl_id
--   ALTER TABLE sales_invoice_govtskg DROP COLUMN mode_of_transport
--   ALTER TABLE sales_order_govtskg DROP COLUMN mode_of_transport

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE sales_order_govtskg ADD COLUMN mode_of_transport VARCHAR(20) NULL AFTER loading_point',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_order_govtskg' AND COLUMN_NAME = 'mode_of_transport');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE sales_invoice_govtskg ADD COLUMN mode_of_transport VARCHAR(20) NULL AFTER loading_point',
  'SELECT 1')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_invoice_govtskg' AND COLUMN_NAME = 'mode_of_transport');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE sales_invoice_dtl ADD INDEX idx_sid_delivery_order_dtl_id (delivery_order_dtl_id)',
  'SELECT 1')
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_invoice_dtl' AND INDEX_NAME = 'idx_sid_delivery_order_dtl_id');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE sales_invoice_dtl ADD INDEX idx_sid_sales_order_dtl_id (sales_order_dtl_id)',
  'SELECT 1')
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_invoice_dtl' AND INDEX_NAME = 'idx_sid_sales_order_dtl_id');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;

SET @ddl = (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE sales_invoice ADD CONSTRAINT fk_sales_invoice_bank_detail FOREIGN KEY (bank_detail_id) REFERENCES bank_details_mst(bank_detail_id)',
  'SELECT 1')
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_invoice' AND CONSTRAINT_NAME = 'fk_sales_invoice_bank_detail');
PREPARE s FROM @ddl;
EXECUTE s;
DEALLOCATE PREPARE s;
