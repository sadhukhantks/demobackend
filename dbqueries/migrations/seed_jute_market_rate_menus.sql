-- =============================================================================
-- Migration: Jute Market Rate menus (under Jute Procurement)
-- =============================================================================
-- Date: 2026-09-23
-- TARGET: tenant DB only (dev3 first; other tenants once BE+FE deploy there).
-- Run AFTER create_jute_market_rate.sql.
--
--   'Jute Market Rate'        -> jutePurchase/marketRate
--   'Market Rate Source'      -> jutePurchase/marketRateSource
--   'Jute Rate Difference'    -> jutePurchase/rateDiff
--
-- Role grants mirror each role's access on 'jutePurchase/po'.
-- Runner note: each comment block is terminated by a lone ';'.
;

INSERT INTO menu_mst (menu_name, menu_path, active, menu_parent_id, menu_type_id, menu_icon, module_mst_id, order_by)
SELECT v.menu_name, v.menu_path, 1, p.menu_id, p.menu_type_id, NULL, p.module_mst_id, v.order_by
FROM (SELECT menu_id, menu_type_id, module_mst_id FROM menu_mst
      WHERE menu_path = 'jutePurchase' AND menu_parent_id IS NULL LIMIT 1) p
CROSS JOIN (
    SELECT 'Jute Market Rate' AS menu_name, 'jutePurchase/marketRate' AS menu_path, 50 AS order_by
    UNION ALL SELECT 'Market Rate Source', 'jutePurchase/marketRateSource', 51
    UNION ALL SELECT 'Jute Rate Difference', 'jutePurchase/rateDiff', 52
) v
WHERE NOT EXISTS (SELECT 1 FROM (SELECT menu_path FROM menu_mst) m WHERE m.menu_path = v.menu_path);

-- role grants cloned from Jute Purchase Order
;
INSERT INTO role_menu_map (role_id, menu_id, access_type_id, updated_by, updated_date_time)
SELECT r.role_id, m.menu_id, r.access_type_id, 1, NOW()
FROM (
    SELECT rmm.role_id, MAX(rmm.access_type_id) AS access_type_id
    FROM role_menu_map rmm
    JOIN menu_mst po ON po.menu_id = rmm.menu_id AND po.menu_path = 'jutePurchase/po'
    GROUP BY rmm.role_id
) r
CROSS JOIN (SELECT menu_id FROM menu_mst
            WHERE menu_path IN ('jutePurchase/marketRate', 'jutePurchase/marketRateSource', 'jutePurchase/rateDiff')) m
WHERE NOT EXISTS (SELECT 1 FROM (SELECT role_id, menu_id FROM role_menu_map) x
                  WHERE x.role_id = r.role_id AND x.menu_id = m.menu_id);

-- VERIFICATION
;
SELECT menu_id, menu_name, menu_path, menu_parent_id, order_by FROM menu_mst
WHERE menu_path IN ('jutePurchase/marketRate', 'jutePurchase/marketRateSource', 'jutePurchase/rateDiff');

-- =============================================================================
-- ROLLBACK:
-- DELETE FROM role_menu_map WHERE menu_id IN (SELECT menu_id FROM (SELECT menu_id FROM menu_mst
--   WHERE menu_path IN ('jutePurchase/marketRate','jutePurchase/marketRateSource','jutePurchase/rateDiff')) x);
-- DELETE FROM menu_mst WHERE menu_path IN
--   ('jutePurchase/marketRate','jutePurchase/marketRateSource','jutePurchase/rateDiff');
-- =============================================================================
;
