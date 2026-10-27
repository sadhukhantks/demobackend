-- Jute Production — Waste Entry: waste type is now an ITEM, not a fixed app enum.
-- The stored value becomes item_mst.item_id of an item whose group carries
-- item_grp_mst.item_type_id = 3 ('jute wastage'), replacing the VARCHAR enum code.
-- Target tenant DBs: dev3 + sls (both tables verified EMPTY, so no data mapping).
-- Rollback: ALTER TABLE jute_prod_waste_entry CHANGE COLUMN item_id waste_type_code VARCHAR(20) NOT NULL
ALTER TABLE jute_prod_waste_entry CHANGE COLUMN waste_type_code item_id INT NOT NULL;
