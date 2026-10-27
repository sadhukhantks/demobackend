-- =============================================================================
-- Menu entry: Jute SQC -> Std Moisture Master (juteSQC/stdMoisture)
--
-- Tenant-agnostic: the parent is resolved by menu_path ('juteSQC'), so the same
-- script works on dev3 (parent 781), sls (parent 784) and any other tenant.
-- Re-runnable: menu_name is the natural key and every insert is NOT EXISTS
-- guarded (menu_mst has no unique key on menu_name).
--
-- Section 2 clones the role grants of the sibling 'Breaker Card SQC' menu, so
-- exactly the roles that can already reach the carding SQC pages can reach the
-- master that now gates them. Tenants differ here (dev3 grants 1 role, sls 9) —
-- cloning keeps each tenant's own answer.
--
-- NOTE: this is the TENANT half (menu_mst + role_menu_map). The vowconsole3
-- template row is in add_jute_sqc_std_moisture_portal_menu.sql.
--
-- Target: tenant DBs (dev3, sls, and every other tenant BEFORE the code deploys)
--
-- ROLLBACK:
--   DELETE FROM role_menu_map WHERE menu_id IN
--       (SELECT menu_id FROM (SELECT menu_id FROM menu_mst
--         WHERE menu_name = 'Std Moisture Master') x);
--   DELETE FROM menu_mst WHERE menu_name = 'Std Moisture Master';
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. The menu row, under the tenant's own 'Jute SQC' parent
-- ---------------------------------------------------------------------------
INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by, report)
SELECT 'Std Moisture Master', 'juteSQC/stdMoisture', 1, p.menu_id, p.menu_type_id, NULL, p.module_mst_id, 1, NULL
FROM menu_mst p
WHERE p.menu_path = 'juteSQC'
  AND p.menu_parent_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM (SELECT menu_name FROM menu_mst) m
      WHERE m.menu_name = 'Std Moisture Master'
  )
LIMIT 1;

-- ---------------------------------------------------------------------------
-- 2. Role grants — cloned from the sibling 'Breaker Card SQC' menu
-- ---------------------------------------------------------------------------
-- role_menu_map.updated_by is NOT NULL with no default, so it is carried over
-- from the source grant rather than defaulted.
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT src.role_id, tgt.menu_id, src.access_type_id, src.updated_by
FROM (
        SELECT rm.role_id, rm.access_type_id, rm.updated_by
        FROM role_menu_map rm
        INNER JOIN menu_mst sm ON sm.menu_id = rm.menu_id
        WHERE sm.menu_path = 'juteSQC/breakerCard'
     ) src
CROSS JOIN (
        SELECT menu_id FROM menu_mst WHERE menu_path = 'juteSQC/stdMoisture' LIMIT 1
     ) tgt
WHERE NOT EXISTS (
        SELECT 1
        FROM (SELECT role_id, menu_id FROM role_menu_map) x
        WHERE x.role_id = src.role_id
          AND x.menu_id = tgt.menu_id
     );
