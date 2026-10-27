"""
Give every employee on an sls pay scheme a current pay_employee_structure row for EVERY active component of the
scheme (pay_scheme_details STATUS=1), inserting only the (employee, component) pairs that are missing.

Why: the sls "save salary structure" UI writes only the input (TYPE 0) components, and a re-save deactivates the
employee's previous rows, so employees end up with 16 of the scheme's 39 components while the migrated legacy
employees carry all 39 (derived components stored as 0). This makes the table uniform per scheme.

Amount for an inserted row:
  * TYPE 0 (input)  : the scheme detail's formula constant when numeric, else the component DEFAULT_VALUE, else 0
  * TYPE 1/2/3      : 0  (calculated by the formula at processing time; the engine only reads structure for TYPE 0)
EFFECTIVE_FROM = the employee's latest EFFECTIVE_FROM among their current rows on the scheme (NULL if none),
STATUS 1, REMARKS tags the batch so it can be reversed:
    DELETE FROM sls.pay_employee_structure WHERE PAYSCHEME_ID = <scheme> AND REMARKS = 'gap-fill <date> from pay_scheme_details';

Usage:
    python run_fill_structure_gaps.py --scheme 3163            # dry run
    python run_fill_structure_gaps.py --scheme 3163 --commit
"""
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
SCHEME = int(sys.argv[sys.argv.index("--scheme") + 1])
TAG = f"gap-fill {date.today().isoformat()} from pay_scheme_details"

REPO = Path(__file__).resolve().parents[2]
ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"], password=ENV["DATABASE_PASSWORD"],
                       port=int(ENV["DATABASE_PORT"]), autocommit=False, connect_timeout=30, read_timeout=600)
cur = conn.cursor(pymysql.cursors.DictCursor)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(m):
    print(m, flush=True)


def q(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


# scheme components (active) with the amount a fresh row should carry
comps = {}
for r in q("""select d.COMPONENT_ID cid, c.CODE code, c.TYPE typ, d.FORMULA formula, c.DEFAULT_VALUE dv
              from sls.pay_scheme_details d join sls.pay_components c on c.ID = d.COMPONENT_ID
              where d.PAY_SCHEME_ID = %s and d.STATUS = 1 order by d.ID""", (SCHEME,)):
    amount = 0.0
    if r["typ"] == 0:
        try:
            amount = float(r["formula"])
        except (TypeError, ValueError):
            amount = float(r["dv"] or 0)
    comps[r["cid"]] = (r["code"], r["typ"], amount)
assert comps, f"scheme {SCHEME} has no active pay_scheme_details"
log(f"scheme {SCHEME}: {len(comps)} active components")

# employees: assigned to the scheme (distinct) plus anyone already holding current structure rows on it
emps = sorted({r["eb"] for r in q("""select EMPLOYEEID eb from sls.pay_employee_payscheme where PAY_SCHEME_ID = %s
                                     union select EMPLOYEEID from sls.pay_employee_structure where PAYSCHEME_ID = %s and STATUS = 1""",
                                  (SCHEME, SCHEME))})
dup_assign = q("select EMPLOYEEID eb, count(*) n from sls.pay_employee_payscheme where PAY_SCHEME_ID = %s group by 1 having count(*) > 1", (SCHEME,))
have, eff = defaultdict(set), {}
for r in q("select EMPLOYEEID eb, COMPONENT_ID cid, EFFECTIVE_FROM ef from sls.pay_employee_structure where PAYSCHEME_ID = %s and STATUS = 1", (SCHEME,)):
    have[r["eb"]].add(r["cid"])
    if r["ef"] and (eff.get(r["eb"]) is None or r["ef"] > eff[r["eb"]]):
        eff[r["eb"]] = r["ef"]

rows = []
for eb in emps:
    missing = [cid for cid in comps if cid not in have[eb]]
    if missing:
        log(f"  employee {eb}: has {len(have[eb])}, missing {len(missing)}: {[comps[c][0] for c in missing]}")
        for cid in missing:
            rows.append((eb, SCHEME, cid, comps[cid][2], 1, eff.get(eb), TAG, 0))
log(f"\n{len(emps)} employees on scheme {SCHEME}; {sum(1 for eb in emps if len(have[eb]) < len(comps))} with gaps; {len(rows)} row(s) to insert")
if dup_assign:
    log(f"NOTE: duplicate pay_employee_payscheme rows on this scheme (not touched): {dup_assign}")

if not COMMIT:
    log("\nDry run - nothing written. Re-run with --commit to insert.")
    conn.close()
    sys.exit(0)
if not rows:
    log("nothing to insert")
    conn.close()
    sys.exit(0)

cur.executemany("""insert into sls.pay_employee_structure
    (EMPLOYEEID, PAYSCHEME_ID, COMPONENT_ID, AMOUNT, STATUS, EFFECTIVE_FROM, REMARKS, updated_by, updated_date_time)
    values (%s, %s, %s, %s, %s, %s, %s, %s, now())""", rows)
inserted = cur.rowcount
# verify: every employee now has every active component exactly once (current rows)
bad = q("""select x.eb, x.n from (
             select e.EMPLOYEEID eb, count(distinct s.COMPONENT_ID) n
             from (select distinct EMPLOYEEID from sls.pay_employee_payscheme where PAY_SCHEME_ID = %s) e
             left join sls.pay_employee_structure s on s.EMPLOYEEID = e.EMPLOYEEID and s.PAYSCHEME_ID = %s and s.STATUS = 1
             group by 1) x where x.n <> %s""", (SCHEME, SCHEME, len(comps)))
dups = q("""select EMPLOYEEID, COMPONENT_ID, count(*) n from sls.pay_employee_structure
            where PAYSCHEME_ID = %s and STATUS = 1 group by 1, 2 having count(*) > 1""", (SCHEME,))
log(f"inserted {inserted}; employees not at {len(comps)} components: {len(bad)}; duplicate current (employee, component): {len(dups)}")
if inserted != len(rows) or bad or dups:
    conn.rollback()
    log("VERIFICATION FAILED - rolled back")
    for b in bad[:10]: log(f"   {b}")
    for d in dups[:10]: log(f"   {d}")
    sys.exit(1)
cur.execute("""insert into vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
               values (%s, 'committed', now(), now(), %s)""",
            (f"pay_structure_gap_fill_{SCHEME}", json.dumps({"scheme": SCHEME, "inserted": inserted, "tag": TAG,
                                                             "employees": sorted({r[0] for r in rows}), "components": len(comps)}, default=str)))
conn.commit()
log(f"COMMITTED; rows tagged REMARKS='{TAG}'; migration_log step 'pay_structure_gap_fill_{SCHEME}'")
conn.close()
