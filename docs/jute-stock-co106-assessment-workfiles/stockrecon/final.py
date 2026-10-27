import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT="2026-03-31"; CO=106
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=2400)
cur=conn.cursor(); cur.execute("SET SESSION sql_mode=''")
def q(sql):
    cur.execute(sql); return cur.fetchall()
def wcsv(name, rows):
    if not rows: print("  (no rows)",name); return
    with open(os.path.join(OUT,name+".csv"),"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader()
        for r in rows: w.writerow(r)
    print("  wrote",name+".csv",len(rows),"rows")

for t in ("vw_a","vw_b","vw_c"):
    cur.execute("CREATE TEMPORARY TABLE %s AS SELECT * FROM view_jute_receipt_issue_sale" % t)
    cur.execute("CREATE INDEX ix_%s ON %s (mr_line_id)" % (t,t))
print("snapshots ready", q("SELECT COUNT(*) n FROM vw_a")[0]["n"])

def B(alias, wt="accepted_weight", upto=CUT, status=None, incl_null_sales=False, extra=""):
    cond = "tran_date <= '%s'" % upto
    if incl_null_sales: cond = "(%s OR (tran_date IS NULL AND transaction_type='S'))" % cond
    if status: cond += " AND tran_status IN (%s)" % status
    if extra: cond += " AND " + extra
    return """(SELECT mr_line_id AS MRLineNo,
   round(sum(bales_receipt),2) `Bales`, round(sum(bales_issued),2) `IssueBales`, round(sum(bales_sold),2) `SoldBales`,
   round(sum(bales_receipt)-sum(bales_issued)-sum(bales_sold),2) `BalesStock`,
   round(sum(drums_receipt),2) `Drums`, round(sum(drums_issued),2) `DrumsIssued`, round(sum(drums_sold),2) `DrumsSold`,
   round(sum(drums_receipt)-sum(drums_issued)-sum(drums_sold),2) `DrumsStock`,
   round(sum(%s),2) `ReceiptWt`, round(sum(weight_issued),2) `IssuedWt`, round(sum(weight_sold),2) `SoldWt`,
   round(sum(%s)-sum(weight_issued)-sum(weight_sold),2) `StockQNT`
 FROM %s WHERE %s GROUP BY mr_line_id) b""" % (wt, wt, alias, cond)

def REP(bsql, astat="NOT IN (4,6)", branch=None, rowfilter="baledrum", a_upto=CUT):
    w = ["a.transaction_type='R'", "a.tran_status %s" % astat, "a.company_id='%d'" % CO,
         "a.tran_date <= '%s'" % a_upto]
    if branch: w.append("a.branch_id=%d" % branch)
    if rowfilter=="baledrum": w.append("(round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)")
    elif rowfilter=="weight": w.append("round(b.StockQNT,2)!=0")
    elif rowfilter=="any": w.append("(round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0 OR round(b.StockQNT,2)!=0)")
    return """SELECT a.tran_date MR_Date, a.jute_receive_no MR_No, a.mr_print_no, a.branch_id, a.unit_conversion,
 a.quality_code Quality_ID, a.quality_name Quality, a.godown Godown_ID, a.godown_name Godown_Name,
 a.status_name Status, a.tran_status, a.item_code, a.uom, a.rate,
 b.MRLineNo MR_Line_No, b.Bales, b.IssueBales, b.SoldBales, b.BalesStock,
 b.Drums, b.DrumsIssued, b.DrumsSold, b.DrumsStock,
 b.ReceiptWt Receipt_Wt, b.IssuedWt Issued_Wt, b.SoldWt Sold_Wt, b.StockQNT Stock_Qnt
FROM vw_a a LEFT JOIN """ + bsql + """ ON b.MRLineNo=a.mr_line_id WHERE """ + " AND ".join(w) + " ORDER BY a.tran_date"

def tot(rows,k): return round(sum(float(r[k] or 0) for r in rows),2)
RES={}
def run(tag, sql, save=None):
    r=q(sql); RES[tag]=r
    print("%-64s lines=%5d closing=%12.2f  (recv %.2f / iss %.2f / sold %.2f)  bales=%.0f drums=%.0f"
          % (tag, len(r), tot(r,"Stock_Qnt"), tot(r,"Receipt_Wt"), tot(r,"Issued_Wt"), tot(r,"Sold_Wt"),
             tot(r,"BalesStock"), tot(r,"DrumsStock")))
    if save: wcsv(save, r)
    return r

print("\n=== A. OLD REPORT REPRODUCTION (Jute MR in Stock), co106, as on %s ===" % CUT)
base = run("A1 old report AS-IS (accepted wt, bale/drum<>0)", REP(B("vw_b")), save="A1_old_report_asis")
run("A2 same but row filter weight<>0", REP(B("vw_b"), rowfilter="weight"))
full = run("A3 same but bale/drum OR weight <>0 (full stock list)", REP(B("vw_b"), rowfilter="any"), save="A3_all_nonzero_lines")
run("A4 no row filter (every eligible MR line)", REP(B("vw_b"), rowfilter="none"))

print("\n=== B. ALTERNATIVE DEFINITIONS (all on the 'no row filter' basis so deltas are clean) ===")
run("B1 accepted weight   (old report basis)          [BASE]", REP(B("vw_b"), rowfilter="none"))
run("B2 MILL weight instead of accepted weight", REP(B("vw_b", wt="mill_weight"), rowfilter="none"))
run("B3 include NULL-dated sales", REP(B("vw_b", incl_null_sales=True), rowfilter="none"))
run("B4 receipts APPROVED(3) only", REP(B("vw_b"), astat="=3", rowfilter="none"))
run("B5 receipts exclude 4,6 AND 24 (transfer-pending)", REP(B("vw_b"), astat="NOT IN (4,6,24)", rowfilter="none"))
run("B6 receipts exclude 4,6,1,21,24", REP(B("vw_b"), astat="NOT IN (4,6,1,21,24)", rowfilter="none"))
run("B7 receipts: no status filter at all (incl 4,6)", REP(B("vw_b"), astat="IS NOT NULL OR 1=1", rowfilter="none"))
run("B8 movements also status-filtered (b excl 4,6)", REP(B("vw_b", extra="tran_status NOT IN (4,6)"), rowfilter="none"))
run("B9 branch 87 only (MR header branch)", REP(B("vw_b"), branch=87, rowfilter="none"))
run("B10 branch 87 only + bale/drum<>0 row filter", REP(B("vw_b"), branch=87, rowfilter="baledrum"))
run("B11 MILL wt + NULL-dated sales + no status filter", REP(B("vw_b", wt="mill_weight", incl_null_sales=True), astat="IS NOT NULL OR 1=1", rowfilter="none"))
