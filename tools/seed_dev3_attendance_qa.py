"""Seed dev3 with QA fixture data for the MIS-port Jute Production reports.

dev3.daily_attendance is empty, so every report that joins attendance (hands,
spinning/winding low-producer) returns nothing and cannot be QA'd in the browser.
This lays down a small, rerun-safe fixture on the Empire / Factory branch.

Writes two things, both scoped and delete-first:
  1. daily_attendance  — approved rows for the real dev3 eb_ids across 3 dates x
     the three 8-hour full-shift spells (A / B / C).
  2. jute_prod_hands_norms_mst    — 3 QA norm rows so the Hands Report has targets to compare
     against. The EMPMILL12 seed resolves ZERO designations on dev3 (dev3 has only
     3 designations, none matching legacy occupation names), so without these the
     norms table stays empty. Uses effective_date 2026-01-01 to stay clear of the
     EMPMILL12 seed's (co_id, effective_date='2000-01-01') delete scope.

The FINISHING block (--only finishing) seeds machine attendance for
POST /finishingProd/entry_sync_eb, on the branch where dev3's hemming / herackle
machines actually live (co 9 / branch 12, NOT the co 1 / branch 2 above):
  3. designation_mst   — dev3 ships 3 designations ('Desig 1' / 'Weaver' / 'Staff'),
     none of them a hemming or heracle name, so the sync's designation gate would
     match nothing. Four rows are created if absent (NOT EXISTS guard, never
     re-inserted). Note HERACLE (no k): the process token is 'herackle' but the
     shop-floor designation is not.
  4. daily_attendance + daily_ebmc_attendance — one machine per sync outcome:
     machine #1 a single operator (stamps), machine #2 an operator + a helper
     (operator wins), machine #3 two operators (reported ambiguous, never guessed).
     Machines are looked up by machine_type + branch, exactly as the entry form
     lists them — never hardcoded ids. A process with fewer machines gets fewer
     scenarios (dev3 has only ONE machine of type 'Herackle', so herackle covers
     the single-operator case only).
     The daily_ebmc_attendance LINE columns (branch_id / attendace_date /
     designation_id) are seeded NULL ON PURPOSE: that is what mobile marking
     leaves behind, and the sync query must never read them.

Known dev3 limitation: only eb_id 3 has an hrms_ed_official_details row, so
reports resolving emp_code from it will show NULL for eb_id 1 and 2. That is a
real edge case (worker with no official details), not a bug — eb_no/eb_code are
populated on the attendance rows themselves. It also means eb 1 / 2 cannot be
typed into the finishing entry form by hand (that path validates the HRMS branch);
the sync stamps them regardless, which is the point of the fixture.

Usage:
    python tools/seed_dev3_attendance_qa.py [--dry-run] [--only hands|finishing]
"""

import argparse

import pymysql
from dotenv import dotenv_values

TENANT = "dev3"
CO_ID = 1              # The Empire Jute Company Limited
BRANCH_ID = 2          # Factory
DATES = ["2026-08-20", "2026-08-21", "2026-08-22"]
SOURCE_TAG = "QA_SEED"

# spell_id -> (spell_code, working_hours). The three 8-hour full-shift spells on
# dev3 (ids 1-5 are the A1/A2/B1/B2/C part-spells with 3-5 hour lengths).
SPELLS = {6: ("A", 8.0), 7: ("B", 8.0), 8: ("C", 7.5)}

# eb_id -> (eb_no, designation_id, department_id). All real dev3 rows.
WORKERS = {
    1: ("10001", 1, 11),   # Desig 1  / jute
    2: ("10002", 3, 91),   # Staff    / ACCOUNTS
    3: ("10000", 2, 3),    # Weaver   / Batching Dept
}

