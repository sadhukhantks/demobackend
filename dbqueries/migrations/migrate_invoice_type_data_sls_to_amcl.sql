-- Migration: seed amcl invoice type master — Regular only (per user -- amcl is a machinery co,
-- jute invoice types not applicable). Tables exist in amcl and are empty.
-- Date: 2026-08-06
-- invoice_type_id 1 = Regular, matching sls/dev3 (ids are code-referenced).
-- co_map generated for amcl's own company co_id 30 (AMCL MACHINERY LIMITED).
--
-- ROLLBACK:
-- DELETE FROM invoice_type_co_map WHERE co_id = 30
-- DELETE FROM invoice_type_mst WHERE invoice_type_id = 1

INSERT INTO invoice_type_mst (invoice_type_id, invoice_type_name, invoice_type_remarks) VALUES
(1, 'Regular', NULL);

INSERT INTO invoice_type_co_map (co_id, invoice_type_id, active) VALUES
(30, 1, 1);
