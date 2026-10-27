from lib import *
from collections import Counter, defaultdict
c = conn()
rows = q(c, f"""
SELECT h.jute_receive_no id, h.branch_id ob, DATE(h.jute_receive_dt) odt, h.mr_good_recept_status ostat,
       h.gate_entry_no oge, h.unit_conversion ouc, h.mukam_id omuk, h.vehicle_no oveh, h.weight owt,
       h.frieght_paid ofp, h.challan_no ocn, h.challan_date ocd, h.po_num, h.supp_code, h.mr_print_no,
       h.src_hdr_id, h.src_com_id oscid, h.mr_type,
       s.branch_id sb, s.jute_mr_date sdt, s.out_date, s.jute_gate_entry_date sged, s.status_id,
       s.jute_gate_entry_no sge, s.unit_conversion suc, s.mukam_id smuk, s.vehicle_no sveh,
       s.actual_weight sawt, s.mr_weight smwt, s.challan_weight scw, s.frieght_paid sfp,
       s.challan_no scn, s.challan_date scd, s.po_id, s.party_id, s.jute_supplier_id, s.branch_mr_no,
       s.src_jute_mr_id, s.src_com_id sscid, s.transfer_mode
FROM vowsls.scm_mr_hdr h JOIN sls.jute_mr s ON s.jute_mr_id=h.jute_receive_no AND s.branch_id=87
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'""")
print("pairs:", len(rows))

def norm(v):
    if v is None: return None
    if isinstance(v,str):
        v=v.strip()
        return None if v in ("","null","NULL","0") else v
    return v
def num(v):
    try: return None if v is None else round(float(v),3)
    except: return None

checks = {
 "jute_receive_dt vs jute_mr_date": lambda r: (r["odt"], r["sdt"]),
 "jute_receive_dt vs out_date":     lambda r: (r["odt"], r["out_date"]),
 "gate_entry_no":                   lambda r: (norm(r["oge"]), norm(r["sge"])),
 "unit_conversion":                 lambda r: (norm(r["ouc"]), norm(r["suc"])),
 "mukam_id":                        lambda r: (norm(r["omuk"]), norm(r["smuk"])),
 "vehicle_no":                      lambda r: (norm(r["oveh"]), norm(r["sveh"])),
 "hdr weight vs actual_weight":     lambda r: (num(r["owt"]), num(r["sawt"])),
 "hdr weight vs mr_weight":         lambda r: (num(r["owt"]), num(r["smwt"])),
 "frieght_paid":                    lambda r: (num(r["ofp"]), num(r["sfp"])),
 "challan_no":                      lambda r: (norm(r["ocn"]), norm(r["scn"])),
 "challan_date":                    lambda r: (r["ocd"].date() if r["ocd"] else None, r["scd"]),
 "src_com_id":                      lambda r: (norm(r["oscid"]), norm(r["sscid"])),
 "src_hdr_id vs src_jute_mr_id":    lambda r: (norm(r["src_hdr_id"]), norm(r["src_jute_mr_id"])),
}
res={}
detail=defaultdict(list)
for name,f in checks.items():
    bad=0; both_null=0
    for r in rows:
        a,b=f(r)
        if a is None and b is None: both_null+=1; continue
        if a!=b:
            bad+=1
            if len(detail[name])<50000: detail[name].append({"id":r["id"],"col":name,"old":a,"sls":b})
    res[name]=(bad,both_null)
for k,(bad,bn) in res.items():
    print(f"  {k:38s} mismatch={bad:5d}  both_null={bn}")

# status crosstab
print("\nstatus crosstab old->sls:", Counter((str(r["ostat"]),str(r["status_id"])) for r in rows))
print("branch old->sls:", Counter((str(r["ob"]),str(r["sb"])) for r in rows))
print("unit_conversion old->sls:", Counter((str(r["ouc"]),str(r["suc"])) for r in rows))
print("out_date null count:", sum(1 for r in rows if r["out_date"] is None))
print("jute_gate_entry_date vs old dt mismatch:", sum(1 for r in rows if r["sged"]!=r["odt"]))

allrows=[]
for k,v in detail.items(): allrows.extend(v)
csvout("mr_hdr_col_mismatches.csv", allrows)
print("\nwrote", len(allrows), "hdr col mismatch rows")
