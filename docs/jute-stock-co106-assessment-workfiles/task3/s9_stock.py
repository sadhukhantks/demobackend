from lib import *
from collections import defaultdict, Counter
c = conn()
R = lambda x: round(x, 2)

# --- old MR lines in scope (co106, hdr<=cut). keep flags for is_active / hdr status
lines = q(c, f"""
SELECT l.jute_line_item_no id, l.jute_receive_no mr, h.mr_good_recept_status hstat, l.is_active oact,
       l.actual_weight mill, l.accepted_weight/100 acc, l.actual_bale, l.actual_loose, l.actual_quality
FROM vowsls.scm_mr_line_item l JOIN vowsls.scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
WHERE h.company_id=106 AND h.jute_receive_dt<='{CUT}'""")
L = {r["id"]: r for r in lines}
print("old lines in scope:", len(L))

# old-system stock eligibility: line active, header status not rejected(4)/cancelled(6)
elig = {k for k,v in L.items() if v["oact"]==1 and str(v["hstat"]) not in ("4","6")}
print("old stock-eligible lines:", len(elig))

# --- old issues <= cut, is_active=1 (old view rule)
oi = q(c, f"""SELECT i.stock_id id, i.issue_no, i.total_weight w, i.issue_status st, i.is_active act
FROM vowsls.jute_issue i WHERE i.company_id=106 AND i.issue_date<='{CUT}'""")
I_old = defaultdict(float); I_old_rows = defaultdict(list)
for r in oi:
    if r["act"]==1:
        I_old[r["id"]] += r["w"] or 0; I_old_rows[r["id"]].append(r)
print("old issue rows <=cut:", len(oi), " counted(active):", sum(1 for r in oi if r["act"]==1),
      " qtl:", R(sum(r["w"] or 0 for r in oi if r["act"]==1)))

# --- old sales <= cut, line active & quality_id>0 (old view rule)
osl = q(c, f"""SELECT il.mr_line_id id, il.invoice_line_item_id lid, il.sales_weight w, il.is_active lact,
       il.quality_id, h.status hst, h.is_active hact, h.invoice_date idt, h.invoice_id
FROM vowsls.invoice_line_items il JOIN vowsls.invoice_hdr h ON h.invoice_id=il.invoice_id
WHERE h.company_id=106""")
S_old = defaultdict(float); S_old_rows = defaultdict(list)
for r in osl:
    if r["lact"]==1 and (r["quality_id"] or 0) > 0 and r["idt"] is not None and str(r["idt"]) <= CUT:
        S_old[r["id"]] += r["w"] or 0; S_old_rows[r["id"]].append(r)
print("old sales lines total:", len(osl), " counted:", sum(len(v) for v in S_old_rows.values()),
      " qtl:", R(sum(S_old.values())))

# --- sls receipt
sr = q(c, f"""SELECT x.jute_mr_li_id id, x.actual_weight w, x.active act FROM sls.jute_mr_li x
JOIN sls.jute_mr m ON m.jute_mr_id=x.jute_mr_id AND m.branch_id=87 AND m.jute_mr_date<='{CUT}'""")
R_sls = {r["id"]: (r["w"] or 0) for r in sr if r["act"]==1}
print("sls lines (active, <=cut):", len(R_sls), " qtl:", R(sum(R_sls.values())))

si = q(c, f"""SELECT s.jute_mr_li_id id, s.jute_issue_id, s.weight w, s.status_id st
FROM sls.jute_issue s WHERE s.branch_id=87 AND s.issue_date<='{CUT}'""")
I_sls = defaultdict(float); I_sls_rows=defaultdict(list)
for r in si:
    if r["st"]==3: I_sls[r["id"]] += r["w"] or 0; I_sls_rows[r["id"]].append(r)
print("sls issue rows <=cut approved:", sum(1 for r in si if r["st"]==3), " qtl:", R(sum(I_sls.values())))

ss = q(c, f"""SELECT d.jute_mr_li_id id, d.invoice_line_item_id lid, d.sales_weight w, s.invoice_date idt, s.status_id st, s.active act, s.invoice_id
FROM sls.sales_invoice_dtl d JOIN sls.sales_invoice s ON s.invoice_id=d.invoice_id
WHERE s.branch_id=87 AND s.invoice_type=5""")
S_sls = defaultdict(float); S_sls_rows=defaultdict(list)
for r in ss:
    if r["st"]==3 and r["act"]==1 and r["idt"] is not None and str(r["idt"])<=CUT and r["id"]:
        S_sls[r["id"]] += r["w"] or 0; S_sls_rows[r["id"]].append(r)
print("sls sales lines counted:", sum(len(v) for v in S_sls_rows.values()), " qtl:", R(sum(S_sls.values())))

