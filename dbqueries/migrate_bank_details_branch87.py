"""Sync employee bank details from vowsls company 1 to sls branch 87 (NJM).

vowsls company_id = 1 is the same mill as sls branch_id = 87. Employees match on
emp_code, which is unique per branch on sls -- so every lookup is scoped to
branch 87 or it lands on the branch-4 twin.

Scope: employees active on BOTH the vowsls personal and official records, and
their ACTIVE bank row (is_active = 1). Most rows already agree; only the drift
is written:

  insert   -- employee has no bank row at all on sls
  replace  -- sls carries a different acc/ifsc: old row set active = 0, the
              vowsls one inserted (history kept, same as the payscheme migration)
  namefill -- acc/ifsc agree but sls bank_name/branch is blank and vowsls has
              one. Never the reverse: sls often has the richer name and vowsls
              a blank, and blanking it out would be a data loss.

Usage (from the backend repo root, with the repo venv):

    .venv/Scripts/python.exe dbqueries/migrate_bank_details_branch87.py
    .venv/Scripts/python.exe dbqueries/migrate_bank_details_branch87.py --apply

Without --apply it only reports. Rollback for a run is printed at the end.
"""
import argparse

import pymysql
from dotenv import dotenv_values

SOURCE_COMPANY = 1
TARGET_BRANCH = 87

SOURCE_SQL = """
    SELECT TRIM(o.emp_code) AS emp_code, b.bank_acc_no, b.ifsc_code, b.bank_name,
           b.bank_branch_name, b.is_verified, b.updated_by, b.updated_time
    FROM vowsls.tbl_hrms_ed_bank_details b
    JOIN vowsls.tbl_hrms_ed_personal_details p
         ON p.eb_id = b.eb_id AND p.company_id = %s AND p.is_active = 1
    JOIN vowsls.tbl_hrms_ed_official_details o
         ON o.eb_id = b.eb_id AND o.is_active = 1
    WHERE b.is_active = 1
    ORDER BY b.updated_time
"""

# DISTINCT because 399 employees carry two official_details rows for one eb_id.
TARGET_SQL = """
    SELECT DISTINCT TRIM(o.emp_code) AS emp_code, o.eb_id,
           b.tbl_hrms_ed_bank_detail_id AS id, b.bank_acc_no, b.ifsc_code,
           b.bank_name, b.bank_branch_name
    FROM sls.hrms_ed_official_details o
    LEFT JOIN sls.hrms_ed_bank_details b ON b.eb_id = o.eb_id AND b.active = 1
    WHERE o.branch_id = %s
"""

INSERT_SQL = """
    INSERT INTO sls.hrms_ed_bank_details
        (eb_id, ifsc_code, bank_acc_no, bank_name, bank_branch_name,
         is_verified, active, updated_by, updated_date_time)
    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
"""


def acct(row):
    return (row["bank_acc_no"].strip(), row["ifsc_code"].strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; otherwise report only")
    args = ap.parse_args()

    env = dotenv_values("d:/vownextjs/vowerp3be/.env")
    cn = pymysql.connect(host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]),
                         user=env["DATABASE_USER"], password=env["DATABASE_PASSWORD"],
                         cursorclass=pymysql.cursors.DictCursor)
    c = cn.cursor()

    c.execute(SOURCE_SQL, (SOURCE_COMPANY,))
    src = {}
    for r in c.fetchall():
        src[r["emp_code"]] = r  # ORDER BY updated_time -> newest wins

    c.execute(TARGET_SQL, (TARGET_BRANCH,))
    eb_of = {}
    tgt = {}
    for r in c.fetchall():
        eb_of[r["emp_code"]] = r["eb_id"]
        if r["id"] is not None:
            tgt.setdefault(r["emp_code"], []).append(r)

    inserts, replaces, namefills, unknown, ok = [], [], [], [], 0
    for code, s in src.items():
        eb_id = eb_of.get(code)
        if eb_id is None:
            unknown.append(code)
            continue
        rows = tgt.get(code, [])
        hit = next((t for t in rows if acct(t) == acct(s)), None)
        if hit is None:
            if rows:
                replaces.append((code, eb_id, s, rows))
            else:
                inserts.append((code, eb_id, s))
        elif ((s["bank_name"] and not hit["bank_name"])
              or (s["bank_branch_name"] and not hit["bank_branch_name"])):
            namefills.append((code, hit, s))
        else:
            ok += 1

    print(f"source active bank rows (vowsls co {SOURCE_COMPANY}): {len(src)}")
    print(f"already in sync on sls branch {TARGET_BRANCH}: {ok}")
    print(f"insert   : {len(inserts)}")
    print(f"replace  : {len(replaces)}")
    print(f"namefill : {len(namefills)}")
    print(f"emp_code not on branch {TARGET_BRANCH}: {len(unknown)} {unknown[:10]}")

    for code, eb_id, s in inserts:
        print(f"  INSERT {code} eb={eb_id} {acct(s)}")
    for code, eb_id, s, rows in replaces:
        print(f"  REPLACE {code} eb={eb_id} {[ (t['id'], acct(t)) for t in rows ]} -> {acct(s)}")
    for code, hit, s in namefills:
        print(f"  NAMEFILL {code} id={hit['id']} -> {s['bank_name']!r} / {s['bank_branch_name']!r}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    new_ids, deactivated, filled = [], [], []
    try:
        for code, eb_id, s in inserts + [(c_, e_, s_) for c_, e_, s_, _ in replaces]:
            c.execute(INSERT_SQL, (eb_id, s["ifsc_code"], s["bank_acc_no"], s["bank_name"],
                                   s["bank_branch_name"], s["is_verified"],
                                   s["updated_by"], s["updated_time"]))
            new_ids.append(c.lastrowid)
        for _, _, _, rows in replaces:
            for t in rows:
                c.execute("UPDATE sls.hrms_ed_bank_details SET active = 0 "
                          "WHERE tbl_hrms_ed_bank_detail_id = %s", (t["id"],))
                deactivated.append(t["id"])
        for _, hit, s in namefills:
            c.execute("UPDATE sls.hrms_ed_bank_details "
                      "SET bank_name = %s, bank_branch_name = %s "
                      "WHERE tbl_hrms_ed_bank_detail_id = %s",
                      (s["bank_name"] or hit["bank_name"],
                       s["bank_branch_name"] or hit["bank_branch_name"], hit["id"]))
            filled.append(hit["id"])
        cn.commit()
    except Exception:
        cn.rollback()
        raise

    print(f"\ninserted {len(new_ids)}, deactivated {len(deactivated)}, namefilled {len(filled)}")
    print("-- rollback:")
    if new_ids:
        print(f"DELETE FROM sls.hrms_ed_bank_details WHERE tbl_hrms_ed_bank_detail_id IN "
              f"({','.join(map(str, new_ids))});")
    if deactivated:
        print(f"UPDATE sls.hrms_ed_bank_details SET active = 1 WHERE tbl_hrms_ed_bank_detail_id IN "
              f"({','.join(map(str, deactivated))});")
    if filled:
        print(f"-- namefilled ids (bank_name/branch were blank before): "
              f"{','.join(map(str, filled))}")


if __name__ == "__main__":
    main()
