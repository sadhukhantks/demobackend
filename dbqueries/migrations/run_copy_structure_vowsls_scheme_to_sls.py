"""
Copy the CURRENT (STATUS=1) pay_employee_structure of a legacy vowsls pay scheme onto its sls mirror scheme,
then verify every component value per employee.

Why: branch-87 mirror schemes on sls (3163, 2015, ...) were created without per-employee structure rows, so the
sls payroll run silently falls back to the scheme constants (C_RATE_, C_TARATE_, C_esi_eligible_ ...) instead of
each employee's own value. The migrated copy of those values exists under the branch-4 scheme (legacy+1000 / 15)
but the mirror runs against the branch-87 eb_ids, which carried no structure.

Mapping: legacy eb -> sls target-branch eb by emp_code (active official-details row, else the latest one),
restricted to employees assigned to the target scheme. Map table: vowsls_to_sls_transfer._map_struct_{L}_to_{S}.

Target PK = legacy ID + 20,000,000 (the 2026-08-15 branch-4 copy used +10,000,000), so the copy is idempotent
(INSERT IGNORE) and reversible:
    DELETE FROM sls.pay_employee_structure WHERE PAYSCHEME_ID = <S> AND ID BETWEEN 20000000 AND 29999999;
    DROP TABLE vowsls_to_sls_transfer._map_struct_<L>_to_<S>;

--update-existing: when mapped employees already hold active rows under the sls scheme (e.g. rows that arrived via
the 2026-08-15 migration + branch-87 re-point), the plain copy aborts to avoid duplicate (employee, component)
pairs. With this flag the script instead (a) UPDATEs the existing active pair to the legacy AMOUNT /
EFFECTIVE_FROM / ENDS_ON where they differ, (b) INSERTs only the pairs the employee is missing, and
(c) leaves sls-only pairs (component not in the legacy structure) untouched but reports them. Overwritten rows are
backed up first, so the update side is reversible too:
    UPDATE sls.pay_employee_structure s JOIN vowsls_to_sls_transfer._bak_struct_<L>_to_<S> b ON b.ID = s.ID
       SET s.AMOUNT = b.AMOUNT, s.EFFECTIVE_FROM = b.EFFECTIVE_FROM, s.ENDS_ON = b.ENDS_ON,
           s.updated_by = b.updated_by, s.updated_date_time = b.updated_date_time;
    DROP TABLE vowsls_to_sls_transfer._bak_struct_<L>_to_<S>;

Usage:
    python run_copy_structure_vowsls_scheme_to_sls.py --legacy 164 --sls 2015 --branch 87            # dry run
    python run_copy_structure_vowsls_scheme_to_sls.py --legacy 164 --sls 2015 --branch 87 --commit   # copy + verify + log
    python run_copy_structure_vowsls_scheme_to_sls.py --legacy 167 --sls 3167 --update-existing --commit
    python run_copy_structure_vowsls_scheme_to_sls.py --legacy 96 --sls 3096 --update-existing --legacy-branch 4 --commit
Done so far: 163 -> 3163, 164 -> 2015, 169 -> 2017 (plain copies); 167 -> 3167 (njm_retired_payroll, update-existing);
96 -> 3096 (NJM Blue Force No ESI, legacy-branch 4: B008/D001/D002/270270 also exist as co-106 clones).
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import pymysql

ap = argparse.ArgumentParser()
ap.add_argument("--legacy", type=int, required=True, help="vowsls PAYSCHEME_ID")
ap.add_argument("--sls", type=int, required=True, help="sls payscheme_id (mirror)")
ap.add_argument("--branch", type=int, default=87, help="sls branch the target employees must sit on")
ap.add_argument("--legacy-branch", type=int,
                help="keep only legacy employees whose official row is on this vowsls branch - for people cloned under "
                     "co 1 (branch 4) and co 106 (branch 87) on one legacy scheme; the branch of the scheme's "
                     "vowsls.tbl_pay_period rows is the one legacy actually pays")
ap.add_argument("--update-existing", action="store_true",
                help="update active sls pairs that differ from legacy and insert only the missing pairs")
ap.add_argument("--commit", action="store_true")
A = ap.parse_args()
COMMIT, UPDATE = A.commit, A.update_existing
L_SCHEME, S_SCHEME, S_BRANCH = A.legacy, A.sls, A.branch
OFFSET = 20_000_000
MAP = f"vowsls_to_sls_transfer._map_struct_{L_SCHEME}_to_{S_SCHEME}"
BAK = f"vowsls_to_sls_transfer._bak_struct_{L_SCHEME}_to_{S_SCHEME}"
STEP = f"pay_structure_{L_SCHEME}_to_{S_SCHEME}"

REPO = Path(__file__).resolve().parents[2]
ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"], password=ENV["DATABASE_PASSWORD"],
                       port=int(ENV["DATABASE_PORT"]), autocommit=False, connect_timeout=30, read_timeout=1800)
cur = conn.cursor(pymysql.cursors.DictCursor)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
STATS = {"legacy_scheme": L_SCHEME, "sls_scheme": S_SCHEME, "branch": S_BRANCH, "update_existing": UPDATE,
         "legacy_branch": A.legacy_branch}


def log(msg):
    print(msg, flush=True)


def q(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def one(sql, args=None):
    return q(sql, args)[0]


# -- 1. employee map (client-side, same rule as the comparison workbook) -----------
def pick(rows, active_key):
    by = defaultdict(list)
    for r in rows:
        by[r["eb"]].append(r)
    return {eb: max([r for r in rs if r[active_key] == 1] or rs, key=lambda r: r["oid"] or 0) for eb, rs in by.items()}


legacy = pick(q(f"""select e.EMPLOYEEID eb, o.emp_code, o.is_active act, o.tbl_hrms_ed_official_detail_id oid
                   from vowsls.tbl_pay_employee_payscheme e
                   left join vowsls.tbl_hrms_ed_official_details o on o.eb_id = e.EMPLOYEEID
                   where e.PAY_SCHEME_ID = {L_SCHEME} and e.STATUS = 1
                   {f"and o.branch_id = {A.legacy_branch}" if A.legacy_branch else ""}"""), "act")
target = pick(q(f"""select e.EMPLOYEEID eb, o.emp_code, o.active act, o.branch_id, o.tbl_hrms_ed_official_detail_id oid
                   from sls.pay_employee_payscheme e
                   left join sls.hrms_ed_official_details o on o.eb_id = e.EMPLOYEEID
                   where e.PAY_SCHEME_ID = {S_SCHEME} and e.STATUS = 1"""), "act")
code = lambda r: (r["emp_code"] or "").strip()
l_by_code = {code(r): r for r in legacy.values()}
s_by_code = {code(r): r for r in target.values()}
assert len(l_by_code) == len(legacy) and len(s_by_code) == len(target), "emp_code is not unique on one side"
common = sorted(set(l_by_code) & set(s_by_code))
STATS["legacy_employees"] = len(legacy)
STATS["target_employees"] = len(target)
STATS["mapped"] = len(common)
STATS["legacy_unmapped"] = sorted(set(l_by_code) - set(s_by_code))
STATS["target_without_legacy"] = sorted(set(s_by_code) - set(l_by_code))
log(f"employees: legacy {len(legacy)}, target {len(target)}, mapped by emp_code {len(common)}, "
    f"legacy unmapped {len(STATS['legacy_unmapped'])} {STATS['legacy_unmapped']}, target-only {STATS['target_without_legacy']}")
wrong_branch = [c for c in common if s_by_code[c]["branch_id"] != S_BRANCH]
assert not wrong_branch, f"target employees not on branch {S_BRANCH}: {wrong_branch}"

cur.execute(f"""create table if not exists {MAP} (
    legacy_eb_id int not null primary key, new_eb_id int not null unique, emp_code varchar(50)) engine=InnoDB""")
cur.executemany(f"insert ignore into {MAP} (legacy_eb_id, new_eb_id, emp_code) values (%s, %s, %s)",
                [(l_by_code[c]["eb"], s_by_code[c]["eb"], c) for c in common])
conn.commit()
STATS["map_rows"] = one(f"select count(*) n from {MAP}")["n"]
log(f"map table {MAP}: {STATS['map_rows']} rows")

# -- 2. pre-flight ------------------------------------------------------------------
# legacy double-saves: identical active rows for one (employee, component) - legacy 1 had U0249 saved twice. They fan
# out PAIR (negative sls-only, per-employee check fails). Keep the lowest ID; copies that disagree would change pay: stop.
legacy_by_pair = defaultdict(list)
for r in q(f"""select l.ID, l.EMPLOYEEID, l.COMPONENT_ID, l.AMOUNT, l.EFFECTIVE_FROM, l.ENDS_ON
               from vowsls.tbl_pay_employee_structure l join {MAP} m on m.legacy_eb_id = l.EMPLOYEEID
               where l.PAYSCHEME_ID = {L_SCHEME} and l.STATUS = 1"""):
    legacy_by_pair[(r["EMPLOYEEID"], r["COMPONENT_ID"])].append(r)
legacy_dups = [sorted(rs, key=lambda r: r["ID"]) for rs in legacy_by_pair.values() if len(rs) > 1]
conflicts = [rs for rs in legacy_dups if len({(r["AMOUNT"], r["EFFECTIVE_FROM"], r["ENDS_ON"]) for r in rs}) > 1]
assert not conflicts, f"legacy holds differing active rows for one (employee, component): {conflicts[:5]}"
LEGACY_DUP_IDS = [r["ID"] for rs in legacy_dups for r in rs[1:]]
NOT_DUP = f"and l.ID not in ({','.join(map(str, LEGACY_DUP_IDS))})" if LEGACY_DUP_IDS else ""
STATS["legacy_duplicate_rows_skipped"] = len(LEGACY_DUP_IDS)
log(f"legacy duplicate active rows skipped (identical copies): {len(LEGACY_DUP_IDS)}")

# legacy active row  <->  sls active row for the same (mapped employee, component); one active row per pair on each side
PAIR = f"""from vowsls.tbl_pay_employee_structure l join {MAP} m on m.legacy_eb_id = l.EMPLOYEEID
           left join sls.pay_employee_structure s on s.EMPLOYEEID = m.new_eb_id and s.PAYSCHEME_ID = {S_SCHEME}
                and s.STATUS = 1 and s.COMPONENT_ID = l.COMPONENT_ID
           where l.PAYSCHEME_ID = {L_SCHEME} and l.STATUS = 1 {NOT_DUP}"""
# value fields only - REMARKS differs on nearly every migrated row (NULL vs '') and the engine never reads it
DIFF = "(s.AMOUNT <> l.AMOUNT or not (s.EFFECTIVE_FROM <=> l.EFFECTIVE_FROM) or not (s.ENDS_ON <=> l.ENDS_ON))"
eligible = one(f"""select count(*) n, count(distinct l.EMPLOYEEID) e, min(l.ID) mn, max(l.ID) mx,
                          sum(s.ID is not null) existing_pairs, sum(s.ID is null) missing_pairs,
                          sum(s.ID is not null and {DIFF}) differing_pairs {PAIR}""")
# only STATUS=1 rows count: the engine (query.py get_employee_pay_structure) reads nothing else
existing = one(f"""select count(*) n, count(distinct s.EMPLOYEEID) e from sls.pay_employee_structure s
                   join {MAP} m on m.new_eb_id = s.EMPLOYEEID where s.PAYSCHEME_ID = {S_SCHEME} and s.STATUS = 1""")
already = one(f"""select count(*) n from sls.pay_employee_structure
                  where PAYSCHEME_ID = {S_SCHEME} and ID between {OFFSET} and {OFFSET + 9_999_999}""")["n"]
# only rows that will be INSERTed: existing pairs are skipped, and AUTO_INCREMENT already sits inside the +20M range,
# so UI saves of other schemes own some offset IDs of legacy rows that never need copying (96 -> 3096: 15 such rows)
pk_taken = one(f"""select count(*) n {PAIR} and s.ID is null and exists (select 1 from sls.pay_employee_structure t
                   where t.ID = l.ID + {OFFSET} and t.PAYSCHEME_ID <> {S_SCHEME})""")["n"]
sls_only = existing["n"] - int(eligible["existing_pairs"] or 0)
STATS.update(eligible_rows=eligible["n"], eligible_employees=eligible["e"], legacy_id_range=[eligible["mn"], eligible["mx"]],
             existing_pairs=int(eligible["existing_pairs"] or 0), missing_pairs=int(eligible["missing_pairs"] or 0),
             differing_pairs=int(eligible["differing_pairs"] or 0), sls_only_active_rows=sls_only,
             existing_active_rows_for_mapped=existing["n"], existing_active_employees_for_mapped=existing["e"],
             rows_already_in_offset_range=already, pk_taken_by_other_scheme=pk_taken)
log(f"eligible legacy rows: {eligible['n']} for {eligible['e']} employees (ID {eligible['mn']}..{eligible['mx']} -> +{OFFSET})")
log(f"active rows already under sls {S_SCHEME} for the mapped employees: {existing['n']} rows / {existing['e']} employees; "
    f"rows already in the +{OFFSET} range: {already}; target PKs taken by another scheme: {pk_taken}")
log(f"pairs: existing {STATS['existing_pairs']} (differing {STATS['differing_pairs']}), missing {STATS['missing_pairs']}, "
    f"sls-only (left alone) {sls_only}")
assert pk_taken == 0, "legacy ID + OFFSET collides with rows of another scheme - pick a different OFFSET"
if existing["n"] and not already and not UPDATE:
    log(f"WARNING: mapped employees already have active {S_SCHEME} structure rows that did not come from this script - "
        "the copy would create duplicates per (employee, component). Aborting (re-run with --update-existing).")
    sys.exit(1)
if UPDATE and STATS["differing_pairs"]:
    log("rows that would be updated:")
    for r in q(f"""select m.emp_code, s.ID sls_id, s.COMPONENT_ID, s.AMOUNT sls_amount, l.AMOUNT legacy_amount,
                          s.EFFECTIVE_FROM s_eff, l.EFFECTIVE_FROM l_eff, s.ENDS_ON s_end, l.ENDS_ON l_end
                   {PAIR} and s.ID is not null and {DIFF} order by m.emp_code, s.COMPONENT_ID limit 50"""):
        log(f"   {r}")

if not COMMIT:
    log("\nDry run complete - nothing written except the map table. Re-run with --commit to copy.")
    conn.close()
    sys.exit(0)

# -- 3. copy (server-side, same MySQL server) ----------------------------------------
# Both structure tables carry only a PRIMARY KEY. Pair matching therefore happens in plain SELECTs (non-locking,
# hash-joinable) and every write on sls is addressed by ID. A join-UPDATE against sls.pay_employee_structure
# scanned and row-locked ~5M rows of the live table (2026-09-10) and had to be killed - never write through PAIR.
STATS["updated"] = STATS["backed_up"] = 0
if UPDATE and STATS["differing_pairs"]:
    diff_rows = q(f"""select s.ID sls_id, l.AMOUNT, l.EFFECTIVE_FROM, l.ENDS_ON, coalesce(l.MODIFIED_BY, l.CREATED_BY, 0) upd_by
                      {PAIR} and s.ID is not null and {DIFF}""")
    ids = [r["sls_id"] for r in diff_rows]
    cur.execute(f"create table if not exists {BAK} like sls.pay_employee_structure")  # implicit commit; nothing pending yet
    cur.execute(f"insert ignore into {BAK} select * from sls.pay_employee_structure where ID in ({','.join(['%s'] * len(ids))})", ids)
    STATS["backed_up"] = cur.rowcount
    STATS["updated"] = cur.executemany("""update sls.pay_employee_structure set AMOUNT = %s, EFFECTIVE_FROM = %s, ENDS_ON = %s,
                                          updated_by = %s, updated_date_time = now() where ID = %s""",
                                       [(r["AMOUNT"], r["EFFECTIVE_FROM"], r["ENDS_ON"], r["upd_by"], r["sls_id"]) for r in diff_rows])
    log(f"backed up {STATS['backed_up']} rows to {BAK}, updated {STATS['updated']} rows")

# legacy rows whose pair already exists on sls are skipped by legacy ID, so the INSERT reads vowsls + the map only
SKIP = "vowsls_to_sls_transfer._tmp_struct_skip"
cur.execute(f"create temporary table {SKIP} (legacy_id int not null primary key)")
skip_ids = [r["legacy_id"] for r in q(f"select l.ID legacy_id {PAIR} and s.ID is not null")]
cur.executemany(f"insert into {SKIP} (legacy_id) values (%s)", skip_ids)
cur.execute(f"""insert ignore into sls.pay_employee_structure
    (ID, EMPLOYEEID, PAYSCHEME_ID, COMPONENT_ID, AMOUNT, STATUS, EFFECTIVE_FROM, ENDS_ON, REMARKS, updated_by, updated_date_time)
    select l.ID + {OFFSET}, m.new_eb_id, {S_SCHEME}, l.COMPONENT_ID, l.AMOUNT, l.STATUS, l.EFFECTIVE_FROM, l.ENDS_ON, l.REMARKS,
           coalesce(l.MODIFIED_BY, l.CREATED_BY, 0), l.LUPDATE
    from vowsls.tbl_pay_employee_structure l join {MAP} m on m.legacy_eb_id = l.EMPLOYEEID
    left join {SKIP} k on k.legacy_id = l.ID
    where l.PAYSCHEME_ID = {L_SCHEME} and l.STATUS = 1 and k.legacy_id is null {NOT_DUP}""")
STATS["inserted"] = cur.rowcount
log(f"inserted {cur.rowcount} rows (skipped {len(skip_ids)} existing pairs)")

# -- 4. verify: every (employee, component) value equal, no duplicates ---------------
v = one(f"""select count(*) n, sum(s.ID is null) missing, sum(s.AMOUNT <> l.AMOUNT) amount_diff,
                   sum(coalesce(s.STATUS,-1) <> l.STATUS) status_diff,
                   sum(not (s.EFFECTIVE_FROM <=> l.EFFECTIVE_FROM)) eff_diff, sum(not (s.ENDS_ON <=> l.ENDS_ON)) ends_diff
            {PAIR}""")
dup = one(f"""select count(*) n from (select EMPLOYEEID, COMPONENT_ID from sls.pay_employee_structure
              where PAYSCHEME_ID = {S_SCHEME} and STATUS = 1 group by 1, 2 having count(*) > 1) d""")["n"]
# two scans instead of three correlated full scans per employee (no secondary index on either table)
per_emp = q(f"""select m.emp_code, m.legacy_eb_id, m.new_eb_id, count(*) legacy_rows,
                       sum(s.ID is not null and s.AMOUNT = l.AMOUNT) equal_values
                {PAIR} group by m.emp_code, m.legacy_eb_id, m.new_eb_id""")
sls_rows = {r["EMPLOYEEID"]: r["n"] for r in q(f"""select EMPLOYEEID, count(*) n from sls.pay_employee_structure
                                                  where PAYSCHEME_ID = {S_SCHEME} and STATUS = 1 group by 1""")}
for r in per_emp:
    r["sls_rows"] = sls_rows.get(r["new_eb_id"], 0)
bad = [r for r in per_emp if not (r["legacy_rows"] == r["sls_rows"] == r["equal_values"])]
STATS.update(verify=v, duplicate_emp_component_pairs=dup, employees_checked=len(per_emp), employees_with_mismatch=len(bad))
log(f"verify: {v}  duplicate (employee, component) pairs under {S_SCHEME}: {dup}")
log(f"per-employee check: {len(per_emp)} employees, {len(bad)} with a row-count or value mismatch")
for r in bad[:20]:
    log(f"   MISMATCH {r}")

ok = (v["missing"] == 0 and v["amount_diff"] == 0 and v["status_diff"] == 0 and v["eff_diff"] == 0 and v["ends_diff"] == 0
      and dup == 0 and not bad and STATS["inserted"] == STATS["missing_pairs"] and STATS["updated"] == STATS["differing_pairs"])
if not ok:
    conn.rollback()
    log("\nVERIFICATION FAILED - rolled back, nothing kept (map table remains).")
    sys.exit(1)

cur.execute("""insert into vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
               values (%s, 'committed', now(), now(), %s)""", (STEP, json.dumps(STATS, default=str)))
conn.commit()
log(f"\nCOMMITTED and logged to vowsls_to_sls_transfer.migration_log step '{STEP}'")
conn.close()