# designation_id -> (branch_id, direct_indirect, variable_fixed, a, b, c, g,
#                    machine_type_id, hands_per_machine, norms_text)
# Covers all three target paths: exact-branch fixed, exact-branch VARIABLE
# (machine-linked, dev3 machine_type 6 = 'LOOM'), and co-wide NULL-branch fixed.
QA_NORMS = {
    1: (BRANCH_ID, "D", "F", 2, 2, 1, 0, None, None, "A=2,B=2,C=1"),
    2: (BRANCH_ID, "D", "V", 0, 0, 0, 0, 6, 0.5, "2 MC = 1 HAND"),
    3: (None, "I", "F", 1, 1, 0, 1, None, None, "1 PER SHIFT"),
}
NORM_EFFECTIVE_DATE = "2026-01-01"

# --- finishing block (entry_sync_eb QA) -------------------------------------
FIN_CO_ID = 9          # the company that owns dev3's finishing machines
FIN_BRANCH_ID = 12     # its Factory branch (dept 'Finishing')
FIN_DEPT_ID = 15011
FIN_DATE = DATES[0]
FIN_SPELL = (6, "A", 8.0)   # spell_id, code, working_hours — branch 12's A shift
FIN_SOURCE_TAG = "QA_SEED_FIN"

# process -> machine_type_mst.machine_type_name tuple. MUST stay in step with
# src/juteProduction/constants.FINISHING_MACHINE_TYPE_NAMES — the seed has to pick the
# SAME machines the entry form offers, or the seeded attendance points at nothing.
FIN_MACHINE_TYPES = {"hemming": ("Hemming",), "herackle": ("Herackle",)}

# process -> (operator designation, helper designation). The names must start with
# constants.FINISHING_SYNC_DESIG_PREFIX ('hemming%' / 'heracle%') and the operator one
# must contain 'operator' (that word is what breaks the operator/helper tie).
FIN_DESIGS = {
    "hemming": ("HEMMING OPERATOR", "HEMMING HELPER"),
    "herackle": ("HERACLE OPERATOR", "HERACLE HELPER"),
}

# One scenario per machine slot, in order: the (role, eb_id) marks on that machine.
FIN_SCENARIOS = [
    ("single operator -> stamps", [("op", 1)]),
    ("operator + helper -> operator wins", [("op", 2), ("help", 3)]),
    ("two operators -> ambiguous, skipped", [("op", 1), ("op", 2)]),
]


def _ensure_designations(cur):
    """Create the hemming / heracle designations if dev3 lacks them; return name -> id.

    NOT EXISTS guarded on (desig, branch_id), so a re-run neither duplicates them nor
    disturbs ids that seeded attendance already points at."""
    names = [n for pair in FIN_DESIGS.values() for n in pair]
    for name in names:
        cur.execute(
            "INSERT INTO designation_mst (branch_id, dept_id, desig, direct_indirect,"
            " active, updated_by)"
            " SELECT %s, %s, %s, 'D', 1, 1 FROM DUAL WHERE NOT EXISTS"
            " (SELECT 1 FROM designation_mst WHERE desig = %s AND branch_id = %s)",
            (FIN_BRANCH_ID, FIN_DEPT_ID, name, name, FIN_BRANCH_ID),
        )
        if cur.rowcount:
            print(f"  created designation {name!r}")
    cur.execute(
        "SELECT designation_id, desig FROM designation_mst"
        " WHERE branch_id = %s AND desig IN %s",
        (FIN_BRANCH_ID, tuple(names)),
    )
    return {r["desig"]: r["designation_id"] for r in cur.fetchall()}


