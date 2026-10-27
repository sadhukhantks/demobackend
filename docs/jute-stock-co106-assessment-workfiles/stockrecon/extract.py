import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = r"C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/stockrecon"
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor,
                       autocommit=True, read_timeout=1200)
cur = conn.cursor(); cur.execute("SET SESSION sql_mode=''")
cols = ("jute_receive_no,company_id,ac_year,tran_date,mr_print_no,tran_status,status_name,unit_conversion,"
        "branch_id,mr_line_id,item_code,quality_code,quality_name,uom,rate,godown,godown_name,"
        "bales_receipt,bales_sold,bales_issued,drums_receipt,drums_sold,drums_issued,"
        "mill_weight,accepted_weight,weight_sold,weight_issued,transaction_type")
print("querying view ...")
cur.execute("SELECT %s FROM view_jute_receipt_issue_sale" % cols)
rows = cur.fetchall()
print("view rows:", len(rows))
with open(os.path.join(OUT,"view_all.csv"),"w",newline="",encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols.split(","))
    w.writeheader()
    for r in rows: w.writerow(r)
print("wrote view_all.csv")
