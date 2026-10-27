import pymysql, csv, os
from dotenv import dotenv_values
E = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = "C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task3"
CUT = "2026-03-31"

def conn():
    c = pymysql.connect(host=E["DATABASE_HOST"], port=int(E["DATABASE_PORT"]), user=E["DATABASE_USER"],
                        password=E["DATABASE_PASSWORD"], cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    cur = c.cursor(); cur.execute("SET SESSION sql_mode=''")
    return c

def q(c, sql, args=None):
    cur = c.cursor(); cur.execute(sql, args or ())
    return cur.fetchall()

def csvout(name, rows):
    p = os.path.join(OUT, name)
    if not rows:
        open(p, "w", encoding="utf-8").write("(empty)\n"); return p
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    return p
