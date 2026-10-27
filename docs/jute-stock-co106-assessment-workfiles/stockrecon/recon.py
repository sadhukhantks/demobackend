import csv, os, collections, math
D = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT="2026-03-31"
def load(n):
    with open(os.path.join(D,n+".csv"),encoding="utf-8") as f: return list(csv.DictReader(f))
def F(v):
    if v in (None,"","None"): return 0.0
    try: return float(v)
    except: return 0.0
view=load("view_all"); line=load("line106"); hdr=load("hdr106"); iss=load("issue106"); inv=load("inv106")
hdrby={h["jute_receive_no"]:h for h in hdr}
lineby={l["jute_line_item_no"]:l for l in line}
L106=set(lineby)
def dated(d): return bool(d) and d!="None" and d<=CUT

# view rows restricted to co106 lines
v106=[r for r in view if r["mr_line_id"] in L106]
print("view rows for co106 lines:",len(v106))
byT=collections.Counter(r["transaction_type"] for r in v106)
print("by type:",byT)

# --- receipts
vR=[r for r in v106 if r["transaction_type"]=="R" and dated(r["tran_date"])]
print("R rows<=cut:",len(vR),"acc_wt:",round(sum(F(r['accepted_weight']) for r in vR),2),
      "mill_wt:",round(sum(F(r['mill_weight']) for r in vR),2))
rawR=[l for l in line if l["is_active"]=="1" and dated(hdrby[l["jute_receive_no"]]["jute_receive_dt"])]
print("raw active lines<=cut:",len(rawR),"acc/100:",round(sum(F(l['accepted_weight'])/100 for l in rawR),2),
      "actual_weight:",round(sum(F(l['actual_weight']) for l in rawR),2))
# receipts excluding status 4,6
rawR46=[l for l in rawR if hdrby[l["jute_receive_no"]]["mr_good_recept_status"] not in ("4","6")]
print("  excl status 4/6:",len(rawR46),"acc/100:",round(sum(F(l['accepted_weight'])/100 for l in rawR46),2),
      "mill:",round(sum(F(l['actual_weight']) for l in rawR46),2))
rawR3=[l for l in rawR if hdrby[l["jute_receive_no"]]["mr_good_recept_status"]=="3"]
print("  approved(3) only:",len(rawR3),"acc/100:",round(sum(F(l['accepted_weight'])/100 for l in rawR3),2))
print("  status mix of pre-cut active lines:",collections.Counter(hdrby[l["jute_receive_no"]]["mr_good_recept_status"] for l in rawR))

# --- issues
vI=[r for r in v106 if r["transaction_type"]=="I" and dated(r["tran_date"])]
print("I rows<=cut:",len(vI),"wt:",round(sum(F(r['weight_issued']) for r in vI),2))
rawI=[i for i in iss if i["is_active"]=="1" and dated(i["issue_date"])]
print("raw jute_issue active<=cut:",len(rawI),"tot_wt:",round(sum(F(i['total_weight']) for i in rawI),2))
print("  issue_status mix:",collections.Counter(i["issue_status"] for i in rawI))
# dedup loss check
seen=collections.Counter((i["stock_id"],i["issue_date"],round(F(i["total_weight"]),2),round(F(i["quantity"]),2)) for i in rawI)
dupes={k:c for k,c in seen.items() if c>1}
print("  DUPLICATE (line,date,wt,qty) issue groups:",len(dupes),"extra rows lost to UNION dedup:",sum(c-1 for c in dupes.values()),
      "weight lost:",round(sum(k[2]*(c-1) for k,c in dupes.items()),2))

# --- sales
vS=[r for r in v106 if r["transaction_type"]=="S" and dated(r["tran_date"])]
print("S rows<=cut:",len(vS),"wt:",round(sum(F(r['weight_sold']) for r in vS),2))
rawS=[x for x in inv if x["is_active"]=="1" and F(x["quality_id"])>0 and dated(x["invoice_date"])]
print("raw invoice lines active,q>0,<=cut:",len(rawS),"wt:",round(sum(F(x['sales_weight']) for x in rawS),2))
print("  invoice status mix:",collections.Counter(x["status"] for x in rawS))
nulld=[x for x in inv if x["is_active"]=="1" and F(x["quality_id"])>0 and not dated(x["invoice_date"])]
print("  NULL/after-cut dated sale lines:",len(nulld),
      " of which NULL date:",sum(1 for x in nulld if x["invoice_date"] in (None,"","None")),
      " wt(NULL):",round(sum(F(x['sales_weight']) for x in nulld if x["invoice_date"] in (None,"","None")),2))
seenS=collections.Counter((x["mr_line_id"],x["invoice_date"],round(F(x["sales_weight"]),2),round(F(x["sales_bale"]),2),round(F(x["sales_drum"]),2),x["quality_id"],x["item_id"]) for x in rawS)
dupS={k:c for k,c in seenS.items() if c>1}
print("  DUP sale groups:",len(dupS),"rows lost:",sum(c-1 for c in dupS.values()),"wt lost:",round(sum(k[2]*(c-1) for k,c in dupS.items()),2))

# --- company-wide net over ALL co106 lines (no R-row survival requirement)
def netall(wt="accepted_weight"):
    r=sum(F(x[wt]) for x in vR); i=sum(F(x["weight_issued"]) for x in vI); s=sum(F(x["weight_sold"]) for x in vS)
    return round(r,2),round(i,2),round(s,2),round(r-i-s,2)
print("\nCOMPANY-WIDE over all co106 lines (view, <=cut): recv/iss/sold/net accepted =",netall())
print("COMPANY-WIDE mill weight:",netall("mill_weight"))
