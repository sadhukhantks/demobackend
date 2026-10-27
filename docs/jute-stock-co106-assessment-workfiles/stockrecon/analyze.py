import csv, os, collections
from decimal import Decimal
D = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT = "2026-03-31"
def load(n):
    with open(os.path.join(D,n+".csv"),encoding="utf-8") as f: return list(csv.DictReader(f))
def F(v):
    if v is None or v=="" or v=="None": return 0.0
    try: return float(v)
    except: return 0.0
def R(x,n=2):
    # MySQL ROUND: half away from zero
    import math
    m = 10**n
    return math.floor(abs(x)*m+0.5)/m * (1 if x>=0 else -1)

view = load("view_all")
line = load("line106"); hdr = load("hdr106"); iss = load("issue106"); inv = load("inv106")
qual = {r["id"]:r["jute_quality"] for r in load("qual")}
wh   = {r["id"]:(r["name"],r["address"]) for r in load("wh")}

hdrby = {h["jute_receive_no"]:h for h in hdr}
lineby = {l["jute_line_item_no"]:l for l in line}
L106 = set(lineby)
print("co106 MR headers:",len(hdr)," lines:",len(line)," view rows total:",len(view))

def dated(r, upto=CUT):
    d = r["tran_date"]
    return bool(d) and d != "None" and d <= upto

# ---------- b subquery: group by mr_line_id over view rows with tran_date <= CUT (NO company/status filter) ----------
def build_b(rows, wtcol="accepted_weight", incl_null_sales=False, status_filter=None, upto=CUT):
    b = collections.defaultdict(lambda: dict(br=0.0,bi=0.0,bs=0.0,dr=0.0,di=0.0,ds=0.0,wr=0.0,wi=0.0,ws=0.0,n=0))
    for r in rows:
        ok = dated(r, upto)
        if not ok and incl_null_sales and r["transaction_type"]=="S" and (not r["tran_date"] or r["tran_date"]=="None"):
            ok = True
        if not ok: continue
        if status_filter is not None and str(r["tran_status"]) not in status_filter: continue
        k = r["mr_line_id"]; e = b[k]; e["n"] += 1
        e["br"] += F(r["bales_receipt"]); e["bi"] += F(r["bales_issued"]); e["bs"] += F(r["bales_sold"])
        e["dr"] += F(r["drums_receipt"]); e["di"] += F(r["drums_issued"]); e["ds"] += F(r["drums_sold"])
        e["wr"] += F(r[wtcol]) if r["transaction_type"]=="R" else 0.0
        e["wi"] += F(r["weight_issued"]); e["ws"] += F(r["weight_sold"])
    return b

# a rows = R rows, company 106, tran_status not in (4,6)
def a_rows(rows, statuses_excl={"4","6"}, branch=None, upto=None):
    out=[]
    for r in rows:
        if r["transaction_type"]!="R": continue
        if str(r["company_id"])!="106": continue
        if str(r["tran_status"]) in statuses_excl: continue
        if branch is not None and str(r["branch_id"])!=str(branch): continue
        if upto is not None and not dated(r, upto): continue
        out.append(r)
    return out

def report(rows, wtcol="accepted_weight", incl_null_sales=False, statuses_excl={"4","6"},
           branch=None, a_upto=None, b_status=None, rowfilter="baledrum", upto=CUT):
    b = build_b(rows, wtcol, incl_null_sales, b_status, upto)
    out=[]
    for a in a_rows(rows, statuses_excl, branch, a_upto):
        e = b.get(a["mr_line_id"])
        if e is None: continue
        bs = R(e["br"]-e["bi"]-e["bs"]); ds = R(e["dr"]-e["di"]-e["ds"])
        ws = R(e["wr"]-e["wi"]-e["ws"])
        if rowfilter=="baledrum":
            if R(bs,0)==0 and R(ds,0)==0: continue
        elif rowfilter=="weight":
            if ws==0: continue
        elif rowfilter=="any":
            if R(bs,0)==0 and R(ds,0)==0 and ws==0: continue
        out.append((a,e,bs,ds,ws))
    return out

def tot(res):
    return (len(res), round(sum(x[4] for x in res),2), round(sum(x[2] for x in res),2), round(sum(x[3] for x in res),2),
            round(sum(x[1]["wr"] for x in res),2), round(sum(x[1]["wi"] for x in res),2), round(sum(x[1]["ws"] for x in res),2))

print("\n=== BASELINE: old 'Jute MR in Stock' report, as on", CUT, "(company 106) ===")
base = report(view)
n,ws,bs,ds,wr,wi,wsold = tot(base)
print(f"rows={n}  receipt_wt={wr}  issued_wt={wi}  sold_wt={wsold}  closing_wt={ws} qtl  bales={bs} drums={ds}")

print("\n--- ALT definitions ---")
alts = {}
def show(name, res):
    n,ws,bs,ds,wr,wi,wsold = tot(res)
    alts[name]=(n,ws,bs,ds,wr,wi,wsold)
    print(f"{name:62s} lines={n:4d} closing={ws:12.2f} qtl (recv {wr:.2f} / iss {wi:.2f} / sold {wsold:.2f}) bales={bs} drums={ds}")

show("A. OLD REPORT AS-IS (accepted wt, bale/drum!=0 filter)", base)
show("B. row filter on WEIGHT!=0 instead of bale/drum!=0", report(view, rowfilter="weight"))
show("C. row filter bale/drum OR weight !=0", report(view, rowfilter="any"))
show("D. MILL weight instead of accepted weight", report(view, wtcol="mill_weight"))
show("E. MILL wt + weight!=0 filter", report(view, wtcol="mill_weight", rowfilter="weight"))
show("F. include NULL-dated sales", report(view, incl_null_sales=True))
show("G. include NULL-dated sales + weight!=0", report(view, incl_null_sales=True, rowfilter="weight"))
show("H. receipts APPROVED(3) only", report(view, statuses_excl=set(str(s) for s in range(0,60))-{"3"}))
show("I. exclude status 1/21/24 receipts too", report(view, statuses_excl={"4","6","1","21","24"}))
show("J. b-subquery also status-filtered (excl 4,6)", report(view, b_status=set(str(s) for s in range(0,60))-{"4","6"}))
show("K. branch 87 only", report(view, branch=87))
show("L. branch 87 only + weight!=0", report(view, branch=87, rowfilter="weight"))
show("M. no row filter at all (all R lines co106)", report(view, rowfilter="none"))

import json
json.dump({k:list(v) for k,v in alts.items()}, open(os.path.join(D,"alts.json"),"w"), indent=1)
