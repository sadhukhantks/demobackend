import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT = "2026-03-31"
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=2400)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")
def q(sql, t=None, n=60):
    cur.execute(sql); r = cur.fetchall()
    if t: print("\n---", t)
    for x in r[:n]: print("   ", x)
    if len(r) > n: print("    ...", len(r), "rows")
    return r

q("""SELECT quality_code, quality_name, transaction_type, COUNT(*) n,
   ROUND(SUM(accepted_weight),2) recv, ROUND(SUM(weight_issued),2) iss, ROUND(SUM(weight_sold),2) sold
 FROM view_jute_receipt_issue_sale
 WHERE company_id=106 AND tran_date<='%s' AND tran_status NOT IN (4,6) AND quality_code IN (814,992)
 GROUP BY quality_code, quality_name, transaction_type""" % CUT,
  "the -96.00 / -5.00 quality rows, by transaction type")

q("""SELECT il.mr_line_id, ih.invoice_id, ih.invoice_no_string, ih.invoice_date, ih.company_id, ih.branch_id,
   il.quality_id, il.quality_name, il.sales_weight, il.sales_bale,
   (SELECT h2.company_id FROM scm_mr_line_item l2 JOIN scm_mr_hdr h2 ON h2.jute_receive_no=l2.jute_receive_no
      WHERE l2.jute_line_item_no=il.mr_line_id) line_owner_company,
   (SELECT h2.mr_good_recept_status FROM scm_mr_line_item l2 JOIN scm_mr_hdr h2 ON h2.jute_receive_no=l2.jute_receive_no
      WHERE l2.jute_line_item_no=il.mr_line_id) line_mr_status
 FROM invoice_line_items il JOIN invoice_hdr ih ON ih.invoice_id=il.invoice_id
 WHERE ih.company_id=106 AND il.is_active=1 AND il.quality_id IN (814,992) AND ih.invoice_date<='%s'""" % CUT,
  "sales rows carrying quality 814/992")

# sls cross-check
cols = q("""SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='sls' AND TABLE_NAME='jute_mr'
  AND COLUMN_NAME IN ('co_id','company_id','branch_id')""", "sls.jute_mr key cols")
ids = [r["MR_Line_No"] for r in csv.DictReader(open(os.path.join(OUT, "A3_all_nonzero_lines.csv"), encoding="utf-8"))]
IN = ",".join(ids)
q("""SELECT COUNT(*) lines_present, ROUND(SUM(actual_weight),2) sum_actual_weight_new,
   SUM(active=1) active_lines FROM sls.jute_mr_li WHERE jute_mr_li_id IN (%s)""" % IN,
  "the 28 old stock lines, presence in sls.jute_mr_li")
q("""SELECT li.jute_mr_li_id, li.jute_mr_id, li.actual_qty, li.actual_weight, li.active, li.status, mr.branch_id
 FROM sls.jute_mr_li li JOIN sls.jute_mr mr ON mr.jute_mr_id=li.jute_mr_id
 WHERE li.jute_mr_li_id IN (%s) ORDER BY li.jute_mr_li_id""" % IN, "detail", n=40)
