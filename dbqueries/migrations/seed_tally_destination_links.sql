-- Tally sales export (Sales > Jute Tally Download): fixed "Destination" per receiving mill,
-- with a per-buyer override. Seeds tally_link_mst; no DDL.
--
--   link_type DESTINATION        ref_id = branch_mst.branch_id of the MILL's own FACTORY branch,
--                                row in the mill's company. Used by every raw-jute sale whose
--                                chain ROOT MR (juteTransfer) was received at that branch.
--   link_type DESTINATION_PARTY  ref_id = party_mst.party_id, row in the SELLING company.
--                                Overrides DESTINATION for sales to that buyer.
--   Unmapped sales keep today's value: the ship-to party branch address.
--
-- Entities are resolved by name, so the same script runs on dev3 and sls. Idempotent:
-- an existing (co, type, ref) row is left as it is (edit it in Masters > Tally Link).
-- Run with tools/run_sql_migration.py (pymysql); target tenant chosen at run time.

-- 1. Empire Jute factory -> "15/16 B.T. Road"
INSERT INTO tally_link_mst (co_id, link_type, ref_id, tally_name, active)
SELECT b.co_id, 'DESTINATION', b.branch_id, '15/16 B.T. Road', 1
FROM branch_mst b
JOIN co_mst c ON c.co_id = b.co_id
WHERE c.co_prefix = 'EJM'
  AND UPPER(b.branch_name) = 'FACTORY'
  AND NOT EXISTS (SELECT 1 FROM tally_link_mst t
                  WHERE t.co_id = b.co_id AND t.link_type = 'DESTINATION' AND t.ref_id = b.branch_id);

-- 2. Limelight factory -> its own branch address (what the sheet shows today for the final hop)
INSERT INTO tally_link_mst (co_id, link_type, ref_id, tally_name, active)
SELECT b.co_id, 'DESTINATION', b.branch_id, TRIM(REPLACE(b.branch_address1, '  ', ' ')), 1
FROM branch_mst b
JOIN co_mst c ON c.co_id = b.co_id
WHERE c.co_prefix = 'LCPL'
  AND UPPER(b.branch_name) = 'FACTORY'
  AND COALESCE(b.branch_address1, '') <> ''
  AND NOT EXISTS (SELECT 1 FROM tally_link_mst t
                  WHERE t.co_id = b.co_id AND t.link_type = 'DESTINATION' AND t.ref_id = b.branch_id);

-- 3. Sales to Aravali Vyapaar, from any selling company -> "Golabari"
--    (party_mst is per company; the name is spelt three ways on sls: ARAVALI / ARVALI / VYPAAR)
INSERT INTO tally_link_mst (co_id, link_type, ref_id, tally_name, active)
SELECT p.co_id, 'DESTINATION_PARTY', p.party_id, 'Golabari', 1
FROM party_mst p
WHERE p.active = 1
  AND UPPER(p.supp_name) REGEXP 'ARA?VALI'
  AND NOT EXISTS (SELECT 1 FROM tally_link_mst t
                  WHERE t.co_id = p.co_id AND t.link_type = 'DESTINATION_PARTY' AND t.ref_id = p.party_id);

-- Rollback:
-- DELETE FROM tally_link_mst WHERE link_type IN ('DESTINATION', 'DESTINATION_PARTY');
