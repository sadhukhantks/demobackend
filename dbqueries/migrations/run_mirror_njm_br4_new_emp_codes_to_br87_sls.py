"""
Mirror the 9 NJM emp_codes just migrated into sls branch 4 (co 1) across to branch 87
(co 106, Limelight) — the twin-record pattern this tenant already maintains.

Context: 2783 of 2802 branch-4 emp_codes also exist at branch 87 as a SEPARATE employee row
with its own eb_id and branch-scoped masters. The 9 rows added by
run_migrate_vowsls_njm_branch4_missing_emp_codes_to_sls.py (eb_id 59100-59108) are among the
few that only exist at branch 4; this script gives them their branch-87 twin.

Source is the branch-4 rows in sls (not vowsls) so the two sides cannot drift.

Branch-scoped FKs are remapped (verified 2026-08-15, all by matching description):
  sub_dept_id  : sub_dept_mst_sls_co1_branch87_map.csv     (e.g. 35 SPG MAIN C -> 729)
  designation  : designation_mst_sls_co1_branch87_map.csv  (e.g. 1141 -> 1907)
  catagory_id  : branch-4 cata_desc -> branch-87 cata_id   (RETIRED 20->68, OUTSIDER(CONT) 22->91,
                 SERVICE STAFF 48->97, NEW BUDLI RTD 104->83)
eb_id is freshly allocated; map persisted in vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026.

ROLLBACK (run only if a committed run must be reversed):
  DELETE x FROM sls.hrms_ed_official_details x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_address_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_bank_details     x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026 m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_contact_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026 m ON m.new_eb_id=x.eb_id;
  DELETE p FROM sls.hrms_ed_personal_details p JOIN vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026 m ON m.new_eb_id=p.eb_id;
  DROP TABLE vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026;

Usage:
  python run_mirror_njm_br4_new_emp_codes_to_br87_sls.py            # dry run (rolls back)
  python run_mirror_njm_br4_new_emp_codes_to_br87_sls.py --commit   # apply
"""
import csv
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv

