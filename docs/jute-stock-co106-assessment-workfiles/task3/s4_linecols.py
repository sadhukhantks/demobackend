from lib import *
from collections import Counter, defaultdict
c = conn()
rows = q(c, f"""
SELECT l.jute_line_item_no id, l.jute_receive_no ohdr, x.jute_mr_id shdr,
       l.actual_bale, l.actual_loose, l.quantity oqty, l.bale, l.loose,
       l.actual_weight omill, l.accepted_weight oacc_kg, l.shortage_kgs oshort,
       l.rate orate, l.jute_issue_rate, l.actual_quality oq, l.advised_quality oaq,
       l.item_code, l.warehouse_no owh, l.is_active oact, l.status ostat,
       l.unit_conversion ouc, l.marka omarka, l.crop_year ocrop,
       l.advised_quantity, l.advised_weight, l.allowable_moisture oam, l.claim_dust ocd,
       l.total_price otp, l.claim_rate ocr, l.premium_amount opa, l.water_damage_amount owda,
       l.quality_desc, l.sales_bales, l.sales_drums, l.sales_weight, l.bales_consumed, l.drums_consumed, l.weight_consumed,
       x.actual_qty sqty, x.actual_weight smill, x.accepted_weight sacc, x.shortage_kgs sshort,
       x.rate srate, x.actual_rate, x.actual_quality sq, x.challan_quality_id saq, x.actual_item_id,
       x.challan_item_id, x.challan_quantity, x.challan_weight, x.warehouse_id swh, x.active sact,
       x.status sstat, x.unit_conversion suc, x.marka smarka, x.crop_year scrop,
       x.allowable_moisture sam, x.claim_dust scd, x.total_price stp, x.claim_rate scr,
       x.premium_amount spa, x.water_damage_amount swda, x.jute_po_li_id,
       h.unit_conversion huc
FROM vowsls.scm_mr_line_item l
JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
JOIN sls.jute_mr_li x ON x.jute_mr_li_id=l.jute_line_item_no
JOIN sls.jute_mr m ON m.jute_mr_id=x.jute_mr_id AND m.branch_id=87
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'""")
print("line pairs:", len(rows))

def n(v):
    try: return None if v is None else round(float(v),3)
    except: return None
def s(v):
    if v is None: return None
    v=str(v).strip(); return None if v=="" else v

checks = {
 "hdr link (jute_receive_no vs jute_mr_id)": lambda r:(r["ohdr"], r["shdr"]),
 "actual_weight (mill qtl)":  lambda r:(n(r["omill"]), n(r["smill"])),
 "accepted_weight":           lambda r:(n(r["oacc_kg"]), n(r["sacc"])),
 "shortage_kgs":              lambda r:(n(r["oshort"]), n(r["sshort"])),
 "qty: actual_bale+loose vs actual_qty": lambda r:(n((r["actual_bale"] or 0)+(r["actual_loose"] or 0)), n(r["sqty"])),
 "qty: old quantity vs actual_qty": lambda r:(n(r["oqty"]), n(r["sqty"])),
 "rate":                      lambda r:(n(r["orate"]), n(r["srate"])),
 "actual_quality":            lambda r:(n(r["oq"]), n(r["sq"])),
 "advised_quality vs challan_quality_id": lambda r:(n(r["oaq"]), n(r["saq"])),
 "warehouse":                 lambda r:(n(r["owh"]), n(r["swh"])),
 "is_active vs active":       lambda r:(n(r["oact"]), n(r["sact"])),
 "status":                    lambda r:(s(r["ostat"]), s(r["sstat"])),
 "unit_conversion (line)":    lambda r:(s(r["ouc"]), s(r["suc"])),
 "unit_conversion sls-line vs old-HDR": lambda r:(s(r["huc"]), s(r["suc"])),
 "marka":                     lambda r:(s(r["omarka"]), s(r["smarka"])),
 "crop_year":                 lambda r:(n(r["ocrop"]), n(r["scrop"])),
 "allowable_moisture":        lambda r:(n(r["oam"]), n(r["sam"])),
 "claim_dust":                lambda r:(n(r["ocd"]), n(r["scd"])),
 "total_price":               lambda r:(n(r["otp"]), n(r["stp"])),
 "claim_rate":                lambda r:(n(r["ocr"]), n(r["scr"])),
 "premium_amount":            lambda r:(n(r["opa"]), n(r["spa"])),
 "water_damage_amount":       lambda r:(n(r["owda"]), n(r["swda"])),
 "old quantity vs challan_quantity": lambda r:(n(r["oqty"]), n(r["challan_quantity"])),
 "old advised_weight vs challan_weight": lambda r:(n(r["advised_weight"]), n(r["challan_weight"])),
}
detail=[]
print(f"{'column':46s} {'mismatch':>8s} {'both_null':>9s}")
for name,f in checks.items():
    bad=0; bn=0
    for r in rows:
        a,b=f(r)
        if a is None and b is None: bn+=1; continue
        if a!=b:
            bad+=1; detail.append({"line_id":r["id"],"mr_id":r["ohdr"],"col":name,"old":a,"sls":b,"old_active":r["oact"],"sls_active":r["sact"]})
    print(f"{name:46s} {bad:8d} {bn:9d}")
csvout("mr_line_col_mismatches.csv", detail)
print("wrote", len(detail))

# unit sanity: accepted_weight vs mill*100
print("\naccepted_weight(old) / actual_weight(old) ratio distribution:")
rr=Counter()
for r in rows:
    if r["omill"] and r["oacc_kg"]:
        rr[round(r["oacc_kg"]/r["omill"],0)]+=1
print(rr.most_common(8))
print("sls accepted_weight null:", sum(1 for r in rows if r["sacc"] is None), "zero:", sum(1 for r in rows if r["sacc"]==0))
print("sls shortage_kgs !=0:", sum(1 for r in rows if r["sshort"]), " old shortage !=0:", sum(1 for r in rows if r["oshort"]))
print("old shortage sum kg:", sum(r["oshort"] or 0 for r in rows), "=> qtl", round(sum(r["oshort"] or 0 for r in rows)/100,2))
# item mapping consistency
qi=defaultdict(set)
for r in rows: qi[r["oq"]].add(r["actual_item_id"])
print("\nactual_quality -> actual_item_id 1:many count:", sum(1 for k,v in qi.items() if len(v)>1))
print("distinct qualities:", len(qi))
iq=defaultdict(set)
for r in rows: iq[r["actual_item_id"]].add(r["oq"])
print("actual_item_id shared by >1 quality:", sum(1 for k,v in iq.items() if len(v)>1))
print("null actual_item_id:", sum(1 for r in rows if r["actual_item_id"] is None))
ic=defaultdict(set)
for r in rows: ic[s(r["item_code"])].add(r["actual_item_id"])
print("old item_code -> sls item 1:many:", {k:len(v) for k,v in ic.items()})