def _ensure_hrms_worker(cur):
    """Give eb 2 an hrms_ed_official_details row on the finishing branch.

    The typed-EB entry path (employee_lookup + entry_save's _worker_branch) resolves
    workers through official details, and dev3 ships NONE on branch 12 — so without
    this row the EB-only / machine+EB forms cannot be exercised at all. eb 2 already
    has an active personal-details row (the lookup INNER JOINs it); emp_code matches
    the WORKERS fixture ('10002'). NOT EXISTS on (eb_id, branch_id), so a re-run
    never duplicates and never disturbs the id."""
    emp_code = WORKERS[2][0]
    cur.execute(
        "SELECT designation_id FROM designation_mst"
        " WHERE branch_id = %s AND desig = %s",
        (FIN_BRANCH_ID, FIN_DESIGS["hemming"][0]),
    )
    desig_id = cur.fetchone()["designation_id"]
    cur.execute(
        "INSERT INTO hrms_ed_official_details (eb_id, updated_by, active, sub_dept_id,"
        " catagory_id, designation_id, branch_id, minimum_working_commitment,"
        " reporting_eb_id, emp_code)"
        " SELECT 2, 1, 1, 1, 1, %s, %s, 0, 2, %s FROM DUAL WHERE NOT EXISTS"
        " (SELECT 1 FROM hrms_ed_official_details WHERE eb_id = 2 AND branch_id = %s)",
        (desig_id, FIN_BRANCH_ID, emp_code, FIN_BRANCH_ID),
    )
    if cur.rowcount:
        print(f"  created official-details row: eb 2 / emp_code {emp_code} on branch {FIN_BRANCH_ID}")


def _finishing_marks(cur, desig_ids):
    """Resolve the machines per process and pair them with the scenarios.

    Returns [(process, machine_id, mech_code, label, [(eb_id, designation_id)])].
    Machines come from the SAME machine_type + branch lookup the entry form uses, so a
    machine that is not offered for the process is never seeded."""
    marks = []
    for process, types in FIN_MACHINE_TYPES.items():
        cur.execute(
            "SELECT m.machine_id, m.mech_code, m.machine_name FROM machine_mst m"
            " JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id"
            " JOIN dept_mst d ON d.dept_id = m.dept_id"
            " WHERE m.active = 1 AND mt.active = 1 AND mt.machine_type_name IN %s"
            " AND d.branch_id = %s ORDER BY m.machine_id",
            (types, FIN_BRANCH_ID),
        )
        machines = cur.fetchall()
        if not machines:
            print(f"  WARNING: no {process} machines on branch {FIN_BRANCH_ID} — skipped")
            continue
        if len(machines) < len(FIN_SCENARIOS):
            print(f"  note: {process} has {len(machines)} machine(s), "
                  f"{len(FIN_SCENARIOS) - len(machines)} scenario(s) not covered")
        op, helper = FIN_DESIGS[process]
        for machine, (label, roles) in zip(machines, FIN_SCENARIOS):
            marks.append((
                process, machine["machine_id"], machine["mech_code"], label,
                [(eb, desig_ids[op if role == "op" else helper]) for role, eb in roles],
            ))
    return marks


