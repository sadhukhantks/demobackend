import pymysql
from dotenv import dotenv_values
e = dotenv_values("c:/code/vowerp3be/env/database.env")
cn = pymysql.connect(host=e["DATABASE_HOST"], port=int(e["DATABASE_PORT"]), user=e["DATABASE_USER"],
                     password=e["DATABASE_PASSWORD"], database="vowsls",
                     cursorclass=pymysql.cursors.DictCursor, autocommit=True)
c = cn.cursor(); c.execute("SET SESSION sql_mode=''")
def run(sql, label):
    c.execute(sql); rows = c.fetchall()
    print("\n== " + label)
    if not rows: print("(0 rows)"); return rows
    print(" | ".join(rows[0].keys()))
    for r in rows[:40]: print(" | ".join("" if v is None else str(v) for v in r.values()))
    if len(rows) > 40: print(f"... {len(rows)} rows total")
    return rows

c.execute("""CREATE TEMPORARY TABLE vw1 AS SELECT mr_line_id, bales_receipt,bales_issued,bales_sold,
 drums_receipt,drums_issued,drums_sold,accepted_weight,weight_issued,weight_sold
 FROM view_jute_receipt_issue_sale WHERE tran_date <= '2026-03-31'""")
c.execute("""CREATE TEMPORARY TABLE agg AS SELECT mr_line_id MRLineNo,
 round(sum(bales_receipt)-sum(bales_issued)-sum(bales_sold),2) BalesStock,
 round(sum(drums_receipt)-sum(drums_issued)-sum(drums_sold),2) DrumsStock,
 round(sum(accepted_weight),2) ReceiptWt, round(sum(weight_issued),2) IssuedWt,
 round(sum(weight_sold),2) SoldWt,
 round(sum(accepted_weight)-sum(weight_issued)-sum(weight_sold),2) StockQNT FROM vw1 GROUP BY mr_line_id""")
c.execute("CREATE INDEX ix ON agg (MRLineNo)")
c.execute("""CREATE TEMPORARY TABLE rrow AS SELECT a.mr_line_id, a.jute_receive_no, a.branch_id, a.tran_status,
 a.quality_code, a.quality_name, a.godown, a.godown_name, a.tran_date
 FROM view_jute_receipt_issue_sale a WHERE a.transaction_type='R' AND a.tran_status NOT IN (4,6)
 AND a.company_id='106' AND a.tran_date <= '2026-03-31'""")

BASE = """SELECT count(*) n_lines, round(sum(b.StockQNT),2) qtl, round(sum(b.BalesStock),0) bales,
 round(sum(b.DrumsStock),0) drums, round(sum(b.ReceiptWt),2) recv, round(sum(b.IssuedWt),2) iss,
 round(sum(b.SoldWt),2) sold FROM rrow a JOIN agg b ON b.MRLineNo=a.mr_line_id WHERE 1=1 """
for lbl, w in [("A1 old screen as shipped (bale/drum filter)", "AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0)"),
               ("A2 weight!=0 filter", "AND round(b.StockQNT,2)!=0"),
               ("A3 bale/drum OR weight", "AND (round(b.BalesStock,0)!=0 OR round(b.DrumsStock,0)!=0 OR round(b.StockQNT,2)!=0)"),
               ("A4 no row filter = company closing", "")]:
    run(BASE + w, lbl)

run("""SELECT a.branch_id, count(*) n_lines, round(sum(b.ReceiptWt),2) recv, round(sum(b.IssuedWt),2) iss,
 round(sum(b.SoldWt),2) sold, round(sum(b.StockQNT),2) closing
 FROM rrow a JOIN agg b ON b.MRLineNo=a.mr_line_id GROUP BY a.branch_id WITH ROLLUP""", "branch slice (old MR header branch)")

run("""SELECT il.mr_line_id, ih.invoice_id, ih.invoice_no_string, ih.status hdr_status, ih.is_active hdr_active,
 il.is_active line_active, ih.invoice_date, round(sum(il.sales_weight),2) qtl,
 (SELECT count(*) FROM scm_mr_line_item x WHERE x.jute_line_item_no=il.mr_line_id) line_exists,
 (SELECT h.company_id FROM scm_mr_line_item x JOIN scm_mr_hdr h ON h.jute_receive_no=x.jute_receive_no
   WHERE x.jute_line_item_no=il.mr_line_id) line_company
 FROM invoice_line_items il JOIN invoice_hdr ih ON ih.invoice_id=il.invoice_id
 WHERE ih.company_id=106 AND il.mr_line_id IN (7949,7950,7952,11279,11280)
 GROUP BY il.mr_line_id, ih.invoice_id, ih.invoice_no_string, ih.status, ih.is_active, il.is_active, ih.invoice_date""",
 "sales against the disputed mr_line_ids 7949/7950/7952/11279/11280")

run("""SELECT l.jute_line_item_no, l.jute_receive_no, h.company_id, h.branch_id, h.mr_good_recept_status,
 l.is_active, l.actual_weight mill_qtl, l.accepted_weight accepted_kg,
 (SELECT count(*) FROM jute_issue i WHERE i.stock_id=l.jute_line_item_no AND i.is_active=1) issues,
 (SELECT ifnull(sum(il.sales_weight),0) FROM invoice_line_items il WHERE il.mr_line_id=l.jute_line_item_no AND il.is_active=1) sold_qtl
 FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no
 WHERE l.jute_line_item_no IN (7949,7950,7952,29343)""", "the four contested MR lines")

run("""SELECT CASE WHEN ih.invoice_date IS NULL THEN 'NULL-dated' WHEN ih.status='3' THEN 'approved'
   WHEN ih.status IN ('0','1') THEN 'draft/open' WHEN ih.status='4' THEN 'rejected hdr' ELSE ih.status END bucket,
 CASE WHEN il.is_active=1 THEN 'line active' ELSE 'line inactive' END la,
 CASE WHEN x.jute_line_item_no IS NULL THEN 'dangling mr_line' ELSE 'resolves' END res,
 count(*) n_lines, round(sum(il.sales_weight),2) qtl
 FROM invoice_line_items il JOIN invoice_hdr ih ON ih.invoice_id=il.invoice_id
 LEFT JOIN scm_mr_line_item x ON x.jute_line_item_no=il.mr_line_id
 LEFT JOIN scm_mr_hdr xh ON xh.jute_receive_no=x.jute_receive_no
 WHERE ih.company_id=106 AND il.quality_id>0 AND (ih.invoice_date IS NULL OR ih.invoice_date<='2026-03-31')
 GROUP BY 1,2,3 ORDER BY 5 DESC""", "sales universe decomposition (co106, <=cut or NULL date)")
