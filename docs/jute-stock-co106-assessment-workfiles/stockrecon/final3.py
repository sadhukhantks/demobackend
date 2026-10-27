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

lines = list(csv.DictReader(open(os.path.join(OUT, "A3_all_nonzero_lines.csv"), encoding="utf-8")))
ids = [r["MR_Line_No"] for r in lines]
IN = ",".join(ids)
print("stock lines:", len(ids))

# post-cut movement in OLD system, per line
post_i = {str(r["stock_id"]): r for r in q("""SELECT stock_id, COUNT(*) n, ROUND(SUM(total_weight),2) wt,
    ROUND(SUM(quantity),2) qty, MIN(issue_date) d1, MAX(issue_date) d2, GROUP_CONCAT(issue_no ORDER BY issue_date) nos
  FROM jute_issue WHERE is_active=1 AND issue_date > '%s' AND stock_id IN (%s) GROUP BY stock_id""" % (CUT, IN))}
post_s = {str(r["mr_line_id"]): r for r in q("""SELECT il.mr_line_id, COUNT(*) n, ROUND(SUM(il.sales_weight),2) wt,
    ROUND(SUM(il.sales_bale),2) bl, ROUND(SUM(il.sales_drum),2) dr, MIN(ih.invoice_date) d1, MAX(ih.invoice_date) d2,
    GROUP_CONCAT(DISTINCT ih.invoice_no_string) inv
  FROM invoice_line_items il JOIN invoice_hdr ih ON ih.invoice_id=il.invoice_id
  WHERE il.is_active=1 AND il.quality_id>0 AND il.mr_line_id IN (%s)
    AND (ih.invoice_date > '%s' OR ih.invoice_date IS NULL) GROUP BY il.mr_line_id""" % (IN, CUT))}
# balance TODAY in the old system (same formula, no date cut)
today = {str(r["mr_line_id"]): r for r in q("""SELECT mr_line_id,
    ROUND(SUM(bales_receipt)-SUM(bales_issued)-SUM(bales_sold),2) bs,
    ROUND(SUM(drums_receipt)-SUM(drums_issued)-SUM(drums_sold),2) ds,
    ROUND(SUM(accepted_weight)-SUM(weight_issued)-SUM(weight_sold),2) ws
  FROM view_jute_receipt_issue_sale WHERE mr_line_id IN (%s) GROUP BY mr_line_id""" % IN)}
# raw line info
raw = {str(r["jute_line_item_no"]): r for r in q("""SELECT l.jute_line_item_no, l.jute_receive_no, l.actual_bale, l.actual_loose,
    l.actual_weight, l.accepted_weight, l.shortage_kgs, l.warehouse_no, l.is_active, l.status, l.marka, l.crop_year,
    l.sales_bales, l.sales_drums, l.sales_weight, l.bales_consumed, l.drums_consumed, l.weight_consumed,
    h.jute_receive_dt, h.mr_print_no, h.mr_good_recept_status, h.branch_id, h.unit_conversion, h.gate_entry_no
  FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
  WHERE l.jute_line_item_no IN (%s)""" % IN)}

def f(v):
    try: return float(v or 0)
    except: return 0.0

out = []
print("\n%-8s %-8s %-10s %-11s %-5s %-22s %8s %8s %10s %10s %10s %10s"
      % ("LINE", "MR", "MR_PRINT", "MR_DATE", "UNIT", "QUALITY", "R_BALE", "R_DRUM", "RECV_QTL", "ISS_QTL", "SOLD_QTL", "BAL_QTL"))
