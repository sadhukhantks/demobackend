from lib import *
from collections import Counter
c = conn()
rows = q(c, f"""
SELECT i.issue_no id, i.issue_date od, i.branch_id ob, i.issue_status ost, i.is_active oact,
       i.mr_no, i.stock_id, i.quantity oqty, i.total_weight oqtl, i.issue_value oval,
       i.jute_quality oq, i.yarn_type_id oyt, i.uom_code, i.godown_no, i.dept_id, i.no_bales, i.bale_loose, i.fin_year, i.close_stock, i.open_stock,
       s.issue_date sd, s.branch_id sb, s.status_id sst, s.jute_mr_li_id, s.quantity sqty, s.weight sw,
       s.issue_value sval, s.jute_quality_id sq, s.yarn_type_id syt, s.item_id, s.unit_conversion, s.waste_entry_id,
       x.actual_item_id li_item, x.actual_quality li_q
FROM vowsls.jute_issue i JOIN sls.jute_issue s ON s.jute_issue_id=i.issue_no AND s.branch_id=87
LEFT JOIN sls.jute_mr_li x ON x.jute_mr_li_id=s.jute_mr_li_id
WHERE i.company_id=106 AND i.issue_date<='{CUT}'""")
print("issue pairs:", len(rows))
def n(v):
    try: return None if v is None else round(float(v),3)
    except: return None
checks = {
 "issue_date":                lambda r:(r["od"], r["sd"]),
 "branch_id":                 lambda r:(n(r["ob"]), n(r["sb"])),
 "stock_id vs jute_mr_li_id": lambda r:(n(r["stock_id"]), n(r["jute_mr_li_id"])),
 "quantity":                  lambda r:(n(r["oqty"]), n(r["sqty"])),
 "total_weight vs weight (qtl)": lambda r:(n(r["oqtl"]), n(r["sw"])),
 "issue_value":               lambda r:(n(r["oval"]), n(r["sval"])),
 "jute_quality":              lambda r:(n(r["oq"]), n(r["sq"])),
 "yarn_type_id":              lambda r:(n(r["oyt"]), n(r["syt"])),
 "item_id vs MR-line item":   lambda r:(n(r["li_item"]), n(r["item_id"])),
}
det=[]
for name,f in checks.items():
    bad=0;bn=0
    for r in rows:
        a,b=f(r)
        if a is None and b is None: bn+=1; continue
        if a!=b: bad+=1; det.append({"issue_no":r["id"],"col":name,"old":a,"sls":b,"old_status":r["ost"],"old_active":r["oact"],"sls_status":r["sst"],"qtl":r["oqtl"]})
    print(f"  {name:34s} mismatch={bad:5d} both_null={bn}")
csvout("issue_col_mismatches.csv", det)
print("status crosstab (old_status,old_active)->sls:", Counter((str(r["ost"]),str(r["oact"]),str(r["sst"])) for r in rows))
print("dropped-in-sls fields nonempty in old: godown", sum(1 for r in rows if r["godown_no"]), "dept", sum(1 for r in rows if r["dept_id"]),
      "no_bales", sum(1 for r in rows if r["no_bales"]), "bale_loose", sum(1 for r in rows if r["bale_loose"]),
      "uom_code", sum(1 for r in rows if r["uom_code"]), "fin_year", sum(1 for r in rows if r["fin_year"]))
print("sls unit_conversion nonnull:", sum(1 for r in rows if r["unit_conversion"]))
print("mr_no vs sls li's mr:", q(c, f"""SELECT COUNT(*) bad FROM vowsls.jute_issue i JOIN sls.jute_issue s ON s.jute_issue_id=i.issue_no AND s.branch_id=87
 JOIN sls.jute_mr_li x ON x.jute_mr_li_id=s.jute_mr_li_id WHERE i.company_id=106 AND i.issue_date<='{CUT}' AND x.jute_mr_id<>i.mr_no""")[0])
