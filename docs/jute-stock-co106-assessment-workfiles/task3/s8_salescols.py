from lib import *
from collections import Counter
c = conn()
H = q(c, f"""
SELECT h.invoice_id oid, s.invoice_id sid, h.invoice_date od, s.invoice_date sd, h.status ost, s.status_id sst,
       h.is_active oact, s.active sact, h.invoice_type otyp, s.invoice_type styp, h.branch_id ob, s.branch_id sb,
       h.invoice_amount oamt, s.invoice_amount samt, h.tax_amount otax, s.tax_amount stax,
       h.customer_id ocust, s.party_id spart, h.vehicle_no ovn, s.vehicle_no svn,
       h.invoice_no_string oins, s.invoice_no sino, h.mr_id, h.challan_no ocn, s.challan_no scn,
       h.challan_date ocd, s.challan_date scd, h.round_off oro, s.round_off sro, h.contract_no octr, s.contract_no sctr,
       h.eway_bill_no oew, s.eway_bill_no sew, h.unit_conversion ouc
FROM vowsls.invoice_hdr h JOIN vowsls_to_sls_transfer._map_invoices mi ON mi.old_invoice_id=h.invoice_id
JOIN sls.sales_invoice s ON s.invoice_id=mi.new_invoice_id
WHERE h.company_id=106""")
print("invoice hdr pairs:", len(H))
def n(v):
    try: return None if v is None else round(float(v),3)
    except: return None
def st(v):
    if v is None: return None
    v=str(v).strip(); return None if v=="" else v
hc = {
 "invoice_date": lambda r:(r["od"], r["sd"]),
 "status":       lambda r:(n(r["ost"]), n(r["sst"])),
 "is_active vs active": lambda r:(n(r["oact"]), n(r["sact"])),
 "invoice_type (2 vs 5)": lambda r:(st(r["otyp"]), st(r["styp"])),
 "branch_id":    lambda r:(n(r["ob"]), n(r["sb"])),
 "invoice_amount": lambda r:(n(r["oamt"]), n(r["samt"])),
 "tax_amount":   lambda r:(n(r["otax"]), n(r["stax"])),
 "customer_id vs party_id": lambda r:(n(r["ocust"]), n(r["spart"])),
 "vehicle_no":   lambda r:(st(r["ovn"]), st(r["svn"])),
 "challan_no":   lambda r:(st(r["ocn"]), st(r["scn"])),
 "challan_date": lambda r:(r["ocd"], r["scd"]),
 "round_off":    lambda r:(n(r["oro"]), n(r["sro"])),
 "contract_no":  lambda r:(n(r["octr"]), n(r["sctr"])),
 "eway_bill_no": lambda r:(st(r["oew"]), st(r["sew"])),
}
det=[]
for k,f in hc.items():
    bad=0;bn=0
    for r in H:
        a,b=f(r)
        if a is None and b is None: bn+=1; continue
        if a!=b: bad+=1; det.append({"old_invoice_id":r["oid"],"sls_invoice_id":r["sid"],"col":k,"old":a,"sls":b,"old_status":r["ost"]})
    print(f"  {k:26s} mismatch={bad:5d} both_null={bn}")
csvout("sales_hdr_col_mismatches.csv", det)
print("status crosstab:", Counter((str(r["ost"]),str(r["oact"]),str(r["sst"]),str(r["sact"])) for r in H))
print("date-null pairs:", [(r["oid"],r["sid"],r["od"],r["sd"]) for r in H if r["od"] is None or r["sd"] is None])
print("old invoice_no_string vs sls invoice_no sample:", [(r["oins"],r["sino"]) for r in H[:5]])
print("old mr_id nonnull:", sum(1 for r in H if r["mr_id"]))

L = q(c, f"""
SELECT li.invoice_line_item_id oid, d.invoice_line_item_id sid, li.invoice_id oinv, d.invoice_id sinv,
       li.mr_line_id, d.jute_mr_li_id, li.sales_weight osw, d.sales_weight ssw, li.quantity oq, d.quantity sq,
       li.rate orate, d.rate srate, li.total_amount ota, d.total_amount sta, li.amount_without_tax oawt, d.amount_without_tax sawt,
       li.hsn_code ohsn, d.hsn_code shsn, li.is_active lact, li.item_id oitem, d.item_id sitem, li.quality_id,
       li.sales_bale, li.sales_drum, li.uom, d.uom_id, x.actual_item_id li_item, h.status hstat
FROM vowsls.invoice_line_items li JOIN vowsls.invoice_hdr h ON h.invoice_id=li.invoice_id
JOIN vowsls_to_sls_transfer._map_invoice_lines ml ON ml.old_invoice_line_item_id=li.invoice_line_item_id
JOIN sls.sales_invoice_dtl d ON d.invoice_line_item_id=ml.new_invoice_line_item_id
LEFT JOIN sls.jute_mr_li x ON x.jute_mr_li_id=d.jute_mr_li_id
WHERE h.company_id=106""")
print("\ninvoice line pairs:", len(L))
lc = {
 "mr_line_id vs jute_mr_li_id": lambda r:(n(r["mr_line_id"]), n(r["jute_mr_li_id"])),
 "sales_weight (qtl)": lambda r:(n(r["osw"]), n(r["ssw"])),
 "quantity":  lambda r:(n(r["oq"]), n(r["sq"])),
 "rate":      lambda r:(n(r["orate"]), n(r["srate"])),
 "total_amount": lambda r:(n(r["ota"]), n(r["sta"])),
 "amount_without_tax": lambda r:(n(r["oawt"]), n(r["sawt"])),
 "hsn_code":  lambda r:(st(r["ohsn"]), st(r["shsn"])),
 "item: sls line item vs MR-line item": lambda r:(n(r["li_item"]), n(r["sitem"])),
}
det2=[]
for k,f in lc.items():
    bad=0;bn=0
    for r in L:
        a,b=f(r)
        if a is None and b is None: bn+=1; continue
        if a!=b: bad+=1; det2.append({"old_line_id":r["oid"],"sls_line_id":r["sid"],"col":k,"old":a,"sls":b,"hdr_status":r["hstat"],"old_line_active":r["lact"]})
    print(f"  {k:38s} mismatch={bad:5d} both_null={bn}")
csvout("sales_line_col_mismatches.csv", det2)
print("dropped in sls: sales_bale nonzero:", sum(1 for r in L if r["sales_bale"]), "sales_drum:", sum(1 for r in L if r["sales_drum"]),
      "quality_id:", sum(1 for r in L if r["quality_id"]), "is_active=0:", sum(1 for r in L if r["lact"]==0))
print("jute_mr_li_id NULL in sls:", sum(1 for r in L if r["jute_mr_li_id"] is None), "old mr_line_id NULL:", sum(1 for r in L if not r["mr_line_id"]))
