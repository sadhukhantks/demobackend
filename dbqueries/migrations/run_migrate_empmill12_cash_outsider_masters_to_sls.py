"""
Migrate the legacy EJM Cash/Outsider rate masters from EMPMILL12 into sls.

WHY
---
Legacy `Loan_adv_model.php::updateholidayaData` (holget == 14, "EJM Cash/Outsider
Daily Payment preparation") prices T-numbered outsiders from tables sls never had:

  tbl_occupation_rate           rate per designation (+ contac_id = contractor);
                                prod_calc NULL/0 rows override by designation alone
  tbl_worker_sublocation_table  per worker sublocation + daily_others_pay_duration
                                (only 'T' workers are paid daily)
  tbl_sublocation               sublocation names (company 2 only)
  tbl_contractor_type           contractor type / short name

Structure is copied verbatim via SHOW CREATE TABLE (IF NOT EXISTS).
In sls, tbl_occupation_rate is renamed tbl_outsider_occupation_rate (TARGET_NAME).

DATA / ID MAPPING
-----------------
  tbl_occupation_rate, tbl_sublocation, tbl_contractor_type: copied as-is. Their
    designation / contractor ids are EJM (legacy co 2 -> sls branch 29) ids, which
    the co2 migration preserved.
  tbl_worker_sublocation_table: eb_id translated through
    vowsls_to_sls_transfer._map_pay_eb (the authoritative legacy->sls map); rows
    with no mapping or NULL eb_id are skipped, and one row is kept per sls employee
    (MAX worker_subloca_id — checked before writing: no mapped group disagrees on
    subloca_id / daily_others_pay_duration). Legacy PKs are preserved.

Re-running is a no-op: INSERT IGNORE on the preserved PKs.

ROLLBACK:
  DROP TABLE sls.tbl_outsider_occupation_rate, sls.tbl_worker_sublocation_table,
             sls.tbl_sublocation, sls.tbl_contractor_type;

Usage:
  python run_migrate_empmill12_cash_outsider_masters_to_sls.py            # dry run
  python run_migrate_empmill12_cash_outsider_masters_to_sls.py --commit
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

REPO = Path(__file__).resolve().parents[2]
ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, connect_timeout=30, read_timeout=3600, write_timeout=3600)
cur = conn.cursor()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(msg, flush=True)


def scalar(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


TABLES = ["tbl_occupation_rate", "tbl_worker_sublocation_table",
          "tbl_sublocation", "tbl_contractor_type"]
# EMPMILL12 name -> sls name (only the occupation rate is renamed).
TARGET_NAME = {"tbl_occupation_rate": "tbl_outsider_occupation_rate"}


def tgt(t):
    return TARGET_NAME.get(t, t)

# One row per mapped sls employee — the newest legacy row.
KEEP_WORKER = """
    SELECT m.new_eb_id AS nb, MAX(w.worker_subloca_id) AS keep_id
      FROM EMPMILL12.tbl_worker_sublocation_table w
      JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = w.eb_id
     GROUP BY m.new_eb_id
"""

INSERTS = {
    "tbl_occupation_rate":
        "INSERT IGNORE INTO sls.tbl_outsider_occupation_rate SELECT * FROM EMPMILL12.tbl_occupation_rate",
    "tbl_sublocation":
        "INSERT IGNORE INTO sls.tbl_sublocation SELECT * FROM EMPMILL12.tbl_sublocation",
    "tbl_contractor_type":
        "INSERT IGNORE INTO sls.tbl_contractor_type SELECT * FROM EMPMILL12.tbl_contractor_type",
    "tbl_worker_sublocation_table": f"""
        INSERT IGNORE INTO sls.tbl_worker_sublocation_table
               (worker_subloca_id, eb_id, subloca_id, daily_others_pay_duration)
        SELECT w.worker_subloca_id, k.nb, w.subloca_id, w.daily_others_pay_duration
          FROM ({KEEP_WORKER}) k
          JOIN EMPMILL12.tbl_worker_sublocation_table w ON w.worker_subloca_id = k.keep_id""",
}

stats = {t: {"source_rows": scalar(f"SELECT COUNT(*) FROM EMPMILL12.{t}")} for t in TABLES}
cur.execute("SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'sls' AND table_name IN %s", ([tgt(t) for t in TABLES],))
existing = {r[0] for r in cur.fetchall()}
unmapped = scalar("""
    SELECT COUNT(*) FROM EMPMILL12.tbl_worker_sublocation_table w
      LEFT JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = w.eb_id
     WHERE m.new_eb_id IS NULL""")
conflicts = scalar("""
    SELECT COUNT(*) FROM (
        SELECT m.new_eb_id
          FROM EMPMILL12.tbl_worker_sublocation_table w
          JOIN vowsls_to_sls_transfer._map_pay_eb m ON m.legacy_eb_id = w.eb_id
         GROUP BY m.new_eb_id
        HAVING COUNT(DISTINCT CONCAT(IFNULL(w.subloca_id, ''), '|',
                                     IFNULL(w.daily_others_pay_duration, ''))) > 1) x""")
worker_rows = scalar(f"SELECT COUNT(*) FROM ({KEEP_WORKER}) k")

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}")
for t in TABLES:
    log(f"  {t:32s} source {stats[t]['source_rows']:>6,}   "
        f"{'already in sls' if tgt(t) in existing else 'missing in sls'} as {tgt(t)}")
log(f"  worker rows unmapped / NULL eb     : {unmapped}")
log(f"  worker groups with value conflicts : {conflicts}")
log(f"  worker rows to load (1 per sls eb) : {worker_rows:,}")

if conflicts:
    log("  ABORT: mapped workers disagree on sublocation — re-check before loading.")
    sys.exit(1)

if not COMMIT:
    log("  dry run — nothing written. Re-run with --commit.")
    sys.exit(0)

cur.execute("INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at) "
            "VALUES ('empmill12_cash_outsider_masters', 'running', NOW())")
log_id = cur.lastrowid

# DDL auto-commits in MySQL, so tables are created before the data transaction.
for t in TABLES:
    if tgt(t) not in existing:
        cur.execute(f"SHOW CREATE TABLE EMPMILL12.{t}")
        ddl = cur.fetchone()[1].replace(f"CREATE TABLE `{t}`",
                                        f"CREATE TABLE IF NOT EXISTS sls.`{tgt(t)}`", 1)
        cur.execute(ddl)

for t in TABLES:
    cur.execute(INSERTS[t])
    stats[t]["inserted"] = cur.rowcount
    stats[t]["target_rows"] = scalar(f"SELECT COUNT(*) FROM sls.{tgt(t)}")
    log(f"  {t:32s} inserted {cur.rowcount:>6,}   sls now {stats[t]['target_rows']:,}")

stats["worker_unmapped_skipped"] = unmapped
cur.execute("UPDATE vowsls_to_sls_transfer.migration_log SET status='committed', finished_at=NOW(), "
            "details=%s WHERE id=%s", (json.dumps(stats), log_id))
conn.commit()
log("  committed.")
