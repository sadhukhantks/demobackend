import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=1200)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")
def dump(name, sql):
    cur.execute(sql); rows = cur.fetchall()
    print(name, len(rows))
    if rows:
        with open(os.path.join(OUT,name+".csv"),"w",newline="",encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
            for r in rows: w.writerow(r)
    return rows

pass
 h.company_id,h.unit_conversion,h.gate_entry_no,h.supp_code,h.mukam_id,h.po_num,h.vehicle_no,h.ac_year
 FROM scm_mr_hdr h WHERE h.company_id=106""")

dump("line106", """SELECT l.jute_line_item_no,l.jute_receive_no,l.item_code,l.actual_quality,l.advised_quality,
 l.quantity,l.quantity_unit,l.actual_bale,l.actual_loose,l.actual_weight,l.accepted_weight,l.shortage_kgs,
 l.rate,l.jute_issue_rate,l.warehouse_no,l.is_active,l.status,l.company_id,
 l.sales_bales,l.sales_drums,l.sales_weight,l.bales_consumed,l.drums_consumed,l.weight_consumed,l.crop_year,l.marka
 FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106""")

dump("issue106", """SELECT i.issue_no,i.issue_date,i.mr_no,i.stock_id,i.quantity,i.total_weight,i.issue_value,
 i.issue_status,i.is_active,i.company_id,i.branch_id,i.godown_no,i.bale_loose,i.no_bales,i.fin_year,i.jute_quality,i.jute_type
 FROM jute_issue i WHERE i.stock_id IN (SELECT l.jute_line_item_no FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106)""")

dump("inv106", """SELECT il.invoice_line_item_id,il.invoice_id,il.mr_line_id,il.sales_bale,il.sales_drum,il.sales_weight,
 il.quality_id,il.item_id,il.quantity,il.rate,il.is_active,
 ih.invoice_date,ih.status,ih.is_active AS hdr_active,ih.company_id,ih.branch_id,ih.invoice_type,ih.mr_id,ih.invoice_no_string,ih.mr_no,ih.customer_name
 FROM invoice_line_items il JOIN invoice_hdr ih ON ih.invoice_id=il.invoice_id
 WHERE il.mr_line_id IN (SELECT l.jute_line_item_no FROM scm_mr_line_item l JOIN scm_mr_hdr h ON h.jute_receive_no=l.jute_receive_no WHERE h.company_id=106)""")

dump("qual", "SELECT id, jute_quality, item_code FROM jute_quality_price_master")
dump("wh", "SELECT id,name,address,company_id,branch_id,type FROM warehouse_details")
