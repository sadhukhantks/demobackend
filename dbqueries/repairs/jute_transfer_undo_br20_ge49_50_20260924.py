"""
Undo two juteTransfer hops on tenant sls (owner request, 2026-09-24).

Deletes the Jagrati (co 74, branch 20 FACTORY) MRs that were created as
transfer children of two EJM (co 2, branch 29 FACTORY) gate entries, and
puts the EJM sources back to OPEN (1) the same way reopen_mr does
(status 1, approval_level/bill-pass/totals cleared, branch_mr_no kept).

    branch-20 child        EJM source
    28338  GE 49 / MR 49   28267  GE 51  (already status 1 OPEN)
    28339  GE 50 / MR 50   28268  GE 52  (status 13 PENDING -> 1 OPEN)

Child lines 45795, 45796, 45797 have no issues, no moisture readings,
no lot-src rows, no sales rows, no attachments (verified 2026-09-24).

Usage (cwd = vowerp3be, env/database.env exported):
    uv run python dbqueries/repairs/jute_transfer_undo_br20_ge49_50_20260924.py           # dry run
    uv run python dbqueries/repairs/jute_transfer_undo_br20_ge49_50_20260924.py --apply   # execute
A rollback SQL file (full row backups as INSERTs + status restore) is written
next to this script before anything is changed.
"""
import os, sys, datetime, pymysql

TENANT = "sls"
CHILD_MR_IDS = (28338, 28339)
SRC_RESTORE = {28267: 28338, 28268: 28339}   # src -> expected child
UPDATED_BY = 1
APPLY = "--apply" in sys.argv


def lit(v):
    if v is None: return "NULL"
    if isinstance(v, (int, float)): return repr(v)
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds()); return "'%02d:%02d:%02d'" % (s // 3600, s % 3600 // 60, s % 60)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def insert_sql(table, rows):
    out = []
    for r in rows:
        cols = ", ".join(f"`{c}`" for c in r)
        vals = ", ".join(lit(v) for v in r.values())
        out.append(f"INSERT INTO {table} ({cols}) VALUES ({vals});")
    return out


conn = pymysql.connect(host=os.environ["DATABASE_HOST"], port=int(os.environ["DATABASE_PORT"]),
                       user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
                       db=TENANT, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
cur = conn.cursor()
ids = ",".join(map(str, CHILD_MR_IDS))

cur.execute(f"SELECT * FROM jute_mr WHERE jute_mr_id IN ({ids})"); mr_rows = cur.fetchall()
cur.execute(f"SELECT * FROM jute_mr_li WHERE jute_mr_id IN ({ids})"); li_rows = cur.fetchall()
li_ids = [r["jute_mr_li_id"] for r in li_rows]
cur.execute("SELECT jute_mr_id, status_id, approval_level, bill_pass_no, bill_pass_date, total_amount, claim_amount, "
            "tds_amount, roundoff, net_total, updated_by, updated_date_time FROM jute_mr WHERE jute_mr_id IN (%s)"
            % ",".join(map(str, SRC_RESTORE)))
src_rows = {r["jute_mr_id"]: r for r in cur.fetchall()}

# --- guards -----------------------------------------------------------------
assert len(mr_rows) == 2, mr_rows
for r in mr_rows:
    assert r["branch_id"] == 20 and r["src_com_id"] == 2, r
    assert SRC_RESTORE[r["src_jute_mr_id"]] == r["jute_mr_id"], r
li_list = ",".join(map(str, li_ids)) or "0"
for tbl, col in (("jute_issue", "jute_mr_li_id"), ("jute_moisture_rdg", "jute_mr_li_id"),
                 ("sales_invoice_dtl", "jute_mr_li_id"), ("jute_lot_src", "new_jute_mr_li_id"),
                 ("jute_lot_src", "src_jute_mr_li_id")):
    cur.execute(f"SELECT COUNT(*) AS n FROM {tbl} WHERE {col} IN ({li_list})")
    assert cur.fetchone()["n"] == 0, f"{tbl}.{col} still references the child lines"
for tbl, col in (("jute_prod_selection_trolly_wt", "jute_mr_id"), ("sales_invoice_jute", "mr_id"),
                 ("sales_order_jute", "mr_id"), ("jute_mr", "src_jute_mr_id")):
    cur.execute(f"SELECT COUNT(*) AS n FROM {tbl} WHERE {col} IN ({ids})")
    assert cur.fetchone()["n"] == 0, f"{tbl}.{col} still references the child MRs"

print(f"child MRs   : {[ (r['jute_mr_id'], r['jute_gate_entry_no'], r['branch_mr_no'], r['status_id']) for r in mr_rows ]}")
print(f"child lines : {li_ids}")
print(f"sources     : {[ (k, v['status_id']) for k, v in src_rows.items() ]}")

# --- rollback file -----------------------------------------------------------
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
rb = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"jute_transfer_undo_br20_ge49_50_{stamp}_rollback.sql")
lines = [f"-- rollback for jute_transfer_undo_br20_ge49_50 on {TENANT}, generated {stamp}", "START TRANSACTION;"]
lines += insert_sql("jute_mr", mr_rows) + insert_sql("jute_mr_li", li_rows)
for sid, r in src_rows.items():
    sets = ", ".join(f"{c} = {lit(r[c])}" for c in r if c != "jute_mr_id")
    lines.append(f"UPDATE jute_mr SET {sets} WHERE jute_mr_id = {sid};")
lines.append("COMMIT;")
with open(rb, "w") as f: f.write("\n".join(lines) + "\n")
print("rollback written:", rb)

if not APPLY:
    print("DRY RUN - nothing changed. Re-run with --apply to execute."); sys.exit(0)

# --- apply -------------------------------------------------------------------
try:
    cur.execute(f"DELETE FROM jute_mr_li WHERE jute_mr_id IN ({ids})"); print("jute_mr_li deleted:", cur.rowcount)
    cur.execute(f"DELETE FROM jute_mr WHERE jute_mr_id IN ({ids})"); print("jute_mr deleted:", cur.rowcount)
    cur.execute("UPDATE jute_mr SET status_id = 1, approval_level = NULL, bill_pass_no = NULL, bill_pass_date = NULL, "
                "total_amount = NULL, claim_amount = NULL, tds_amount = NULL, roundoff = NULL, net_total = NULL, "
                "updated_by = %s, updated_date_time = NOW() WHERE jute_mr_id IN (%s) AND status_id <> 1"
                % (UPDATED_BY, ",".join(map(str, SRC_RESTORE))))
    print("sources reopened:", cur.rowcount)
    conn.commit()
    cur.execute("SELECT jute_mr_id, status_id FROM jute_mr WHERE jute_mr_id IN (%s)" % ",".join(map(str, SRC_RESTORE)))
    print("after:", cur.fetchall())
except Exception:
    conn.rollback(); raise
