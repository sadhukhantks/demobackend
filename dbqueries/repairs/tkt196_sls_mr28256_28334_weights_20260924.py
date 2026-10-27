"""
TKT-000196 — restore the line weights of sls MR 28256 (EJM, branch 29 FACTORY,
gate entry 46 of 2026-09-20, lorry WB-25J-8701) and of its branch-20 transfer
copy MR 28334 (Jagrati, gate entry 45) to the header's actual weight of 9,040 kg.

Owner ruling 2026-09-24: actual weight is 9,040 kg. On 2026-09-24 16:23 the two
lines of MR 28256 were re-saved at 1,121 / 1,693 kg (2,814 kg) while the header
kept 9,040 kg, and MR 28334 was created from that state at 17:14 with the same
wrong lines. The nine draft issues drawn from MR 28256 on 2026-09-21 already
total 3,600 kg (D TD-5) and 5,440 kg (D TD-6) — the gate-out split of 9,040 kg
over 90 : 136 units — so they are not touched.

Per line:  actual_weight = accepted_weight = target kg, shortage_kgs = 0,
           total_price = target / 100 * rate   (rate is per quintal)
Per header: mr_weight = 9040, total_amount = net_total = sum(total_price),
           claim_amount = tds_amount = roundoff = 0 (as on sibling MR 28257).

    28256 / 45727  D TD-5  (item 56)   90 u  1,121 -> 3,600 kg  @12,500 = 450,000.00
    28256 / 45728  D TD-6  (item 57)  136 u  1,693 -> 5,440 kg  @12,300 = 669,120.00
    28334 / 45789  D TD-5  (item 610)  90 u  1,121 -> 3,600 kg  @12,500 = 450,000.00
    28334 / 45790  D TD-6  (item 612) 136 u  1,693 -> 5,440 kg  @12,300 = 669,120.00
    headers 28256, 28334: 348,364.00 -> 1,119,120.00

Usage (cwd = vowerp3be, env/database.env exported):
    uv run python dbqueries/repairs/tkt196_sls_mr28256_28334_weights_20260924.py           # dry run
    uv run python dbqueries/repairs/tkt196_sls_mr28256_28334_weights_20260924.py --apply   # execute
A rollback SQL file (full row backups as UPDATEs) is written next to this script
before anything is changed.
"""
import os, sys, datetime, pymysql

TENANT = "sls"
UPDATED_BY = 24            # owner's sls login, who gave the ruling
APPLY = "--apply" in sys.argv

