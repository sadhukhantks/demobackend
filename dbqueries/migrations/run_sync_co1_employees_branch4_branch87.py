"""
Give branch 4 (FACTORY, co 1) every legacy co-1 employee, then mirror branch 4 into branch 87.

Requested 2026-08-15: "all emp code from vowsls for co_id = 1 to sls branch id 4, then all sls
emp code insert into branch id 87 from branch id 4."

WHAT IS ACTUALLY MISSING (measured, not assumed)
------------------------------------------------
vowsls co_id=1 holds 4,601 distinct emp_codes. **None of them is absent from sls** — the co1->co106
remap put them under branch 87. Against the two target branches they sit like this:

    in branch 4 .......... 2,801        in branch 87 ......... 4,592
    in BOTH .............. 2,792        absent from sls ...........0

So the job is the two gaps, not a re-import of all 4,601 (which would duplicate the 2,792 people
who already exist in both branches):

    phase `b4`  : 1,800 legacy co-1 codes missing from branch 4   (all 1,800 exist under branch 87)
    phase `b87` :    10 branch-4 codes missing from branch 87
                  ------
                  1,810 new employee records

SOURCE OF THE COPY
------------------
The existing **sls** record, not the vowsls row. It is the same person, and it guarantees every
master FK (sub_dept_id, catagory_id, designation_id, contractor_id) already resolves in sls —
copying from vowsls would risk dragging in masters that were never migrated.

Per new employee: one hrms_ed_personal_details row, ONE active hrms_ed_official_details row at the
target branch (a fresh posting, not the source's whole job history), plus every row from the six
child tables (address / bank / contact / esi / pf / resign).

`emp_code` is deliberately duplicated across the two branches — that is already the norm in sls
(2,785 emp_codes span more than one branch) and no unique key forbids it.

New eb_ids are assigned deterministically from EB_BASE and recorded in `vowsls_to_sls_transfer._map_hrms_eb_branch_clone`,
which is both the audit trail and the rollback key.

ROLLBACK:
  DELETE x FROM sls.hrms_ed_address_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_bank_details     x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_contact_details  x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_esi              x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_pf               x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_resign_details   x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_official_details x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DELETE x FROM sls.hrms_ed_personal_details x JOIN vowsls_to_sls_transfer._map_hrms_eb_branch_clone m ON m.new_eb_id=x.eb_id;
  DROP TABLE vowsls_to_sls_transfer._map_hrms_eb_branch_clone;

NOTE: the new records carry NO pay scheme assignment and NO payroll — that data stays on the
source record. Ask before wiring payroll to the clones; doing it wrong double-counts salary.

Usage:
  python run_sync_co1_employees_branch4_branch87.py                # dry run (counts only)
  python run_sync_co1_employees_branch4_branch87.py --commit
  python run_sync_co1_employees_branch4_branch87.py --commit --only b4
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
PHASES = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else "all"

EB_BASE = 60_000            # sls MAX(eb_id) is 59,117
MAP = "vowsls_to_sls_transfer._map_hrms_eb_branch_clone"

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
STATS: dict[str, object] = {}

LEGACY_CO1_CODES = """
    SELECT DISTINCT UPPER(TRIM(o.emp_code)) code
      FROM vowsls.tbl_hrms_ed_official_details o
      JOIN vowsls.tbl_hrms_ed_personal_details p ON p.eb_id = o.eb_id AND p.company_id = 1
     WHERE TRIM(COALESCE(o.emp_code, '')) <> ''
"""

# codes present at branch 4 but not at branch 87
BRANCH4_CODES = """
    SELECT DISTINCT UPPER(TRIM(o.emp_code)) code
      FROM sls.hrms_ed_official_details o
     WHERE o.branch_id = 4 AND TRIM(COALESCE(o.emp_code, '')) <> ''
"""


def log(msg):
    print(msg, flush=True)


def scalar(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def ensure_map():
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {MAP} (
            new_eb_id     int         NOT NULL PRIMARY KEY,
            source_eb_id  int         NOT NULL,
            emp_code      varchar(50) NULL,
            target_branch int         NOT NULL,
            phase         varchar(8)  NOT NULL,
            KEY idx_clone_source (source_eb_id)
        ) ENGINE=InnoDB""")
    conn.commit()


