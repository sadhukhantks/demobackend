from lib import *
from collections import defaultdict, Counter
c = conn(); R = lambda x: round(x,2)

# every old sales line for co106 with its map + sls counterpart state
osl = q(c, f"""
SELECT il.invoice_line_item_id oid, il.invoice_id oinv, il.mr_line_id, il.sales_weight ow, il.is_active lact,
       il.quality_id, h.status hst, h.is_active hact, h.invoice_date oidt,
       ml.new_invoice_line_item_id nid, d.invoice_id sinv, d.sales_weight sw, d.jute_mr_li_id sline,
       s.invoice_date sidt, s.status_id sst, s.active sact, s.branch_id sbr, s.invoice_type styp
FROM vowsls.invoice_line_items il
JOIN vowsls.invoice_hdr h ON h.invoice_id=il.invoice_id
LEFT JOIN vowsls_to_sls_transfer._map_invoice_lines ml ON ml.old_invoice_line_item_id=il.invoice_line_item_id
LEFT JOIN sls.sales_invoice_dtl d ON d.invoice_line_item_id=ml.new_invoice_line_item_id
LEFT JOIN sls.sales_invoice s ON s.invoice_id=d.invoice_id
WHERE h.company_id=106""")
def old_counted(r): return r["lact"]==1 and (r["quality_id"] or 0)>0 and r["oidt"] is not None and str(r["oidt"])<=CUT
def sls_counted(r): return r["sinv"] is not None and r["sst"]==3 and r["sact"]==1 and r["sidt"] is not None and str(r["sidt"])<=CUT and r["sline"] is not None
buck=defaultdict(lambda:[0,0.0]); det=[]
for r in osl:
    o = (r["ow"] or 0) if old_counted(r) else 0.0
    s = (r["sw"] or 0) if sls_counted(r) else 0.0
    d = o - s
    if abs(d) < 0.005: continue
    if r["nid"] is None:
        cz = "sales line SKIPPED by migration (no map row)" if r["hst"]==3 else "sales line on rejected/void invoice, dropped (old view still counted it)"
    elif not sls_counted(r) and old_counted(r):
        if r["sidt"] is None: cz="sls invoice_date NULL -> excluded by <=cut"
        elif r["sline"] is None: cz="sls jute_mr_li_id NULL -> unlinked"
        elif r["sst"]!=3: cz="sls invoice status != 3"
        else: cz="other sls exclusion"
    elif sls_counted(r) and not old_counted(r):
        cz = "counted only in sls (old view excluded: quality_id=0 or inactive line or NULL date)"
    else:
        cz = "weight value differs"
    buck[cz][0]+=1; buck[cz][1]+=d
    det.append({"old_line_id":r["oid"],"old_inv":r["oinv"],"sls_inv":r["sinv"],"mr_line_id":r["mr_line_id"],
                "old_qtl":o,"sls_qtl":s,"delta_qtl":R(d),"cause":cz,"old_hdr_status":r["hst"],
                "old_line_active":r["lact"],"quality_id":r["quality_id"],"old_inv_date":r["oidt"],"sls_inv_date":r["sidt"],"sls_status":r["sst"]})
print("=== SALES gap attribution (old_counted - sls_counted), qtl ===")
tot=0
for k,(n,v) in sorted(buck.items(), key=lambda x:-abs(x[1][1])):
    print(f"  {k:70s} n={n:4d} {R(v):>10,.2f}"); tot+=v
print("  TOTAL sales delta:", R(tot))
csvout("stock_gap_sales_attribution.csv", det)

# sls sales rows with no old counterpart at all
extra = q(c, f"""SELECT d.invoice_line_item_id, d.invoice_id, d.jute_mr_li_id, d.sales_weight, s.invoice_date, s.status_id
FROM sls.sales_invoice_dtl d JOIN sls.sales_invoice s ON s.invoice_id=d.invoice_id
WHERE s.branch_id=87 AND s.invoice_type=5 AND s.status_id=3 AND s.active=1 AND s.invoice_date<='{CUT}'
AND NOT EXISTS(SELECT 1 FROM vowsls_to_sls_transfer._map_invoice_lines ml WHERE ml.new_invoice_line_item_id=d.invoice_line_item_id)""")
print("  sls counted sales with NO old map row:", len(extra), R(sum(r["sales_weight"] or 0 for r in extra)))

