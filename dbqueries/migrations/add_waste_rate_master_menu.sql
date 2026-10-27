INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT 'Waste Rate Master', 'juteProduction/masters/wasteRate', 1,
       sib.menu_parent_id, sib.menu_type_id, 'payments', sib.module_mst_id, sib.order_by + 1
FROM menu_mst sib
WHERE sib.menu_path = 'juteProduction/masters/wastageArea'
  AND NOT EXISTS (
      SELECT 1 FROM (SELECT menu_id FROM menu_mst WHERE menu_path = 'juteProduction/masters/wasteRate') x
  )
LIMIT 1;

INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by)
SELECT rm.role_id, new_m.menu_id, rm.access_type_id, rm.updated_by
FROM role_menu_map rm
JOIN menu_mst sib ON sib.menu_id = rm.menu_id AND sib.menu_path = 'juteProduction/masters/wastageArea'
JOIN menu_mst new_m ON new_m.menu_path = 'juteProduction/masters/wasteRate'
WHERE NOT EXISTS (
    SELECT 1 FROM (
        SELECT rm2.role_id, rm2.menu_id FROM role_menu_map rm2
        JOIN menu_mst m2 ON m2.menu_id = rm2.menu_id
        WHERE m2.menu_path = 'juteProduction/masters/wasteRate'
    ) y
    WHERE y.role_id = rm.role_id
);

-- ===========================================================================
-- Menu: Waste Rate Master                                     (2026-08-10)
--
-- COMMENT PLACEMENT: the project migration runner splits on ';' and skips any
-- chunk starting with '--', so a comment above a statement swallows it. All
-- commentary sits after the final ';'. Do not move it to the top.
--
-- Deliberately tenant-agnostic: every value that differs per tenant
-- (menu_parent_id, module_mst_id, order_by, and the whole role mapping) is
-- copied from the sibling 'Wastage Area Master' row rather than hardcoded, so
-- the same script runs unchanged on dev3, sls and every other tenant. dev3
-- parents at 768 and sls at 769 — hardcoding either would silently misfile the
-- menu on the other.
--
-- Roles are mirrored from the sibling master: whoever may open Wastage Area
-- Master may open this one, at the SAME access_type_id. That is the intended
-- policy (both are jute-production masters maintained by the same people), not
-- an accident of copying.
--
-- Both statements are idempotent (NOT EXISTS guards), so a re-run is a no-op.
-- The derived subqueries in the guards are required: MySQL refuses to read the
-- target table of a DML statement directly in its own subquery (error 1093).
--
-- PREREQUISITE: the sibling row must exist. If a tenant lacks
-- 'juteProduction/masters/wastageArea', this script inserts NOTHING and exits
-- cleanly — run the wastage-area menu seed there first.
--
-- ROLLBACK
-- DELETE rm FROM role_menu_map rm JOIN menu_mst m ON m.menu_id = rm.menu_id
--   WHERE m.menu_path = 'juteProduction/masters/wasteRate';
-- DELETE FROM menu_mst WHERE menu_path = 'juteProduction/masters/wasteRate';
-- ===========================================================================