def seed_finishing(cur):
    """daily_attendance + daily_ebmc_attendance for the entry_sync_eb QA scenarios."""
    spell_id, spell_code, whrs = FIN_SPELL
    _ensure_hrms_worker(cur)
    marks = _finishing_marks(cur, _ensure_designations(cur))
    for process, machine_id, mech_code, label, people in marks:
        print(f"  {process:9s} mc {machine_id} ({mech_code}): {label} "
              f"-> eb {[eb for eb, _ in people]}")

    # delete-first, scoped to this fixture's own source tag so real attendance is safe
    cur.execute(
        "DELETE FROM daily_ebmc_attendance WHERE daily_atten_id IN"
        " (SELECT daily_atten_id FROM daily_attendance"
        "  WHERE branch_id = %s AND attendance_date = %s AND attendance_source = %s)",
        (FIN_BRANCH_ID, FIN_DATE, FIN_SOURCE_TAG),
    )
    print(f"  deleted {cur.rowcount} existing ebmc row(s)")
    cur.execute(
        "DELETE FROM daily_attendance WHERE branch_id = %s AND attendance_date = %s"
        " AND attendance_source = %s",
        (FIN_BRANCH_ID, FIN_DATE, FIN_SOURCE_TAG),
    )
    print(f"  deleted {cur.rowcount} existing attendance row(s)")

    n_att = n_line = 0
    for process, machine_id, _mech, _label, people in marks:
        for eb_id, designation_id in people:
            eb_no = WORKERS[eb_id][0]
            # header carries EVERY predicate the sync reads: date, spell, branch, eb,
            # is_active and worked_designation_id (the gate).
            cur.execute(
                "INSERT INTO daily_attendance (attendance_date, branch_id, eb_id, eb_code,"
                " eb_no, spell_id, spell, working_hours, idle_hours, spell_hours, is_active,"
                " status_id, worked_designation_id, worked_department_id, attendance_mark,"
                " attendance_type, attendance_source)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (FIN_DATE, FIN_BRANCH_ID, eb_id, eb_no, eb_no, spell_id, spell_code,
                 whrs, 0.0, whrs, 1, "3", designation_id, FIN_DEPT_ID,
                 "P", "R", FIN_SOURCE_TAG),
            )
            n_att += 1
            # line row: mc_id + the header link, everything else NULL — mobile marking
            # leaves branch_id / attendace_date / designation_id empty, and the sync
            # must still find this row.
            cur.execute(
                "INSERT INTO daily_ebmc_attendance (daily_atten_id, eb_id, eb_no, eb_code,"
                " mc_id, spell_id, spell, is_active)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
                (cur.lastrowid, eb_id, eb_no, eb_no, machine_id, spell_id, spell_code),
            )
            n_line += 1
    print(f"  inserted {n_att} attendance + {n_line} ebmc row(s)")


def seed_hands(cur, dry_run: bool):
    att_rows = []
    for d in DATES:
        for eb_id, (eb_no, desig_id, dept_id) in WORKERS.items():
            for spell_id, (spell_code, whrs) in SPELLS.items():
                # vary idle 0.0 / 0.5 / 1.0 so (working_hours - idle_hours) is not constant
                idle = round(((eb_id + spell_id) % 3) * 0.5, 2)
                att_rows.append((
                    d, BRANCH_ID, eb_id, eb_no, eb_no, spell_id, spell_code,
                    whrs, idle, whrs, 1, "3", desig_id, dept_id,
                    "P", "R", SOURCE_TAG,
                ))

    norm_rows = [
        (CO_ID, br, desig_id, di, vf, a, b, c, g, mt, hpm, txt, NORM_EFFECTIVE_DATE, 1)
        for desig_id, (br, di, vf, a, b, c, g, mt, hpm, txt) in QA_NORMS.items()
    ]

    print(f"{TENANT}: {len(att_rows)} attendance rows "
          f"({len(WORKERS)} eb x {len(DATES)} dates x {len(SPELLS)} spells), "
          f"{len(norm_rows)} hands_norm rows")
    if dry_run:
        for r in att_rows[:6]:
            print("   att:", r)
        for r in norm_rows:
            print("  norm:", r)
        return

    cur.execute(
        "DELETE FROM daily_attendance WHERE branch_id=%s"
        " AND attendance_date BETWEEN %s AND %s AND eb_id IN %s",
        (BRANCH_ID, DATES[0], DATES[-1], tuple(WORKERS)),
    )
    print(f"  deleted {cur.rowcount} existing attendance row(s)")
    cur.executemany(
        "INSERT INTO daily_attendance (attendance_date, branch_id, eb_id, eb_code, eb_no,"
        " spell_id, spell, working_hours, idle_hours, spell_hours, is_active, status_id,"
        " worked_designation_id, worked_department_id, attendance_mark, attendance_type,"
        " attendance_source)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        att_rows,
    )
    print(f"  inserted {cur.rowcount} attendance row(s)")

    cur.execute(
        "DELETE FROM jute_prod_hands_norms_mst WHERE co_id=%s AND effective_date=%s"
        " AND designation_id IN %s",
        (CO_ID, NORM_EFFECTIVE_DATE, tuple(QA_NORMS)),
    )
    print(f"  deleted {cur.rowcount} existing hands_norm row(s)")
    cur.executemany(
        "INSERT INTO jute_prod_hands_norms_mst (co_id, branch_id, designation_id, direct_indirect,"
        " variable_fixed, shift_a, shift_b, shift_c, shift_g, machine_type_id,"
        " hands_per_machine, norms_text, effective_date, active)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        norm_rows,
    )
    print(f"  inserted {cur.rowcount} hands_norm row(s)")


