import csv, os, collections
D = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT="2026-03-31"
def load(n):
    with open(os.path.join(D,n+".csv"),encoding="utf-8") as f: return list(csv.DictReader(f))
def F(v):
    if v in (None,"","None"): return 0.0
    try: return float(v)
    except: return 0.0
view=load("view_all"); line=load("line106"); hdr=load("hdr106"); iss=load("issue106")
hdrby={h["jute_receive_no"]:h for h in hdr}; lineby={l["jute_line_item_no"]:l for l in line}
L106=set(lineby)
def dated(d): return bool(d) and d!="None" and d<=CUT
vI=collections.defaultdict(float); vIn=collections.Counter()
for r in view:
    if r["transaction_type"]=="I" and r["mr_line_id"] in L106 and dated(r["tran_date"]):
        vI[r["mr_line_id"]]+=F(r["weight_issued"]); vIn[r["mr_line_id"]]+=1
rI=collections.defaultdict(float); rIn=collections.Counter(); rows=collections.defaultdict(list)
for i in iss:
    if i["is_active"]=="1" and dated(i["issue_date"]):
        rI[i["stock_id"]]+=round(F(i["total_weight"]),2); rIn[i["stock_id"]]+=1; rows[i["stock_id"]].append(i)
diff=[]
for k in set(list(vI)+list(rI)):
    if abs(vI.get(k,0)-rI.get(k,0))>0.005 or vIn.get(k,0)!=rIn.get(k,0):
        diff.append((k, rIn.get(k,0), round(rI.get(k,0),2), vIn.get(k,0), round(vI.get(k,0),2)))
diff.sort(key=lambda x:-(x[2]-x[4]))
print("lines where view I-sum != raw issue sum:",len(diff),"total weight gap:",round(sum(x[2]-x[4] for x in diff),2))
print("line | raw_n | raw_wt | view_n | view_wt | gap")
for d in diff[:40]:
    l=lineby.get(d[0]); h=hdrby.get(l["jute_receive_no"]) if l else None
    print(d, "| MR",l["jute_receive_no"] if l else "?", "hdrstatus",h["mr_good_recept_status"] if h else "?", "lineactive",l["is_active"] if l else "?")
    for r in rows[d[0]][:8]:
        print("      issue",r["issue_no"],r["issue_date"],"wt",r["total_weight"],"qty",r["quantity"],"st",r["issue_status"],"br",r["branch_id"])
