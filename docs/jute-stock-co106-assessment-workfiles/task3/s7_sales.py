from lib import *
from collections import Counter
c = conn()
print("=== SALES INVOICE HDR ===")
print("old co106 all:", q(c,"SELECT COUNT(*) n FROM vowsls.invoice_hdr WHERE company_id=106")[0]["n"])
print("old co106 <=cut (or null date):", q(c,f"SELECT COUNT(*) n, SUM(invoice_date IS NULL) dn FROM vowsls.invoice_hdr WHERE company_id=106 AND (invoice_date<='{CUT}' OR invoice_date IS NULL)")[0])
print("sls b87 type5:", q(c,"SELECT COUNT(*) n FROM sls.sales_invoice WHERE branch_id=87 AND invoice_type=5")[0]["n"])
print("mapped via _map_invoices:", q(c,"SELECT COUNT(*) n FROM vowsls_to_sls_transfer._map_invoices mi JOIN vowsls.invoice_hdr h ON h.invoice_id=mi.old_invoice_id WHERE h.company_id=106")[0]["n"])

miss = q(c, f"""
SELECT h.invoice_id, h.invoice_date, h.branch_id, h.status, h.is_active, h.invoice_type, h.invoice_no_string,
       h.invoice_amount, h.mr_id,
       (SELECT COUNT(*) FROM vowsls.invoice_line_items li WHERE li.invoice_id=h.invoice_id) n_lines,
       (SELECT COALESCE(SUM(li.sales_weight),0) FROM vowsls.invoice_line_items li WHERE li.invoice_id=h.invoice_id AND li.is_active=1) qtl,
       (SELECT mi.new_invoice_id FROM vowsls_to_sls_transfer._map_invoices mi WHERE mi.old_invoice_id=h.invoice_id) newid
FROM vowsls.invoice_hdr h
WHERE h.company_id=106 AND (h.invoice_date<='{CUT}' OR h.invoice_date IS NULL)
  AND NOT EXISTS(SELECT 1 FROM vowsls_to_sls_transfer._map_invoices mi JOIN sls.sales_invoice s ON s.invoice_id=mi.new_invoice_id
                 WHERE mi.old_invoice_id=h.invoice_id)
ORDER BY h.invoice_id""")
print("MISSING invoices in sls:", len(miss), "qtl:", round(sum(r["qtl"] or 0 for r in miss),2))
csvout("sales_hdr_missing_in_sls.csv", miss)
print("  by status/is_active:", Counter((str(r["status"]),str(r["is_active"])) for r in miss))

ext = q(c, f"""
SELECT s.invoice_id, s.invoice_no, s.invoice_date, s.status_id, s.active, s.invoice_type, s.invoice_amount,
       (SELECT COUNT(*) FROM sls.sales_invoice_dtl d WHERE d.invoice_id=s.invoice_id) n_lines,
       (SELECT COALESCE(SUM(d.sales_weight),0) FROM sls.sales_invoice_dtl d WHERE d.invoice_id=s.invoice_id) qtl,
       (SELECT mi.old_invoice_id FROM vowsls_to_sls_transfer._map_invoices mi WHERE mi.new_invoice_id=s.invoice_id) oldid
FROM sls.sales_invoice s
WHERE s.branch_id=87 AND s.invoice_type=5 AND (s.invoice_date<='{CUT}' OR s.invoice_date IS NULL)
  AND NOT EXISTS(SELECT 1 FROM vowsls_to_sls_transfer._map_invoices mi JOIN vowsls.invoice_hdr h ON h.invoice_id=mi.old_invoice_id
                 WHERE mi.new_invoice_id=s.invoice_id AND h.company_id=106 AND (h.invoice_date<='{CUT}' OR h.invoice_date IS NULL))
ORDER BY s.invoice_id""")
print("EXTRA invoices in sls:", len(ext), "qtl:", round(sum(r["qtl"] or 0 for r in ext),2))
csvout("sales_hdr_extra_in_sls.csv", ext)

print("\n=== SALES LINES ===")
print("old lines under in-scope old invoices:", q(c,f"SELECT COUNT(*) n, SUM(li.is_active=1) act FROM vowsls.invoice_line_items li JOIN vowsls.invoice_hdr h ON h.invoice_id=li.invoice_id WHERE h.company_id=106 AND (h.invoice_date<='{CUT}' OR h.invoice_date IS NULL)")[0])
lmiss = q(c, f"""
SELECT li.invoice_line_item_id, li.invoice_id, h.invoice_date, h.status hstat, h.is_active hact, li.is_active lact,
       li.mr_line_id, li.sales_weight qtl, li.sales_bale, li.sales_drum, li.quantity, li.rate, li.item_id, li.quality_id,
       (SELECT ml.new_invoice_line_item_id FROM vowsls_to_sls_transfer._map_invoice_lines ml WHERE ml.old_invoice_line_item_id=li.invoice_line_item_id) newid
FROM vowsls.invoice_line_items li JOIN vowsls.invoice_hdr h ON h.invoice_id=li.invoice_id
WHERE h.company_id=106 AND (h.invoice_date<='{CUT}' OR h.invoice_date IS NULL)
  AND NOT EXISTS(SELECT 1 FROM vowsls_to_sls_transfer._map_invoice_lines ml JOIN sls.sales_invoice_dtl d ON d.invoice_line_item_id=ml.new_invoice_line_item_id
                 WHERE ml.old_invoice_line_item_id=li.invoice_line_item_id)
ORDER BY li.invoice_line_item_id""")
print("MISSING sales lines:", len(lmiss), "qtl:", round(sum(r["qtl"] or 0 for r in lmiss),2))
csvout("sales_line_missing_in_sls.csv", lmiss)
print("  by hdr status/line active:", Counter((str(r["hstat"]),str(r["hact"]),str(r["lact"])) for r in lmiss))
print("  approved-hdr active-line qtl:", round(sum(r["qtl"] or 0 for r in lmiss if r["hstat"]==3 and r["lact"]==1),2))

lext = q(c, f"""
SELECT d.invoice_line_item_id, d.invoice_id, s.invoice_date, s.status_id, d.jute_mr_li_id, d.sales_weight, d.quantity, d.rate, d.item_id,
       (SELECT ml.old_invoice_line_item_id FROM vowsls_to_sls_transfer._map_invoice_lines ml WHERE ml.new_invoice_line_item_id=d.invoice_line_item_id) oldid
FROM sls.sales_invoice_dtl d JOIN sls.sales_invoice s ON s.invoice_id=d.invoice_id
WHERE s.branch_id=87 AND s.invoice_type=5 AND (s.invoice_date<='{CUT}' OR s.invoice_date IS NULL)
  AND NOT EXISTS(SELECT 1 FROM vowsls_to_sls_transfer._map_invoice_lines ml JOIN vowsls.invoice_line_items li ON li.invoice_line_item_id=ml.old_invoice_line_item_id
                 JOIN vowsls.invoice_hdr h ON h.invoice_id=li.invoice_id
                 WHERE ml.new_invoice_line_item_id=d.invoice_line_item_id AND h.company_id=106)
ORDER BY d.invoice_line_item_id""")
print("EXTRA sales lines in sls:", len(lext), "qtl:", round(sum(r["sales_weight"] or 0 for r in lext),2))
csvout("sales_line_extra_in_sls.csv", lext)