# ---- ISSUES
oi = q(c, f"""SELECT i.issue_no oid, i.stock_id, i.total_weight ow, i.issue_status ost, i.is_active oact, i.issue_date odt,
       s.jute_issue_id sid, s.weight sw, s.status_id sst, s.issue_date sdt, s.branch_id sbr, s.jute_mr_li_id sline
FROM vowsls.jute_issue i LEFT JOIN sls.jute_issue s ON s.jute_issue_id=i.issue_no AND s.branch_id=87
WHERE i.company_id=106 AND i.issue_date<='{CUT}'""")
b2=defaultdict(lambda:[0,0.0]); det2=[]
for r in oi:
    o = (r["ow"] or 0) if r["oact"]==1 else 0.0
    s = (r["sw"] or 0) if (r["sid"] is not None and r["sst"]==3 and r["sdt"] is not None and str(r["sdt"])<=CUT) else 0.0
    d=o-s
    if abs(d)<0.005: continue
    if r["sid"] is None:
        cz = f"issue row MISSING in sls (old status {r['ost']}, active {r['oact']})"
    elif r["sst"]!=3: cz="sls issue status != 3"
    else: cz="weight value differs"
    b2[cz][0]+=1; b2[cz][1]+=d
    det2.append({"old_issue_no":r["oid"],"stock_id":r["stock_id"],"old_qtl":o,"sls_qtl":s,"delta_qtl":R(d),
                 "cause":cz,"old_status":r["ost"],"old_active":r["oact"],"issue_date":r["odt"]})
print("\n=== ISSUE gap attribution (old_counted - sls_counted), qtl ===")
tot2=0
for k,(n,v) in sorted(b2.items(), key=lambda x:-abs(x[1][1])):
    print(f"  {k:70s} n={n:4d} {R(v):>10,.2f}"); tot2+=v
print("  TOTAL issue delta:", R(tot2))
csvout("stock_gap_issue_attribution.csv", det2)

# ---- RECEIPT
rc = q(c, f"""SELECT l.jute_line_item_no id, l.is_active oact, h.mr_good_recept_status hstat,
       l.actual_weight mill, l.accepted_weight/100 acc, l.shortage_kgs/100 short,
       x.jute_mr_li_id sid, x.actual_weight sw, x.active sact, m.jute_mr_date sdt, m.branch_id sbr
FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
LEFT JOIN sls.jute_mr_li x ON x.jute_mr_li_id=l.jute_line_item_no
LEFT JOIN sls.jute_mr m ON m.jute_mr_id=x.jute_mr_id AND m.branch_id=87
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'""")
b3=defaultdict(lambda:[0,0.0,0.0]); det3=[]
for r in rc:
    oel = r["oact"]==1 and str(r["hstat"]) not in ("4","6")
    omill = (r["mill"] or 0) if oel else 0.0
    oacc  = (r["acc"] or 0) if oel else 0.0
    s = (r["sw"] or 0) if (r["sid"] is not None and r["sact"]==1 and r["sbr"]==87 and r["sdt"] is not None and str(r["sdt"])<=CUT) else 0.0
    dm, da = s-omill, s-oacc
    if abs(dm)<0.005 and abs(da)<0.005: continue
    if r["sid"] is None and oel: cz="MR line MISSING in sls (old active, hdr not rejected)"
    elif r["sid"] is None: cz="MR line missing in sls but old-ineligible (inactive/rejected) - no effect"
    elif abs(dm)>=0.005: cz="actual_weight value differs"
    else: cz="mill vs accepted basis (sls has no accepted_weight; shortage not deducted)"
    b3[cz][0]+=1; b3[cz][1]+=dm; b3[cz][2]+=da
    det3.append({"line_id":r["id"],"old_mill_qtl":R(omill),"old_acc_qtl":R(oacc),"sls_qtl":R(s),
                 "delta_vs_mill":R(dm),"delta_vs_acc":R(da),"cause":cz,"old_active":r["oact"],"hdr_status":r["hstat"]})
print("\n=== RECEIPT gap attribution (sls - old), qtl ===")
t3m=t3a=0
for k,(n,vm,va) in sorted(b3.items(), key=lambda x:-abs(x[1][2])):
    print(f"  {k:70s} n={n:4d} vs_mill {R(vm):>10,.2f}  vs_acc {R(va):>10,.2f}"); t3m+=vm; t3a+=va
print("  TOTAL receipt delta: vs_mill", R(t3m), " vs_acc", R(t3a))
csvout("stock_gap_receipt_attribution.csv", det3)
