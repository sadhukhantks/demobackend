import pymysql, sys
from dotenv import dotenv_values
env = dotenv_values("c:/code/vowerp3be/env/database.env")
conn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
                       password=env["DATABASE_PASSWORD"], database="vowsls", cursorclass=pymysql.cursors.DictCursor, autocommit=True)
cur = conn.cursor()
views = sys.argv[1:]
out=[]
for v in views:
    try:
        cur.execute("SHOW CREATE VIEW `%s`" % v)
        r = cur.fetchone()
        txt = r.get("Create View") or r.get("Create Table")
        out.append("=== %s ===\n%s\n" % (v, txt))
    except Exception as e:
        out.append("=== %s === ERROR %s\n" % (v, e))
print("\n".join(out))
