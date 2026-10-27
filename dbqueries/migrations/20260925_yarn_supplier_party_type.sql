-- =============================================================================
-- Migration: 'Yarn Supplier' party type for the Yarn Purchase supplier picker
-- =============================================================================
-- Date: 2026-09-25 (owner ruling: narrow the Yarn Purchase supplier list to a
-- dedicated party type, the way Jute Supplier (3) narrows the jute flows).
-- TARGET: every tenant DB that runs Yarn Purchase (dev3, sls).
--
-- party_types_mst_id is not auto-increment, so the row takes MAX + 1.
-- The id is NOT hardcoded anywhere: the Yarn Purchase setup query resolves the
-- type by name (src/juteProcurement/constants.py YARN_SUPPLIER_PARTY_TYPE_NAME)
-- and party_mst.party_type_id is a set-like string ('{1}', '2,3', '{2,3,1}')
-- matched with FIND_IN_SET after stripping the braces, as elsewhere.
-- The Party master reads party_type_mst dynamically, so the new type appears in
-- its type picker with no frontend change. Idempotent via NOT EXISTS.
;

INSERT INTO party_type_mst (party_types_mst_id, party_types_mst_name, party_types_mst_prefix, module_id)
SELECT (SELECT COALESCE(MAX(x.party_types_mst_id), 0) + 1 FROM party_type_mst x), 'Yarn Supplier', 'yarnsupp',
       (SELECT module_mst_id FROM module_mst WHERE module_name = 'Jute Procurement' AND active = 1 ORDER BY module_mst_id LIMIT 1)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM party_type_mst WHERE party_types_mst_name = 'Yarn Supplier');

-- =============================================================================
-- VERIFICATION
--   SELECT * FROM party_type_mst WHERE party_types_mst_name = 'Yarn Supplier';
-- After applying, tag the yarn suppliers on the Party master (add 'Yarn
-- Supplier' to their party types) — until then the Yarn Purchase supplier
-- picker is empty by design.
-- ROLLBACK
--   DELETE FROM party_type_mst WHERE party_types_mst_name = 'Yarn Supplier';
-- =============================================================================
