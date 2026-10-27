-- Add standard Indian entity types to entity_type_mst (party master "Entity Type" dropdown).
-- Tenant-scoped table; run per tenant DB. Rerunnable (INSERT IGNORE on explicit PK).
-- id 1 = 'Private Limited Company' already exists on all tenants and is left untouched.
-- entity_type_name is varchar(30) -- all names below are <= 29 chars.
--
-- ROLLBACK:
--   DELETE FROM entity_type_mst WHERE entity_type_id BETWEEN 2 AND 13;

INSERT IGNORE INTO entity_type_mst (entity_type_id, entity_type_name) VALUES
  (2,  'Proprietorship'),
  (3,  'Partnership Firm'),
  (4,  'Limited Liability Partnership'),
  (5,  'Public Limited Company'),
  (6,  'Hindu Undivided Family'),
  (7,  'Trust'),
  (8,  'Society'),
  (9,  'Co-operative Society'),
  (10, 'Government'),
  (11, 'Association of Persons'),
  (12, 'Body of Individuals'),
  (13, 'Foreign Company');