# ---- orphans
orph_i = [r for r in oi if r["act"]==1 and r["id"] not in elig]
orph_s = [r for v in S_old_rows.values() for r in v if r["id"] not in elig]
print("\nold ACTIVE issues pointing outside eligible lines:", len(orph_i), R(sum(r["w"] or 0 for r in orph_i)))
print("old counted sales pointing outside eligible lines:", len(orph_s), R(sum(r["w"] or 0 for r in orph_s)))
sls_orph_i = [r for r in si if r["st"]==3 and r["id"] not in R_sls]
sls_orph_s = [r for v in S_sls_rows.values() for r in v if r["id"] not in R_sls]
print("sls approved issues pointing outside sls active lines:", len(sls_orph_i), R(sum(r["w"] or 0 for r in sls_orph_i)))
print("sls counted sales pointing outside sls active lines:", len(sls_orph_s), R(sum(r["w"] or 0 for r in sls_orph_s)))
print("sls sales lines with NULL jute_mr_li_id (type5 b87):", sum(1 for r in ss if not r["id"]), R(sum(r["w"] or 0 for r in ss if not r["id"])))

# ---- per line reconciliation over union of universes
univ = set(elig) | set(R_sls) | set(I_old) | set(S_old) | set(I_sls) | set(S_sls)
rows=[]
for k in sorted(univ):
    l = L.get(k)
    racc = (l["acc"] or 0) if (l and k in elig) else 0.0
    rmill = (l["mill"] or 0) if (l and k in elig) else 0.0
    io, so = I_old.get(k,0.0), S_old.get(k,0.0)
    rs = R_sls.get(k, 0.0); isl, ssl = I_sls.get(k,0.0), S_sls.get(k,0.0)
    rows.append(dict(line_id=k, mr=(l or {}).get("mr"), hstat=(l or {}).get("hstat"), oact=(l or {}).get("oact"),
        r_acc=racc, r_mill=rmill, i_old=io, s_old=so, bal_old_acc=racc-io-so, bal_old_mill=rmill-io-so,
        r_sls=rs, i_sls=isl, s_sls=ssl, bal_sls=rs-isl-ssl,
        d_recv_mill=rs-rmill, d_recv_acc=rs-racc, d_iss=io-isl, d_sale=so-ssl))
for r in rows:
    r["gap_vs_mill"]=r["bal_sls"]-r["bal_old_mill"]; r["gap_vs_acc"]=r["bal_sls"]-r["bal_old_acc"]
csvout("stock_recon_per_line.csv", [{k:(R(v) if isinstance(v,float) else v) for k,v in r.items()} for r in rows])

T = lambda f: R(sum(r[f] for r in rows))
print("\n===== TOTALS (QUINTAL) =====")
for f in ["r_acc","r_mill","i_old","s_old","bal_old_acc","bal_old_mill","r_sls","i_sls","s_sls","bal_sls","gap_vs_mill","gap_vs_acc"]:
    print(f"  {f:14s} {T(f):>14,.2f}")
print("\ndecomposition of gap vs MILL:  recv %.2f + issue %.2f + sale %.2f = %.2f" % (T("d_recv_mill"),T("d_iss"),T("d_sale"),T("d_recv_mill")+T("d_iss")+T("d_sale")))
print("decomposition of gap vs ACCEPTED: recv %.2f + issue %.2f + sale %.2f = %.2f" % (T("d_recv_acc"),T("d_iss"),T("d_sale"),T("d_recv_acc")+T("d_iss")+T("d_sale")))

eps=0.005
print("\nlines equal (vs mill):", sum(1 for r in rows if abs(r["gap_vs_mill"])<eps), "differ:", sum(1 for r in rows if abs(r["gap_vs_mill"])>=eps))
print("lines equal (vs accepted):", sum(1 for r in rows if abs(r["gap_vs_acc"])<eps), "differ:", sum(1 for r in rows if abs(r["gap_vs_acc"])>=eps))
print("total lines in universe:", len(rows))
# nonzero balances
print("\nold(acc) nonzero-balance lines:", sum(1 for r in rows if abs(r["bal_old_acc"])>=eps), "qtl", R(sum(r["bal_old_acc"] for r in rows if abs(r["bal_old_acc"])>=eps)))
print("old(mill) nonzero-balance lines:", sum(1 for r in rows if abs(r["bal_old_mill"])>=eps), "qtl", R(sum(r["bal_old_mill"] for r in rows if abs(r["bal_old_mill"])>=eps)))
print("sls nonzero-balance lines:", sum(1 for r in rows if abs(r["bal_sls"])>=eps), "qtl", R(sum(r["bal_sls"] for r in rows if abs(r["bal_sls"])>=eps)))
import pickle; pickle.dump(rows, open("rows.pkl","wb"))
