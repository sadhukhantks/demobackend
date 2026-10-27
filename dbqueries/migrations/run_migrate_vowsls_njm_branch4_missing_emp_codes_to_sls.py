"""
Migrate the 9 NJM (co 1) branch-4 employees whose emp_code exists in vowsls but nowhere in sls.

Found by report_missing_emp_code_vowsls_vs_sls.py: emp_codes
  115157, 117165, 127102, 127127, 665088, C910272, U0268, U0269, U0270
(vowsls eb_id 54519-54527, all created in vowsls 2026-07-29 with DOJ Jun-Aug 2026, i.e. after
the vowsls->sls HRMS cutover). They are whole employees missing from sls, not just codes.

Conventions (per VOWSLS_TO_SLS_MIGRATION_GUIDE.md and the vowsls->sls HRMS series):
  - branch_id 4 passthrough (sls.branch_mst 4 = co 1 FACTORY, the live NJM branch).
  - eb_id NOT preserved: 54519-54527 are already taken in sls by unrelated employees
    (co-1/branch-87 batch). Fresh ids are allocated from MAX(eb_id)+1; map persisted in
    vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026.
  - sub_dept_id / designation_id: identity — every referenced id exists in sls under branch 4
    with matching description (verified 2026-08-15). The branch-87 map does NOT apply here.
  - catagory_id: identity for 20/22/48 (all branch 4). vowsls 83 'NEW BUDLI RTD' exists in sls
    only under branch 87 -> find-or-create the branch-4 twin.
  - vowsls official_details holds several rows per employee; the is_active=1 row (latest id on a
    tie) wins, matching the "latest official row" grain of the comparison report.
  - Audit created_* dropped; updated_by falls back to created_by then 0 (guide rule).
  - reporting_eb_id is remapped through the new eb map, else NULL (no cross-batch lookup).

Tables: hrms_ed_personal_details, hrms_ed_official_details, hrms_ed_address_details,
        hrms_ed_bank_details, hrms_ed_contact_details.
No pf/esi/resign/experience/attendance/leave rows exist for these 9 in vowsls.

ROLLBACK (run only if a committed run must be reversed):
  DELETE x FROM sls.hrms_ed_official_details x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_address_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_bank_details     x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_contact_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026 m ON m.new_eb_id=x.eb_id;
  DELETE p FROM sls.hrms_ed_personal_details p JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026 m ON m.new_eb_id=p.eb_id;
  -- plus the category row printed by the run, if it was created:
  -- DELETE FROM sls.category_mst WHERE cata_id = <printed> ;
  DROP TABLE vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026;

Usage:
  python run_migrate_vowsls_njm_branch4_missing_emp_codes_to_sls.py            # dry run (rolls back)
  python run_migrate_vowsls_njm_branch4_missing_emp_codes_to_sls.py --commit   # apply
"""
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

