import pymysql, csv, os, sys
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT = "2026-03-31"; CO = 106
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=2400)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")
def run(sql, show=200, name=None):
    cur.execute(sql); rows = cur.fetchall()
    if name and rows:
        with open(os.path.join(OUT,name+".csv"),"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader()
            for r in rows: w.writerow(r)
    return rows

print("materialising view snapshots ...")
cur.execute("CREATE TEMPORARY TABLE vw_a AS SELECT * FROM view_jute_receipt_issue_sale")
cur.execute("CREATE TEMPORARY TABLE vw_b AS SELECT * FROM view_jute_receipt_issue_sale")
cur.execute("CREATE INDEX ix1 ON vw_a (mr_line_id)"); cur.execute("CREATE INDEX ix2 ON vw_b (mr_line_id)")
print(run("SELECT COUNT(*) n FROM vw_a")[0])

BSUB = """(SELECT mr_line_id AS MRLineNo,
   round(sum(bales_receipt),2) AS `Bales`,
   round(sum(bales_issued),2) AS `IssueBales`,
   round(sum(bales_sold),2) AS `SoldBales`,
   round(sum(bales_receipt)-sum(bales_issued)-sum(bales_sold),2) AS `BalesStock`,
   round(sum(drums_receipt),2) AS `Drums`,
   round(sum(drums_issued),2) AS `DrumsIssued`,
   round(sum(drums_sold),2) AS `DrumsSold`,
   round(sum(drums_receipt)-sum(drums_issued)-sum(drums_sold),2) AS `DrumsStock`,
   round(sum(accepted_weight),2) AS `ReceiptWt`,
   round(sum(weight_issued),2) AS `IssuedWt`,
   round(sum(weight_sold),2) AS `SoldWt`,
   round(sum(accepted_weight)-sum(weight_issued)-sum(weight_sold),2) AS `StockQNT`
 FROM vw_b WHERE tran_date <= '%s' GROUP BY mr_line_id) b""" % CUT

REPORT = """SELECT a.tran_date AS MR_Date, a.jute_receive_no AS MR_No, a.mr_print_no, a.branch_id, a.unit_conversion,
 a.quality_code AS Quality_ID, a.quality_name AS Quality, a.godown AS Godown_ID, a.godown_name AS Godown_Name,
 a.status_name AS Status, a.tran_status, a.item_code, a.uom, a.rate,
 b.MRLineNo AS MR_Line_No, b.Bales, b.IssueBales, b.SoldBales, b.BalesStock,
 b.Drums, b.DrumsIssued, b.DrumsSold, b.DrumsStock,
 b.ReceiptWt AS Receipt_Wt, b.IssuedWt AS Issued_Wt, b.SoldWt AS Sold_Wt, b.StockQNT AS Stock_Qnt
FROM vw_a a LEFT JOIN """ + BSUB + """ ON b.MRLineNo = a.mr_line_id
WHERE a.transaction_type='R' AND a.tran_status NOT IN (4,6) AND a.company_id='%d'
 AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)
 AND a.tran_date <= '%s'
ORDER BY a.tran_date DESC""" % (CO, CUT)

rows = run(REPORT, name="old_report_asis")
print("\n=== OLD 'Jute MR in Stock' report, co %d, as on %s ===" % (CO, CUT))
print("rows:", len(rows))
def s(k): return round(sum(float(r[k] or 0) for r in rows),2)
print("Receipt_Wt %.2f  Issued_Wt %.2f  Sold_Wt %.2f  Stock_Qnt %.2f  Bales %.2f  Drums %.2f"
      % (s("Receipt_Wt"), s("Issued_Wt"), s("Sold_Wt"), s("Stock_Qnt"), s("BalesStock"), s("DrumsStock")))

# variant: weight-based row filter
REPORT_W = REPORT.replace("AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)", "AND round(b.StockQNT,2)!=0")
rw = run(REPORT_W, name="old_report_weightfilter")
print("\n[weight!=0 row filter] rows:", len(rw), " Stock_Qnt %.2f" % round(sum(float(r["Stock_Qnt"] or 0) for r in rw),2))

REPORT_ANY = REPORT.replace("AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)",
                            "AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0 OR round(b.StockQNT,2)!=0)")
ra = run(REPORT_ANY, name="old_report_anyfilter")
print("[bale/drum OR weight !=0] rows:", len(ra), " Stock_Qnt %.2f" % round(sum(float(r["Stock_Qnt"] or 0) for r in ra),2))

REPORT_ALL = REPORT.replace("AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)", "")
rall = run(REPORT_ALL, name="old_report_nofilter")
print("[no row filter, all R lines] rows:", len(rall), " Stock_Qnt %.2f  Receipt %.2f Issued %.2f Sold %.2f"
      % tuple(round(sum(float(r[k] or 0) for r in rall),2) for k in ("Stock_Qnt","Receipt_Wt","Issued_Wt","Sold_Wt")))
