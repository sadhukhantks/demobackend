import pymysql, csv, os, collections
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT = "2026-03-31"; CO = 106
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=2400)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")

def q(sql):
    cur.execute(sql); return cur.fetchall()

def wcsv(name, rows, fields=None):
    if not rows:
        print("  (no rows)", name); return
    with open(os.path.join(OUT, name + ".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0].keys())); w.writeheader()
        for r in rows: w.writerow(r)
    print("  wrote", name + ".csv", len(rows), "rows")

for t in ("vw_a", "vw_b"):
    cur.execute("CREATE TEMPORARY TABLE %s AS SELECT * FROM view_jute_receipt_issue_sale" % t)
    cur.execute("CREATE INDEX ix_%s ON %s (mr_line_id)" % (t, t))

BT = chr(96)  # backtick

def B(wt="accepted_weight", upto=CUT, incl_null_sales=False, extra=""):
    cond = "tran_date <= '%s'" % upto
    if incl_null_sales:
        cond = "(%s OR (tran_date IS NULL AND transaction_type='S'))" % cond
    if extra:
        cond += " AND (%s)" % extra
    sel = """(SELECT mr_line_id MRLineNo,
   round(sum(bales_receipt),2) Bales, round(sum(bales_issued),2) IssueBales, round(sum(bales_sold),2) SoldBales,
   round(sum(bales_receipt)-sum(bales_issued)-sum(bales_sold),2) BalesStock,
   round(sum(drums_receipt),2) Drums, round(sum(drums_issued),2) DrumsIssued, round(sum(drums_sold),2) DrumsSold,
   round(sum(drums_receipt)-sum(drums_issued)-sum(drums_sold),2) DrumsStock,
   round(sum({W}),2) ReceiptWt, round(sum(weight_issued),2) IssuedWt, round(sum(weight_sold),2) SoldWt,
   round(sum({W})-sum(weight_issued)-sum(weight_sold),2) StockQNT
 FROM vw_b WHERE {C} GROUP BY mr_line_id) b"""
    return sel.replace("{W}", wt).replace("{C}", cond)

def REP(bsql, awhere="a.tran_status NOT IN (4,6)", rowfilter="none"):
    w = ["a.transaction_type='R'", "(%s)" % awhere, "a.company_id='%d'" % CO, "a.tran_date <= '%s'" % CUT]
    if rowfilter == "baledrum":
        w.append("(round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)")
    elif rowfilter == "weight":
        w.append("round(b.StockQNT,2)!=0")
    elif rowfilter == "any":
        w.append("(round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0 OR round(b.StockQNT,2)!=0)")
    return ("""SELECT a.tran_date MR_Date, a.jute_receive_no MR_No, a.mr_print_no, a.branch_id, a.unit_conversion,
 a.quality_code Quality_ID, a.quality_name Quality, a.godown Godown_ID, a.godown_name Godown_Name,
 a.status_name Status, a.tran_status, a.item_code, a.uom, a.rate,
 b.MRLineNo MR_Line_No, b.Bales, b.IssueBales, b.SoldBales, b.BalesStock,
 b.Drums, b.DrumsIssued, b.DrumsSold, b.DrumsStock,
 b.ReceiptWt Receipt_Wt, b.IssuedWt Issued_Wt, b.SoldWt Sold_Wt, b.StockQNT Stock_Qnt
FROM vw_a a LEFT JOIN """ + bsql + " ON b.MRLineNo=a.mr_line_id WHERE " + " AND ".join(w) + " ORDER BY a.tran_date")

def tot(r, k): return round(sum(float(x[k] or 0) for x in r), 2)

def show(tag, r):
    print("%-58s lines=%5d closing=%11.2f (recv %.2f/iss %.2f/sold %.2f) bales=%.0f drums=%.0f"
          % (tag, len(r), tot(r, "Stock_Qnt"), tot(r, "Receipt_Wt"), tot(r, "Issued_Wt"), tot(r, "Sold_Wt"),
             tot(r, "BalesStock"), tot(r, "DrumsStock")))
    return r

print("=== corrected / extra alternatives (no row filter) ===")
show("B7  receipts: NO status filter (incl 4=REJ,6=CAN)", q(REP(B(), awhere="1=1")))
show("B7b receipts: exclude only 4 (REJECTED)", q(REP(B(), awhere="a.tran_status<>4")))
show("B9  branch 87 only", q(REP(B(), awhere="a.tran_status NOT IN (4,6) AND a.branch_id=87")))
show("B9b branch 23 (HO) only", q(REP(B(), awhere="a.tran_status NOT IN (4,6) AND a.branch_id=23")))
show("B9c branch NULL only", q(REP(B(), awhere="a.tran_status NOT IN (4,6) AND a.branch_id IS NULL")))
show("B12 MILL wt + include NULL-dated sales", q(REP(B(wt="mill_weight", incl_null_sales=True))))
show("B13 exclude DRAFT(1) invoices from sales", q(REP(B(extra="NOT (transaction_type='S' AND tran_status=1)"))))
show("B14 approved-only movements (I/S status 3)", q(REP(B(extra="transaction_type='R' OR tran_status=3"))))

allrows = q(REP(B(), rowfilter="none"))
pos = [r for r in allrows if float(r["Stock_Qnt"] or 0) > 0.004]
neg = [r for r in allrows if float(r["Stock_Qnt"] or 0) < -0.004]
zero = [r for r in allrows if abs(float(r["Stock_Qnt"] or 0)) <= 0.004]
print("\nper-line balance: %d lines  positive=%d (%.2f)  negative=%d (%.2f)  ~zero=%d"
      % (len(allrows), len(pos), round(sum(float(r["Stock_Qnt"]) for r in pos), 2),
         len(neg), round(sum(float(r["Stock_Qnt"]) for r in neg), 2), len(zero)))

stock = q(REP(B(), rowfilter="any"))
wcsv("A3_all_nonzero_lines", stock)

def nonzero(r):
    return not (abs(float(r["Stock_Qnt"] or 0)) <= 0.004
                and abs(float(r["BalesStock"] or 0)) < 0.5
                and abs(float(r["DrumsStock"] or 0)) < 0.5)

byq = collections.defaultdict(lambda: dict(n=0, b=0.0, d=0.0, w=0.0))
for r in allrows:
    if not nonzero(r): continue
    e = byq[(r["Quality_ID"], r["Quality"])]; e["n"] += 1
    e["b"] += float(r["BalesStock"] or 0); e["d"] += float(r["DrumsStock"] or 0); e["w"] += float(r["Stock_Qnt"] or 0)
print("\n=== CLOSING BY OLD QUALITY (roll-up of per-line closing) ===")
qrows = []
for (qid, qn), e in sorted(byq.items(), key=lambda x: -x[1]["w"]):
    print("  %-8s %-38s lines=%2d bales=%8.2f drums=%9.2f qtl=%10.2f" % (qid, qn or "(none)", e["n"], e["b"], e["d"], e["w"]))
    qrows.append(dict(quality_id=qid, quality_name=qn, lines=e["n"], bales=round(e["b"], 2), drums=round(e["d"], 2), qtl=round(e["w"], 2)))
print("  TOTAL qtl", round(sum(x["qtl"] for x in qrows), 2), " bales", round(sum(x["bales"] for x in qrows), 2), " drums", round(sum(x["drums"] for x in qrows), 2))
wcsv("by_quality", qrows)

byg = collections.defaultdict(lambda: dict(n=0, b=0.0, d=0.0, w=0.0))
for r in allrows:
    if not nonzero(r): continue
    e = byg[(r["Godown_ID"], r["Godown_Name"])]; e["n"] += 1
    e["b"] += float(r["BalesStock"] or 0); e["d"] += float(r["DrumsStock"] or 0); e["w"] += float(r["Stock_Qnt"] or 0)
print("\n=== CLOSING BY GODOWN ===")
grows = []
for (gid, gn), e in sorted(byg.items(), key=lambda x: -x[1]["w"]):
    print("  godown %-6s %-34s lines=%2d bales=%8.2f drums=%9.2f qtl=%10.2f" % (gid, (gn or "")[:34], e["n"], e["b"], e["d"], e["w"]))
    grows.append(dict(godown_id=gid, godown_name=gn, lines=e["n"], bales=round(e["b"], 2), drums=round(e["d"], 2), qtl=round(e["w"], 2)))
print("  TOTAL qtl", round(sum(x["qtl"] for x in grows), 2))
wcsv("by_godown", grows)

qw = q("""SELECT quality_code, quality_name,
   ROUND(SUM(bales_receipt)-SUM(bales_issued)-SUM(bales_sold),2) Opening_Bales,
   ROUND(SUM(drums_receipt)-SUM(drums_issued)-SUM(drums_sold),2) Opening_Drums,
   ROUND(SUM(accepted_weight)-SUM(weight_issued)-SUM(weight_sold),2) Opening_Wt
 FROM vw_a WHERE company_id=%d AND tran_date <= '%s' AND tran_status NOT IN (4,6)
 GROUP BY quality_code, quality_name
 HAVING Opening_Wt<>0 OR Opening_Bales<>0 OR Opening_Drums<>0
 ORDER BY Opening_Wt DESC""" % (CO, CUT))
print("\n=== OLD 'Jute Quality Wise (with value)' report, Opening as on cut: rows=%d total_wt=%.2f"
      % (len(qw), round(sum(float(r["Opening_Wt"] or 0) for r in qw), 2)))
wcsv("by_quality_oldreport", qw)