def verify_hands(cur):
    """Read back what actually landed — the check that this script did its job."""
    cur.execute(
        "SELECT attendance_date, spell, COUNT(*) n, SUM(working_hours - idle_hours) net_hrs"
        " FROM daily_attendance WHERE branch_id=%s AND attendance_source=%s"
        " GROUP BY attendance_date, spell ORDER BY attendance_date, spell",
        (BRANCH_ID, SOURCE_TAG),
    )
    print("\nverify daily_attendance:")
    for r in cur.fetchall():
        print(f"   {r['attendance_date']} {r['spell']}: {r['n']} rows, "
              f"net {r['net_hrs']} hrs = {float(r['net_hrs']) / 8:.3f} hands")
    cur.execute(
        "SELECT designation_id, branch_id, variable_fixed, shift_a, shift_b, shift_c,"
        " shift_g, machine_type_id, hands_per_machine FROM jute_prod_hands_norms_mst"
        " WHERE co_id=%s ORDER BY designation_id", (CO_ID,),
    )
    print("verify jute_prod_hands_norms_mst:")
    for r in cur.fetchall():
        print("  ", r)


def verify_finishing(cur):
    """The rows the sync will actually read: one line per (machine, worker, desig)."""
    cur.execute(
        "SELECT dea.mc_id, m.mech_code, da.eb_id, dm.desig"
        " FROM daily_ebmc_attendance dea"
        " JOIN daily_attendance da ON da.daily_atten_id = dea.daily_atten_id"
        " LEFT JOIN machine_mst m ON m.machine_id = dea.mc_id"
        " LEFT JOIN designation_mst dm ON dm.designation_id = da.worked_designation_id"
        " WHERE da.branch_id=%s AND da.attendance_date=%s AND da.attendance_source=%s"
        " ORDER BY dea.mc_id, da.eb_id",
        (FIN_BRANCH_ID, FIN_DATE, FIN_SOURCE_TAG),
    )
    print("\nverify finishing machine attendance:")
    for r in cur.fetchall():
        print(f"   mc {r['mc_id']} ({r['mech_code']}) eb {r['eb_id']} {r['desig']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=("hands", "finishing"),
                    help="seed just one block (default: both)")
    args = ap.parse_args()
    do_hands = args.only != "finishing"
    do_fin = args.only != "hands"

    if args.dry_run:
        if do_hands:
            seed_hands(None, True)
        if do_fin:
            print(f"{TENANT}: finishing fixture on co {FIN_CO_ID} / branch {FIN_BRANCH_ID}, "
                  f"{FIN_DATE} spell {FIN_SPELL[1]}")
        # machine resolution needs the DB, so the finishing dry-run stops at the header
        return 0

    env = dotenv_values("env/database.env")
    conn = pymysql.connect(
        host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]),
        user=env["DATABASE_USER"], password=env["DATABASE_PASSWORD"],
        database=TENANT, cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )
    cur = conn.cursor()
    try:
        if do_hands:
            seed_hands(cur, False)
        if do_fin:
            print(f"{TENANT}: finishing fixture on co {FIN_CO_ID} / branch {FIN_BRANCH_ID}, "
                  f"{FIN_DATE} spell {FIN_SPELL[1]}")
            seed_finishing(cur)
        conn.commit()
        print("COMMITTED")
        if do_hands:
            verify_hands(cur)
        if do_fin:
            verify_finishing(cur)
    except Exception:
        conn.rollback()
        print("ROLLED BACK")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
