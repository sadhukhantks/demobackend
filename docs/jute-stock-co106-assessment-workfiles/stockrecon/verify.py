import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
CUT = "2026-03-31"
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=2400)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")
def q(sql, t=None):
    cur.execute(sql); r = cur.fetchall()
    if t: print("\n---", t)
    for x in r[:60]: print("   ", x)
    if len(r) > 60: print("    ...", len(r), "rows")
    return r

q("""SELECT transaction_type, tran_status, status_name, mr_line_id, tran_date, quality_code, quality_name,
   bales_sold, bales_issued, weight_sold, weight_issued, jute_receive_no, branch_id
 FROM view_jute_receipt_issue_sale
 WHERE company_id=106 AND tran_date<='%s' AND tran_status NOT IN (4,6) AND quality_code IN (814,992)""" % CUT,
  "the -96.00 / -5.00 quality rows: where do they come from?")

q("""SELECT l.jute_line_item_no, h.jute_receive_no, h.jute_receive_dt, h.company_id, h.branch_id,
   h.mr_good_recept_status, l.is_active, l.actual_quality, l.actual_bale, l.actual_weight, l.accepted_weight
 FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
 WHERE l.jute_line_item_no IN (7950,29343,43289,43340,44899,40725)""",
  "the never-moved / oddity lines (raw)")

q("""SELECT ih.invoice_id, ih.invoice_no_string, ih.invoice_date, ih.status, ih.is_active, ih.company_id, ih.branch_id,
   ih.invoice_type, ih.mr_id, il.mr_line_id, il.sales_weight, il.sales_bale, il.quality_id, il.quality_name
 FROM invoice_hdr ih JOIN invoice_line_items il ON il.invoice_id=ih.invoice_id
 WHERE il.mr_line_id IN (37538,37539)""", "the NULL-dated sale invoice (113.10 qtl)")

q("""SELECT ROUND(SUM(accepted_weight)-SUM(weight_issued)-SUM(weight_sold),2) stock_today_qtl,
   ROUND(SUM(bales_receipt)-SUM(bales_issued)-SUM(bales_sold),2) bales_today,
   ROUND(SUM(drums_receipt)-SUM(drums_issued)-SUM(drums_sold),2) drums_today
 FROM view_jute_receipt_issue_sale WHERE company_id=106 AND tran_status NOT IN (4,6)""",
  "old-system TOTAL stock TODAY (no date cut), quality-report basis")

q("""SELECT w.id, w.name, w.address, w.company_id, w.branch_id FROM warehouse_details w WHERE w.id IN (244,356)""",
  "godowns")

q("""SELECT h.branch_id, b.branch_name, COUNT(*) n FROM scm_mr_hdr h
 LEFT JOIN branch_master b ON b.branch_id=h.branch_id WHERE h.company_id=106 GROUP BY h.branch_id, b.branch_name""",
  "co106 MR header branches")

# the 31-Mar zeroing batch
q("""SELECT COUNT(*) n, ROUND(SUM(total_weight),2) qtl, MIN(issue_no) lo, MAX(issue_no) hi, MIN(create_date_time) c1,
   MAX(create_date_time) c2, branch_id
 FROM jute_issue WHERE company_id=106 AND is_active=1 AND issue_date='%s' GROUP BY branch_id""" % CUT,
  "issues booked ON the cut date (the branch-87 zero-out batch)")

# cross-check into sls
cur.execute("SET SESSION sql_mode=''")
ids = [r["MR_Line_No"] for r in csv.DictReader(open(os.path.join(OUT, "A3_all_nonzero_lines.csv"), encoding="utf-8"))]
IN = ",".join(ids)
q("""SELECT li.jute_mr_li_id, li.jute_mr_id, li.actual_qty, li.actual_weight, li.accepted_weight, li.active, li.status,
   mr.branch_id, mr.co_id
 FROM sls.jute_mr_li li JOIN sls.jute_mr mr ON mr.jute_mr_id=li.jute_mr_id
 WHERE li.jute_mr_li_id IN (%s) ORDER BY li.jute_mr_li_id""" % IN,
  "same 28 lines in the NEW system (sls.jute_mr_li)")
q("""SELECT COUNT(*) n, ROUND(SUM(actual_weight),2) wt FROM sls.jute_mr_li WHERE jute_mr_li_id IN (%s)""" % IN,
  "sls coverage of the 28 stock lines")