ENV = {}
for line in (Path(__file__).resolve().parents[2] / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

CODES = ("115157", "117165", "127102", "127127", "665088", "C910272", "U0268", "U0269", "U0270")
BRANCH = 4
MAP = "vowsls_to_sls_transfer._map_hrms_eb_njm_br4_2026"

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()


def one(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- pre-flight
cur.execute("SHOW TABLES FROM vowsls_to_sls_transfer LIKE '_map_hrms_eb_njm_br4_2026'")
if cur.fetchone():
    n = one(f"SELECT COUNT(*) c FROM {MAP}")["c"]
    if n:
        sys.exit(f"ABORT: {MAP} already has {n} rows — migration appears to have run. "
                 "Use the rollback block in the header first if you need to re-run.")

clash = one("""SELECT COUNT(*) c FROM sls.hrms_ed_official_details
                WHERE UPPER(TRIM(emp_code)) IN %s""", (CODES,))["c"]
if clash:
    sys.exit(f"ABORT: {clash} of the target emp_codes already exist in sls — re-run the "
             "comparison report; the gap may already have been closed.")

# source employees: the vowsls official row that wins per eb_id
cur.execute("""
    SELECT o.* FROM vowsls.tbl_hrms_ed_official_details o
      JOIN (SELECT eb_id, MAX(is_active) ia FROM vowsls.tbl_hrms_ed_official_details
             WHERE UPPER(TRIM(emp_code)) IN %s AND branch_id = %s GROUP BY eb_id) k
        ON k.eb_id = o.eb_id AND k.ia = o.is_active
     WHERE UPPER(TRIM(o.emp_code)) IN %s AND o.branch_id = %s
     ORDER BY o.eb_id, o.tbl_hrms_ed_official_detail_id DESC
""", (CODES, BRANCH, CODES, BRANCH))
official = {}
for r in cur.fetchall():
    official.setdefault(r["eb_id"], r)          # first per eb_id = highest detail id
if len(official) != len(CODES):
    sys.exit(f"ABORT: expected {len(CODES)} source employees, found {len(official)}")
EB = tuple(official)
log(f"source employees: {len(EB)}  eb_id {min(EB)}-{max(EB)}")

# every referenced master must exist under branch 4 in sls
for col, tbl, key, scoped in (("designation_id", "sls.designation_mst", "designation_id", True),
                              ("department_id", "sls.sub_dept_mst", "sub_dept_id", False)):
    ids = tuple({r[col] for r in official.values() if r[col] is not None})
    where = f"{key} IN %s" + (f" AND branch_id = {BRANCH}" if scoped else "")
    cur.execute(f"SELECT {key} k FROM {tbl} WHERE {where}", (ids,))
    bad = set(ids) - {x["k"] for x in cur.fetchall()}
    if bad:
        sys.exit(f"ABORT: {tbl} missing ids {sorted(bad)} (branch {BRANCH}) referenced by {col}")

# ------------------------------------------------- category: find-or-create in branch 4
cat_map = {}
for cid in {r["catagory_id"] for r in official.values() if r["catagory_id"] is not None}:
    row = one("SELECT cata_id, cata_code, cata_desc, branch_id FROM sls.category_mst WHERE cata_id=%s", (cid,))
    if row and row["branch_id"] == BRANCH:
        cat_map[cid] = cid
        continue
    src = row or one("SELECT cata_id, cata_code, cata_desc FROM sls.category_mst WHERE cata_id=%s", (cid,))
    desc = src["cata_desc"] if src else str(cid)
    code = src["cata_code"] if src else str(cid)
    hit = one("""SELECT cata_id FROM sls.category_mst
                  WHERE branch_id=%s AND UPPER(TRIM(cata_desc))=UPPER(TRIM(%s))""", (BRANCH, desc))
    if hit:
        cat_map[cid] = hit["cata_id"]
        log(f"  category {cid} '{desc}' -> existing branch-{BRANCH} cata_id {hit['cata_id']}")
    else:
        cur.execute("""INSERT INTO sls.category_mst (cata_code, cata_desc, branch_id, updated_by,
                                                     updated_date_time)
                       VALUES (%s, %s, %s, 0, NOW())""", (code, desc, BRANCH))
        cat_map[cid] = cur.lastrowid
        log(f"  category {cid} '{desc}' -> CREATED branch-{BRANCH} cata_id {cur.lastrowid}")

# ------------------------------------------------------------------- eb_id map
cur.execute(f"""CREATE TABLE IF NOT EXISTS {MAP} (
    legacy_eb_id INT NOT NULL PRIMARY KEY,
    new_eb_id    INT NOT NULL,
    emp_code     VARCHAR(50),
    branch_id    INT,
    migrated_at  DATETIME DEFAULT CURRENT_TIMESTAMP)""")
nxt = one("SELECT COALESCE(MAX(eb_id),0)+1 n FROM sls.hrms_ed_personal_details")["n"]
eb_map = {}
for i, eb in enumerate(sorted(EB)):
    eb_map[eb] = nxt + i
    cur.execute(f"INSERT INTO {MAP} (legacy_eb_id, new_eb_id, emp_code, branch_id) VALUES (%s,%s,%s,%s)",
                (eb, eb_map[eb], official[eb]["emp_code"], BRANCH))
log(f"eb_id map: {min(EB)}-{max(EB)} -> {min(eb_map.values())}-{max(eb_map.values())}")

# ------------------------------------------------------------------- personal
cur.execute("SELECT * FROM vowsls.tbl_hrms_ed_personal_details WHERE eb_id IN %s", (EB,))
n_personal = 0
for p in cur.fetchall():
    cur.execute("""INSERT INTO sls.hrms_ed_personal_details
        (eb_id, first_name, middle_name, last_name, gender, date_of_birth, blood_group, email_id,
         marital_status, country_id, relegion_name, fixed_eb_id, father_spouse_name, passport_no,
         driving_licence_no, pan_no, aadhar_no, branch_id, updated_by, updated_date_time,
         active, status_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[p["eb_id"]], p["first_name"], p["middle_name"], p["last_name"], p["gender"],
         p["date_of_birth"], p["blood_group"], p["email_id"], p["marital_status"], p["country_id"],
         p["relegion_name"], p["fixed_eb_id"], p["father_spouse_name"], p["passport_no"],
         p["driving_licence_no"], p["pan_no"], p["aadhar_no"], BRANCH,
         p["updated_by"] or p["created_by"] or 0, p["updated_time"] or p["created_time"],
         p["is_active"], p["status"]))
    n_personal += 1

# ------------------------------------------------------------------- official
for eb, o in official.items():
    cur.execute("""INSERT INTO sls.hrms_ed_official_details
        (eb_id, updated_by, updated_date_time, active, sub_dept_id, catagory_id, designation_id,
         branch_id, date_of_join, probation_period, minimum_working_commitment, reporting_eb_id,
         emp_code, legacy_code, contractor_id, office_mobile_no, office_email_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[eb], o["updated_by"] or 0, o["updated_time"], o["is_active"], o["department_id"],
         cat_map.get(o["catagory_id"]), o["designation_id"], BRANCH, o["date_of_join"],
         o["probation_period"], o["minimum_working_commitment"], eb_map.get(o["reporting_eb_id"]),
         o["emp_code"], o["legacy_code"], o["contractor_id"], o["office_mobile_no"],
         o["office_email_id"]))

# -------------------------------------------------------------- address / bank / contact
cur.execute("SELECT * FROM vowsls.tbl_hrms_ed_address_details WHERE eb_id IN %s", (EB,))
addr = cur.fetchall()
for a in addr:
    cur.execute("""INSERT INTO sls.hrms_ed_address_details
        (eb_id, address_type, country_id, state_id, city_name, address_line_1, address_line_2,
         pin_code, active, is_correspondent_address, updated_by, updated_date_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[a["eb_id"]], a["address_type"], a["country_id"], a["state_id"], a["city_name"],
         a["address_line_1"], a["address_line_2"], a["pin_code"], a["is_active"],
         a["is_correspondent_address"], a["created_by"] or 0, a["created_time"]))

cur.execute("SELECT * FROM vowsls.tbl_hrms_ed_bank_details WHERE eb_id IN %s", (EB,))
bank = cur.fetchall()
for b in bank:
    cur.execute("""INSERT INTO sls.hrms_ed_bank_details
        (ifsc_code, bank_acc_no, active, updated_by, updated_date_time, bank_name, is_verified,
         bank_branch_name, eb_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (b["ifsc_code"], b["bank_acc_no"], b["is_active"], b["updated_by"] or 0, b["updated_time"],
         b["bank_name"], b["is_verified"], b["bank_branch_name"], eb_map[b["eb_id"]]))

cur.execute("SELECT * FROM vowsls.tbl_hrms_ed_contact_details WHERE eb_id IN %s", (EB,))
cont = cur.fetchall()
for t in cont:
    cur.execute("""INSERT INTO sls.hrms_ed_contact_details
        (eb_id, mobile_no, emergency_no, active, updated_by, updated_date_time)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (eb_map[t["eb_id"]], t["mobile_no"], t["emergency_no"], t["is_active"],
         t["updated_by"] or 0, t["updated_time"]))

log(f"inserted: personal {n_personal}, official {len(official)}, address {len(addr)}, "
    f"bank {len(bank)}, contact {len(cont)}")

# ------------------------------------------------------------------ verify
chk = one("""SELECT COUNT(*) c FROM sls.hrms_ed_official_details
              WHERE branch_id=%s AND UPPER(TRIM(emp_code)) IN %s""", (BRANCH, CODES))["c"]
assert chk == len(CODES), f"post-insert check failed: {chk}/{len(CODES)} codes present"
orph = one(f"""SELECT COUNT(*) c FROM sls.hrms_ed_official_details o
                LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id=o.eb_id
                JOIN {MAP} m ON m.new_eb_id=o.eb_id WHERE p.eb_id IS NULL""")["c"]
assert orph == 0, f"{orph} official rows without a personal row"
log(f"verify OK: {chk}/{len(CODES)} emp_codes now in sls branch {BRANCH}, no orphans")

for eb in sorted(EB):
    o = official[eb]
    log(f"  {o['emp_code']:>9}  eb {eb} -> {eb_map[eb]}  doj {o['date_of_join']}  active {o['is_active']}")

if COMMIT:
    conn.commit()
    log("\nCOMMITTED")
else:
    conn.rollback()
    log("\nDRY RUN — rolled back. Re-run with --commit to apply.")