HERE = Path(__file__).resolve().parent
ENV = {}
for line in (HERE.parents[1] / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

CODES = ("115157", "117165", "127102", "127127", "665088", "C910272", "U0268", "U0269", "U0270")
SRC_BRANCH, TGT_BRANCH = 4, 87
MAP = "vowsls_to_sls_transfer._map_hrms_eb_njm_br87_mirror_2026"

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()


def one(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------ map table FIRST
# DDL implicitly commits in MySQL, so it must precede every INSERT or a dry run
# would leave the rows before it committed.
cur.execute(f"""CREATE TABLE IF NOT EXISTS {MAP} (
    legacy_eb_id INT NOT NULL PRIMARY KEY,   -- the branch-4 sls eb_id
    new_eb_id    INT NOT NULL,
    emp_code     VARCHAR(50),
    branch_id    INT,
    migrated_at  DATETIME DEFAULT CURRENT_TIMESTAMP)""")
if one(f"SELECT COUNT(*) c FROM {MAP}")["c"]:
    sys.exit(f"ABORT: {MAP} already has rows — use the rollback block in the header first.")

# ---------------------------------------------------------------- pre-flight
clash = one("""SELECT COUNT(*) c FROM sls.hrms_ed_official_details
                WHERE branch_id=%s AND UPPER(TRIM(emp_code)) IN %s""", (TGT_BRANCH, CODES))["c"]
if clash:
    sys.exit(f"ABORT: {clash} of these emp_codes already exist at branch {TGT_BRANCH}")

cur.execute("""SELECT * FROM sls.hrms_ed_official_details
                WHERE branch_id=%s AND UPPER(TRIM(emp_code)) IN %s ORDER BY eb_id""",
            (SRC_BRANCH, CODES))
official = {r["eb_id"]: r for r in cur.fetchall()}
if len(official) != len(CODES):
    sys.exit(f"ABORT: expected {len(CODES)} source rows at branch {SRC_BRANCH}, found {len(official)}")
EB = tuple(official)
log(f"source: {len(EB)} branch-{SRC_BRANCH} employees, eb_id {min(EB)}-{max(EB)}")

# ------------------------------------------------------- branch-scoped FK maps
sub_map = {int(r["vowsls_dept_id"]): int(r["sls_sub_dept_id"])
           for r in csv.DictReader((HERE / "sub_dept_mst_sls_co1_branch87_map.csv").open(newline=""))}
des_map = {int(r["vowsls_designation_id"]): int(r["sls_designation_id"])
           for r in csv.DictReader((HERE / "designation_mst_sls_co1_branch87_map.csv").open(newline=""))}

cat_map = {}
for cid in {r["catagory_id"] for r in official.values() if r["catagory_id"] is not None}:
    src = one("SELECT cata_desc FROM sls.category_mst WHERE cata_id=%s", (cid,))
    tgt = one("""SELECT cata_id FROM sls.category_mst
                  WHERE branch_id=%s AND UPPER(TRIM(cata_desc))=UPPER(TRIM(%s))""",
              (TGT_BRANCH, src["cata_desc"]))
    if not tgt:
        sys.exit(f"ABORT: no branch-{TGT_BRANCH} category matching '{src['cata_desc']}' (cata_id {cid})")
    cat_map[cid] = tgt["cata_id"]
    log(f"  category {cid} '{src['cata_desc']}' -> {tgt['cata_id']}")

# every mapped FK must really live under the target branch
for eb, o in official.items():
    for label, val, mp in (("sub_dept", o["sub_dept_id"], sub_map),
                           ("designation", o["designation_id"], des_map)):
        if val is not None and val not in mp:
            sys.exit(f"ABORT: no branch-{TGT_BRANCH} {label} mapping for id {val} (eb {eb})")
des_ids = tuple({des_map[o["designation_id"]] for o in official.values() if o["designation_id"]})
cur.execute("SELECT designation_id d FROM sls.designation_mst WHERE designation_id IN %s AND branch_id=%s",
            (des_ids, TGT_BRANCH))
bad = set(des_ids) - {r["d"] for r in cur.fetchall()}
if bad:
    sys.exit(f"ABORT: designations {sorted(bad)} are not on branch {TGT_BRANCH}")
sub_ids = tuple({sub_map[o["sub_dept_id"]] for o in official.values() if o["sub_dept_id"]})
cur.execute("""SELECT s.sub_dept_id s FROM sls.sub_dept_mst s JOIN sls.dept_mst d ON d.dept_id=s.dept_id
                WHERE s.sub_dept_id IN %s AND d.branch_id=%s""", (sub_ids, TGT_BRANCH))
bad = set(sub_ids) - {r["s"] for r in cur.fetchall()}
if bad:
    sys.exit(f"ABORT: sub_depts {sorted(bad)} are not under a branch-{TGT_BRANCH} dept")

# ------------------------------------------------------------------- eb_id map
nxt = one("SELECT COALESCE(MAX(eb_id),0)+1 n FROM sls.hrms_ed_personal_details")["n"]
eb_map = {eb: nxt + i for i, eb in enumerate(sorted(EB))}
for eb, new in eb_map.items():
    cur.execute(f"INSERT INTO {MAP} (legacy_eb_id, new_eb_id, emp_code, branch_id) VALUES (%s,%s,%s,%s)",
                (eb, new, official[eb]["emp_code"], TGT_BRANCH))
log(f"eb_id map: {min(EB)}-{max(EB)} -> {min(eb_map.values())}-{max(eb_map.values())}")

# ------------------------------------------------------------------- personal
cur.execute("SELECT * FROM sls.hrms_ed_personal_details WHERE eb_id IN %s", (EB,))
persons = cur.fetchall()
for p in persons:
    cur.execute("""INSERT INTO sls.hrms_ed_personal_details
        (eb_id, first_name, middle_name, last_name, gender, date_of_birth, blood_group, mobile_no,
         email_id, marital_status, country_id, relegion_name, fixed_eb_id, father_spouse_name,
         passport_no, driving_licence_no, pan_no, aadhar_no, branch_id, updated_by,
         updated_date_time, active, status_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[p["eb_id"]], p["first_name"], p["middle_name"], p["last_name"], p["gender"],
         p["date_of_birth"], p["blood_group"], p["mobile_no"], p["email_id"], p["marital_status"],
         p["country_id"], p["relegion_name"], p["fixed_eb_id"], p["father_spouse_name"],
         p["passport_no"], p["driving_licence_no"], p["pan_no"], p["aadhar_no"], TGT_BRANCH,
         p["updated_by"], p["updated_date_time"], p["active"], p["status_id"]))

# ------------------------------------------------------------------- official
for eb, o in official.items():
    cur.execute("""INSERT INTO sls.hrms_ed_official_details
        (eb_id, updated_by, updated_date_time, active, sub_dept_id, catagory_id, designation_id,
         branch_id, date_of_join, probation_period, minimum_working_commitment, reporting_eb_id,
         emp_code, legacy_code, contractor_id, office_mobile_no, office_email_id, off_day)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[eb], o["updated_by"], o["updated_date_time"], o["active"],
         sub_map.get(o["sub_dept_id"]), cat_map.get(o["catagory_id"]),
         des_map.get(o["designation_id"]), TGT_BRANCH, o["date_of_join"], o["probation_period"],
         o["minimum_working_commitment"], eb_map.get(o["reporting_eb_id"]), o["emp_code"],
         o["legacy_code"], o["contractor_id"], o["office_mobile_no"], o["office_email_id"],
         o["off_day"]))

# -------------------------------------------------------------- address / bank / contact
cur.execute("SELECT * FROM sls.hrms_ed_address_details WHERE eb_id IN %s", (EB,))
addr = cur.fetchall()
for a in addr:
    cur.execute("""INSERT INTO sls.hrms_ed_address_details
        (eb_id, address_type, country_id, state_id, city_name, address_line_1, address_line_2,
         pin_code, active, is_correspondent_address, updated_by, updated_date_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (eb_map[a["eb_id"]], a["address_type"], a["country_id"], a["state_id"], a["city_name"],
         a["address_line_1"], a["address_line_2"], a["pin_code"], a["active"],
         a["is_correspondent_address"], a["updated_by"], a["updated_date_time"]))

cur.execute("SELECT * FROM sls.hrms_ed_bank_details WHERE eb_id IN %s", (EB,))
bank = cur.fetchall()
for b in bank:
    cur.execute("""INSERT INTO sls.hrms_ed_bank_details
        (ifsc_code, bank_acc_no, active, updated_by, updated_date_time, bank_name, is_verified,
         bank_branch_name, eb_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (b["ifsc_code"], b["bank_acc_no"], b["active"], b["updated_by"], b["updated_date_time"],
         b["bank_name"], b["is_verified"], b["bank_branch_name"], eb_map[b["eb_id"]]))

cur.execute("SELECT * FROM sls.hrms_ed_contact_details WHERE eb_id IN %s", (EB,))
cont = cur.fetchall()
for t in cont:
    cur.execute("""INSERT INTO sls.hrms_ed_contact_details
        (eb_id, mobile_no, emergency_no, active, updated_by, updated_date_time)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (eb_map[t["eb_id"]], t["mobile_no"], t["emergency_no"], t["active"], t["updated_by"],
         t["updated_date_time"]))

log(f"inserted: personal {len(persons)}, official {len(official)}, address {len(addr)}, "
    f"bank {len(bank)}, contact {len(cont)}")

# ------------------------------------------------------------------ verify
chk = one("""SELECT COUNT(*) c FROM sls.hrms_ed_official_details
              WHERE branch_id=%s AND UPPER(TRIM(emp_code)) IN %s""", (TGT_BRANCH, CODES))["c"]
assert chk == len(CODES), f"post-insert check failed: {chk}/{len(CODES)}"
orph = one(f"""SELECT COUNT(*) c FROM sls.hrms_ed_official_details o
                LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id=o.eb_id
                JOIN {MAP} m ON m.new_eb_id=o.eb_id WHERE p.eb_id IS NULL""")["c"]
assert orph == 0, f"{orph} official rows without a personal row"
xb = one(f"""SELECT COUNT(*) c FROM sls.hrms_ed_official_details o
              JOIN {MAP} m ON m.new_eb_id=o.eb_id
              LEFT JOIN sls.designation_mst d
                ON d.designation_id=o.designation_id AND d.branch_id=%s
             WHERE d.designation_id IS NULL""", (TGT_BRANCH,))["c"]
assert xb == 0, f"{xb} rows point at a designation outside branch {TGT_BRANCH}"
log(f"verify OK: {chk}/{len(CODES)} emp_codes at branch {TGT_BRANCH}, no orphans, FKs branch-clean")

for eb in sorted(EB):
    o = official[eb]
    log(f"  {o['emp_code']:>9}  eb {eb} -> {eb_map[eb]}  sub_dept {o['sub_dept_id']}->"
        f"{sub_map.get(o['sub_dept_id'])}  desig {o['designation_id']}->{des_map.get(o['designation_id'])}"
        f"  cat {o['catagory_id']}->{cat_map.get(o['catagory_id'])}")

if COMMIT:
    conn.commit()
    log("\nCOMMITTED")
else:
    conn.rollback()
    log("\nDRY RUN — rolled back. Re-run with --commit to apply.")