for r in sorted(lines, key=lambda x: -f(x["Stock_Qnt"])):
    lid = r["MR_Line_No"]; rw = raw.get(lid, {})
    pi = post_i.get(lid); ps = post_s.get(lid); td = today.get(lid, {})
    fate = []
    if pi: fate.append("issued after cut: %s qtl in %d issue(s) %s..%s (issue_no %s)"
                       % (pi["wt"], pi["n"], str(pi["d1"])[:10], str(pi["d2"])[:10], str(pi["nos"])[:60]))
    if ps: fate.append("sold after cut: %s qtl / %s bales / %s drums, %d line(s) %s..%s inv %s"
                       % (ps["wt"], ps["bl"], ps["dr"], ps["n"], str(ps["d1"])[:10], str(ps["d2"])[:10], str(ps["inv"])[:60]))
    if not fate: fate.append("NO further movement in the old system")
    fate.append("old-system balance today: %s qtl / %s bales / %s drums" % (td.get("ws"), td.get("bs"), td.get("ds")))
    rec = dict(
        jute_line_item_no=lid, jute_receive_no=r["MR_No"], mr_print_no=r["mr_print_no"],
        mr_date=str(r["MR_Date"])[:10], mr_status=r["Status"], branch_id=r["branch_id"],
        quality_id=r["Quality_ID"], quality=r["Quality"], unit=r["unit_conversion"],
        godown_id=r["Godown_ID"], godown=r["Godown_Name"], marka=rw.get("marka"), crop_year=rw.get("crop_year"),
        receipt_bales=r["Bales"], receipt_drums=r["Drums"],
        receipt_qtl_accepted=r["Receipt_Wt"], receipt_qtl_mill=rw.get("actual_weight"),
        shortage_kgs=rw.get("shortage_kgs"),
        issued_bales=r["IssueBales"], issued_drums=r["DrumsIssued"], issued_qtl=r["Issued_Wt"],
        sold_bales=r["SoldBales"], sold_drums=r["DrumsSold"], sold_qtl=r["Sold_Wt"],
        bal_bales=r["BalesStock"], bal_drums=r["DrumsStock"], bal_qtl=r["Stock_Qnt"],
        post_cut_issued_qtl=(pi["wt"] if pi else 0), post_cut_issues=(pi["n"] if pi else 0),
        post_cut_sold_qtl=(ps["wt"] if ps else 0), post_cut_sale_lines=(ps["n"] if ps else 0),
        bal_today_qtl=td.get("ws"), bal_today_bales=td.get("bs"), bal_today_drums=td.get("ds"),
        fate="; ".join(fate))
    out.append(rec)
    print("%-8s %-8s %-10s %-11s %-5s %-22s %8s %8s %10s %10s %10s %10s"
          % (lid, r["MR_No"], (r["mr_print_no"] or "")[:10], str(r["MR_Date"])[:10], (r["unit_conversion"] or "")[:5],
             (r["Quality"] or "")[:22], r["Bales"], r["Drums"], r["Receipt_Wt"], r["Issued_Wt"], r["Sold_Wt"], r["Stock_Qnt"]))
    print("         -> " + " | ".join(fate))

with open(os.path.join(OUT, "old_stock_31mar2026_co106_lines.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys())); w.writeheader()
    for x in out: w.writerow(x)
print("\nwrote old_stock_31mar2026_co106_lines.csv", len(out))
print("TOTALS  recv %.2f  iss %.2f  sold %.2f  bal %.2f qtl   bales %.0f  drums %.0f"
      % tuple(round(sum(f(x[k]) for x in out), 2) for k in
              ("receipt_qtl_accepted", "issued_qtl", "sold_qtl", "bal_qtl", "bal_bales", "bal_drums")))
print("post-cut: issued %.2f qtl, sold %.2f qtl, still on hand today %.2f qtl"
      % (round(sum(f(x["post_cut_issued_qtl"]) for x in out), 2),
         round(sum(f(x["post_cut_sold_qtl"]) for x in out), 2),
         round(sum(f(x["bal_today_qtl"]) for x in out), 2)))

# quality-wise old report rows
print("\n=== OLD quality-wise report rows (Opening as on cut, strict old SQL) ===")
for r in csv.DictReader(open(os.path.join(OUT, "by_quality_oldreport.csv"), encoding="utf-8")):
    print("  %-8s %-32s bales=%9s drums=%9s qtl=%10s" % (r["quality_code"], (r["quality_name"] or "")[:32],
                                                         r["Opening_Bales"], r["Opening_Drums"], r["Opening_Wt"]))