def pick_sources(code_sql, source_branch, target_branch, phase):
    """One source eb per code: prefer a row with an active official record, else the latest."""
    cur.execute(f"""
        SELECT l.code,
               (SELECT o.eb_id FROM sls.hrms_ed_official_details o
                 WHERE UPPER(TRIM(o.emp_code)) = l.code
                   AND ({'o.branch_id = %d' % source_branch if source_branch else '1=1'})
                 ORDER BY o.active DESC, o.tbl_hrms_ed_official_detail_id DESC
                 LIMIT 1) AS source_eb
          FROM ({code_sql}) l
         WHERE NOT EXISTS (SELECT 1 FROM sls.hrms_ed_official_details x
                            WHERE UPPER(TRIM(x.emp_code)) = l.code
                              AND x.branch_id = {target_branch})
           AND NOT EXISTS (SELECT 1 FROM {MAP} m
                            WHERE m.emp_code = l.code AND m.target_branch = {target_branch})""")
    rows = [(code, eb) for code, eb in cur.fetchall() if eb is not None]
    return rows


def clone(phase, code_sql, source_branch, target_branch):
    log(f"\n-- phase {phase}: -> branch {target_branch} --")
    sources = pick_sources(code_sql, source_branch, target_branch, phase)
    log(f"  employees to create: {len(sources)}")
    STATS[f"{phase}_employees"] = len(sources)
    if not sources:
        return
    if not COMMIT:
        log(f"  [dry] would allocate eb_ids from {max(EB_BASE, scalar('SELECT MAX(eb_id) FROM sls.hrms_ed_personal_details') + 1)}")
        return

    start = max(EB_BASE, scalar("SELECT MAX(eb_id) FROM sls.hrms_ed_personal_details") + 1,
                (scalar(f"SELECT COALESCE(MAX(new_eb_id), 0) FROM {MAP}") or 0) + 1)
    cur.executemany(
        f"INSERT INTO {MAP} (new_eb_id, source_eb_id, emp_code, target_branch, phase)"
        " VALUES (%s,%s,%s,%s,%s)",
        [(start + i, eb, code, target_branch, phase) for i, (code, eb) in enumerate(sources)])
    conn.commit()
    log(f"  eb_ids {start}..{start + len(sources) - 1} allocated")

    scope = f"{MAP} m ON m.source_eb_id = s.eb_id AND m.phase = '{phase}'"

    cur.execute(f"""
        INSERT INTO sls.hrms_ed_personal_details
               (eb_id, first_name, middle_name, last_name, gender, date_of_birth, blood_group,
                mobile_no, email_id, marital_status, country_id, relegion_name, fixed_eb_id,
                father_spouse_name, passport_no, driving_licence_no, pan_no, aadhar_no,
                branch_id, updated_by, active, status_id)
        SELECT m.new_eb_id, s.first_name, s.middle_name, s.last_name, s.gender, s.date_of_birth,
               s.blood_group, s.mobile_no, s.email_id, s.marital_status, s.country_id,
               s.relegion_name, s.fixed_eb_id, s.father_spouse_name, s.passport_no,
               s.driving_licence_no, s.pan_no, s.aadhar_no, {target_branch}, 0, s.active, s.status_id
          FROM sls.hrms_ed_personal_details s JOIN {scope}""")
    conn.commit()
    log(f"  personal: {cur.rowcount}")

    # exactly one active official row at the target branch
    cur.execute(f"""
        INSERT INTO sls.hrms_ed_official_details
               (eb_id, updated_by, active, sub_dept_id, catagory_id, designation_id, branch_id,
                date_of_join, probation_period, minimum_working_commitment, reporting_eb_id,
                emp_code, legacy_code, contractor_id, office_mobile_no, office_email_id, off_day)
        SELECT m.new_eb_id, 0, 1, s.sub_dept_id, s.catagory_id, s.designation_id, {target_branch},
               s.date_of_join, s.probation_period, s.minimum_working_commitment, s.reporting_eb_id,
               s.emp_code, s.legacy_code, s.contractor_id, s.office_mobile_no, s.office_email_id,
               s.off_day
          FROM sls.hrms_ed_official_details s
          JOIN {MAP} m ON m.source_eb_id = s.eb_id AND m.phase = '{phase}'
         WHERE s.tbl_hrms_ed_official_detail_id = (
                 SELECT o2.tbl_hrms_ed_official_detail_id FROM sls.hrms_ed_official_details o2
                  WHERE o2.eb_id = s.eb_id
                  ORDER BY o2.active DESC, o2.tbl_hrms_ed_official_detail_id DESC LIMIT 1)""")
    conn.commit()
    log(f"  official: {cur.rowcount}")

    children = [
        ("hrms_ed_address_details",
         "address_type, country_id, state_id, city_name, address_line_1, address_line_2, "
         "pin_code, active, is_correspondent_address"),
        ("hrms_ed_bank_details",
         "ifsc_code, bank_acc_no, active, bank_name, is_verified, bank_branch_name"),
        ("hrms_ed_contact_details", "mobile_no, emergency_no, active"),
        ("hrms_ed_esi", "active, esi_no, medical_policy_no, date_of_exit"),
        ("hrms_ed_pf",
         "active, pf_date_of_join, pf_no, pf_uan_no, pf_transfer_no, pf_previous_no, "
         "nominee_name, relationship_name, date_of_exit"),
        ("hrms_ed_resign_details",
         "active, date_of_inactive, fnf_date, net_settlement_amount, notice_days, release_date, "
         "resign_reasons, resign_remarks, resigned_date, type_of_resign, retired_date, reason_status"),
    ]
    for table, cols in children:
        picks = ", ".join(f"s.{c.strip()}" for c in cols.split(","))
        cur.execute(f"""
            INSERT INTO sls.{table} (eb_id, updated_by, {cols})
            SELECT m.new_eb_id, 0, {picks}
              FROM sls.{table} s JOIN {scope}""")
        conn.commit()
        log(f"  {table}: {cur.rowcount}")
    STATS[f"{phase}_eb_range"] = [start, start + len(sources) - 1]


