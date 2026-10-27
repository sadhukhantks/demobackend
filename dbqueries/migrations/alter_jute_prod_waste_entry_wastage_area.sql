-- Jute Production — attach the Wastage Area to each waste booking.
-- Nullable in the DB so pre-existing waste rows stay valid. The API requires it
-- on every create/edit from here on (guarded against wastage_area_mst.dept_id).
-- NOTE keep semicolons out of these comments -- the migration runner splits on them
-- Run AFTER create_wastage_area_mst.sql.
-- Target tenant DBs: dev3, sls.
-- Rollback:
--   ALTER TABLE jute_prod_waste_entry DROP FOREIGN KEY fk_waste_area
--   ALTER TABLE jute_prod_waste_entry DROP COLUMN wastage_area_id
ALTER TABLE jute_prod_waste_entry
    ADD COLUMN wastage_area_id INT NULL AFTER dept_id,
    ADD KEY idx_waste_area (wastage_area_id),
    ADD CONSTRAINT fk_waste_area FOREIGN KEY (wastage_area_id) REFERENCES wastage_area_mst (wastage_area_id);
