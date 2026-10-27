import pymysql, csv, os
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
OUT = "C:/Users/silve/AppData/Local/Temp/claude/c--code-vowerp3be/1dccdffc-6aa5-43ba-97f1-40e8dadc8a38/scratchpad/old31/task2"
def conn(db="vowsls"):
    c = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
        password=env["DATABASE_PASSWORD"], database=db, cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    c.cursor().execute("SET SESSION sql_mode=''")
    return c
def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()
def dump(name, rows):
    p = os.path.join(OUT, name)
    if not rows:
        open(p,"w").write("(none)\n"); return p
    with open(p,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    return p
