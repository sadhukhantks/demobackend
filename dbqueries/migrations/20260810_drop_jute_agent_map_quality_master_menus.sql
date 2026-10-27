-- 20260810_drop_jute_agent_map_quality_master_menus.sql
-- Remove deprecated portal menus 'Jute Agent Map' (masters/juteAgentMap) and
-- 'Jute Quality Master' (masters/juteQualityMaster) from all tenant DBs.
-- Code (FE pages + BE routers) removed in the same change set.
-- vowconsole3.portal_menu_mst has NO rows for these menus (verified 2026-08-10)
-- so only tenant menu_mst + role_menu_map are touched.
-- Targets: sls, dev3, vownjm, amcl (all tenant DBs that exist).
-- NOTE: menu 'Jute Quality Check' (jutePurchase/materialInspection) is a
-- LIKE-pattern false positive and must NOT be deleted.

DELETE rm FROM role_menu_map rm
INNER JOIN menu_mst m ON m.menu_id = rm.menu_id
WHERE m.url IN ('masters/juteAgentMap', 'masters/juteQualityMaster');

DELETE FROM menu_mst
WHERE url IN ('masters/juteAgentMap', 'masters/juteQualityMaster');

-- Rollback: restore from the pre-delete row dump written by the runner to
-- scratchpad menu_removal_rollback_<db>.sql (menu_mst rows were menu_id 717/721,
-- parent 114, active 1 in all four tenants).
