UPDATE state_mst /* ANDHRA PRADESH carries the pre-bifurcation GST state code 28. Post-2014 the codes are 36 = TELANGANA (already correct here) and 37 = ANDHRA PRADESH. Guarded on both the name and the old value, so a re-run is a no-op and a tenant already on 37 is untouched. */
SET state_code = '37'
WHERE state = 'ANDHRA PRADESH'
  AND state_code = '28'
  AND NOT EXISTS (SELECT 1 FROM (SELECT state_id FROM state_mst WHERE state_code = '37') AS taken);

-- Migration: 20260811_fix_state_mst_andhra_pradesh_gst_code.sql
-- Target DBs: sls, dev3, amcl (every tenant whose state_mst still shows ANDHRA PRADESH = 28)
--
-- WHY
-- Nothing could resolve an Andhra Pradesh GSTIN to a state: supplier branches store
-- GST state code 37, and state_mst had no row with that code, so gst_no -> state_code
-- lookups returned NULL (and a careless mapping would have landed on TELANGANA,
-- flipping intra-state purchases to IGST). The rest of the data already assumes 37:
-- on sls, 1,926 sales_invoice rows carry shipping_state_code 37 for AP while the
-- master said 28. This makes the master agree with them.
--
-- BLAST RADIUS CHECKED
-- state_code is read by src/masters/query.py (branch master), src/sales/query.py
-- (invoice co/branch state code) and the sales-invoice intra/inter derivation in the
-- frontend. Those compare state_code against sales_invoice.shipping_state_code /
-- billing_state_code, which are GST codes too -- so this change removes a mismatch
-- rather than creating one. GST computation itself (calculate_gst_amounts) compares
-- state_id, not state_code, and is unaffected.
--
-- ROLLBACK
--   UPDATE state_mst SET state_code = '28' WHERE state = 'ANDHRA PRADESH' AND state_code = '37'