def verify():
    log("\n-- verification --")
    for label, sql in [
        ("legacy co-1 codes still missing from branch 4",
         f"""SELECT COUNT(*) FROM ({LEGACY_CO1_CODES}) l
              WHERE NOT EXISTS (SELECT 1 FROM sls.hrms_ed_official_details o
                                 WHERE UPPER(TRIM(o.emp_code)) = l.code AND o.branch_id = 4)"""),
        ("branch-4 codes still missing from branch 87",
         """SELECT COUNT(*) FROM (SELECT DISTINCT UPPER(TRIM(emp_code)) code
                FROM sls.hrms_ed_official_details WHERE branch_id = 4) l
             WHERE NOT EXISTS (SELECT 1 FROM sls.hrms_ed_official_details o
                                WHERE UPPER(TRIM(o.emp_code)) = l.code AND o.branch_id = 87)"""),
        ("branch 4 headcount",
         "SELECT COUNT(DISTINCT eb_id) FROM sls.hrms_ed_official_details WHERE branch_id = 4"),
        ("branch 87 headcount",
         "SELECT COUNT(DISTINCT eb_id) FROM sls.hrms_ed_official_details WHERE branch_id = 87"),
        ("clones without a personal row (should be 0)",
         f"""SELECT COUNT(*) FROM {MAP} m
              LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id = m.new_eb_id
             WHERE p.eb_id IS NULL"""),
        ("clones without an official row (should be 0)",
         f"""SELECT COUNT(*) FROM {MAP} m
              LEFT JOIN sls.hrms_ed_official_details o ON o.eb_id = m.new_eb_id
             WHERE o.eb_id IS NULL"""),
        ("clones with an unresolved sub_dept (should be 0)",
         f"""SELECT COUNT(*) FROM {MAP} m
               JOIN sls.hrms_ed_official_details o ON o.eb_id = m.new_eb_id
               LEFT JOIN sls.sub_dept_mst d ON d.sub_dept_id = o.sub_dept_id
              WHERE o.sub_dept_id IS NOT NULL AND d.sub_dept_id IS NULL"""),
        ("clones carrying payroll (expected 0 — payroll stays on the source)",
         f"""SELECT COUNT(*) FROM sls.pay_employee_payroll r
               JOIN {MAP} m ON m.new_eb_id = r.EMPLOYEEID"""),
    ]:
        log(f"  {label}: {scalar(sql):,}")


log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}   phases: {PHASES}")
ensure_map()
if PHASES in ("all", "b4"):
    clone("b4", LEGACY_CO1_CODES, None, 4)
if PHASES in ("all", "b87"):
    clone("b87", BRANCH4_CODES, 4, 87)
if COMMIT:
    verify()
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('hrms_co1_branch4_branch87_sync', 'committed', NOW(), NOW(), %s)""",
                (json.dumps(STATS, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'hrms_co1_branch4_branch87_sync'")
else:
    log("\nDry run complete - nothing written.")
conn.close()
