"""Apply a dbqueries/migrations/*.sql script to a tenant DB.

Strips comment lines per statement (the naive "startswith('--')" skip would drop a
statement that carries a comment header, which this migration does).
"""
import os
import sys
import time
import pymysql
from dotenv import load_dotenv

load_dotenv(r'D:\vownextjs\vowerp3be\.env')
script, target_db = sys.argv[1], sys.argv[2]

with open(script, encoding='utf-8') as f:
    raw = f.read()

statements = []
for chunk in raw.split(';'):
    # drop comment-only lines so a statement with a comment header still executes
    body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith('--')).strip()
    if body:
        statements.append(body)

print(f"target db : {target_db}")
print(f"script    : {script}")
print(f"statements: {len(statements)}")
for s in statements:
    print("-" * 60)
    print(s)
print("-" * 60)

con = pymysql.connect(
    host=os.getenv('DATABASE_HOST'), user=os.getenv('DATABASE_USER'),
    password=os.getenv('DATABASE_PASSWORD'), port=int(os.getenv('DATABASE_PORT')),
    database=target_db, connect_timeout=20, read_timeout=1800,
)
cur = con.cursor()
for i, stmt in enumerate(statements, 1):
    t0 = time.time()
    try:
        cur.execute(stmt)
        print(f"[{i}/{len(statements)}] OK in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[{i}/{len(statements)}] FAILED after {time.time()-t0:.1f}s: {e}")
        print("   statement:", stmt[:400])
        con.rollback()
        con.close()
        sys.exit(1)
con.commit()
con.close()
print("Migration applied successfully")