# jute_mr_li_id -> (jute_mr_id, expected item, expected qty, expected current kg, target kg)
LINES = {
    45727: (28256, 56, 90, 1121, 3600),
    45728: (28256, 57, 136, 1693, 5440),
    45789: (28334, 610, 90, 1121, 3600),
    45790: (28334, 612, 136, 1693, 5440),
}
HEADERS = {28256: (29, 9040.0, 2814.0), 28334: (20, 9040.0, 2814.0)}   # mr_id -> (branch, actual_weight, current mr_weight)
ISSUE_EXPECT = {45727: 3600, 45728: 5440}                                # live issues must already match the targets


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds()); return "'%02d:%02d:%02d'" % (s // 3600, s % 3600 // 60, s % 60)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


conn = pymysql.connect(host=os.environ["DATABASE_HOST"], port=int(os.environ["DATABASE_PORT"]),
                       user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
                       db=TENANT, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
cur = conn.cursor()

li_ids = ",".join(map(str, LINES))
mr_ids = ",".join(map(str, HEADERS))
cur.execute(f"SELECT * FROM jute_mr_li WHERE jute_mr_li_id IN ({li_ids})"); li_rows = {r["jute_mr_li_id"]: r for r in cur.fetchall()}
cur.execute(f"SELECT * FROM jute_mr WHERE jute_mr_id IN ({mr_ids})"); mr_rows = {r["jute_mr_id"]: r for r in cur.fetchall()}

# --- guards: the rows must look exactly as they did when the ruling was given ----
assert set(li_rows) == set(LINES), sorted(li_rows)
assert set(mr_rows) == set(HEADERS), sorted(mr_rows)
for li_id, (mr_id, item, qty, cur_kg, _) in LINES.items():
    r = li_rows[li_id]
    assert (r["jute_mr_id"], r["actual_item_id"], r["actual_qty"], r["actual_weight"]) == (mr_id, item, qty, cur_kg), r
    assert r["rate"] in (12500.0, 12300.0), r
for mr_id, (branch, actual, mr_weight) in HEADERS.items():
    r = mr_rows[mr_id]
    assert (r["branch_id"], r["actual_weight"], r["mr_weight"]) == (branch, actual, mr_weight), r
assert mr_rows[28334]["src_jute_mr_id"] == 28256
for li_id, kg in ISSUE_EXPECT.items():
    cur.execute("SELECT COALESCE(SUM(weight),0) AS w, COUNT(*) AS n FROM jute_issue WHERE jute_mr_li_id=%s AND status_id<>4", (li_id,))
    r = cur.fetchone(); assert float(r["w"]) == kg, (li_id, r)
cur.execute("SELECT COUNT(*) AS n FROM jute_issue WHERE jute_mr_li_id IN (45789, 45790)")
assert cur.fetchone()["n"] == 0, "branch-20 lines have issues; re-check"

# --- plan --------------------------------------------------------------------------
plan_li, totals = [], {}
for li_id, (mr_id, item, qty, cur_kg, target) in LINES.items():
    rate = li_rows[li_id]["rate"]
    price = round(target / 100 * rate, 2)
    totals[mr_id] = round(totals.get(mr_id, 0) + price, 2)
    plan_li.append((li_id, mr_id, item, cur_kg, target, rate, price))
    print(f"line {li_id} (MR {mr_id}, item {item}): {cur_kg:>6.0f} -> {target:>5.0f} kg  @ {rate:.0f}  total_price {li_rows[li_id]['total_price']} -> {price:.2f}")
for mr_id, total in totals.items():
    print(f"MR {mr_id}: mr_weight {mr_rows[mr_id]['mr_weight']} -> 9040, total_amount/net_total {mr_rows[mr_id]['total_amount']} -> {total:.2f}")

# --- rollback file ------------------------------------------------------------------
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
rb = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"tkt196_sls_mr28256_28334_weights_{stamp}_rollback.sql")
lines = [f"-- rollback for tkt196_sls_mr28256_28334_weights on {TENANT}, generated {stamp}", "START TRANSACTION;"]
for li_id, r in li_rows.items():
    sets = ", ".join(f"`{c}` = {lit(r[c])}" for c in r if c != "jute_mr_li_id")
    lines.append(f"UPDATE jute_mr_li SET {sets} WHERE jute_mr_li_id = {li_id};")
for mr_id, r in mr_rows.items():
    sets = ", ".join(f"`{c}` = {lit(r[c])}" for c in r if c != "jute_mr_id")
    lines.append(f"UPDATE jute_mr SET {sets} WHERE jute_mr_id = {mr_id};")
lines.append("COMMIT;")
with open(rb, "w") as f:
    f.write("\n".join(lines) + "\n")
print("rollback written:", rb)

if not APPLY:
    print("DRY RUN - nothing changed. Re-run with --apply to execute."); sys.exit(0)

# --- apply --------------------------------------------------------------------------
try:
    n = 0
    for li_id, mr_id, item, cur_kg, target, rate, price in plan_li:
        cur.execute("UPDATE jute_mr_li SET actual_weight=%s, accepted_weight=%s, shortage_kgs=0, total_price=%s, "
                    "updated_date_time=NOW() WHERE jute_mr_li_id=%s AND jute_mr_id=%s AND actual_weight=%s",
                    (target, target, price, li_id, mr_id, cur_kg))
        n += cur.rowcount
    assert n == 4, f"expected 4 line rows, updated {n}"
    m = 0
    for mr_id, total in totals.items():
        cur.execute("UPDATE jute_mr SET mr_weight=9040, total_amount=%s, claim_amount=0, tds_amount=0, roundoff=0, net_total=%s, "
                    "updated_by=%s, updated_date_time=NOW() WHERE jute_mr_id=%s AND mr_weight=2814",
                    (total, total, UPDATED_BY, mr_id))
        m += cur.rowcount
    assert m == 2, f"expected 2 header rows, updated {m}"
    conn.commit()
    print(f"applied: {n} line rows, {m} header rows")
    cur.execute(f"SELECT jute_mr_li_id, jute_mr_id, actual_weight, accepted_weight, total_price FROM jute_mr_li WHERE jute_mr_li_id IN ({li_ids})")
    for r in cur.fetchall(): print(" ", r)
    cur.execute(f"SELECT jute_mr_id, mr_weight, total_amount, net_total FROM jute_mr WHERE jute_mr_id IN ({mr_ids})")
    for r in cur.fetchall(): print(" ", r)
    cur.execute("SELECT jute_mr_li_id, bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id IN (45727, 45728)")
    print("  outstanding view (expect bal 0 or no rows):", cur.fetchall())
except Exception:
    conn.rollback(); raise
