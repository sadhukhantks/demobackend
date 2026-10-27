"""
Migrate the Holiday Master + holiday transactions from legacy `vowsls` into `sls`.

Scope confirmed by the user (2026-08-15):
  * vowsls.holiday_master                 573 rows -> sls.holiday_master
  * vowsls.tbl_hrms_holiday_transactions  is_active=1 only (23,428 of 153,059)
                                                   -> sls.hrms_holiday_transactions

Both sls targets are EMPTY, so every legacy PK is preserved 1:1 (holiday_id = legacy id,
holiday_tran_id = legacy holiday_tran_id). No offsets needed — that also makes the rollback
a plain DELETE of everything.

KEY DECISIONS
-------------
1. Branch. Legacy holiday_master is COMPANY-scoped (company_id, branch_id NULL on 517 of 573);
   sls.holiday_master is BRANCH-scoped — the list query INNER JOINs branch_mst and filters on
   b.co_id, so a branchless row would simply never appear in the portal. `vowsls_to_sls_transfer._map_holiday_branch`
   resolves company_id -> branch_id as: the company's branch holding the most sls employees,
   tie-broken by branch_name='FACTORY' then lowest branch_id. A legacy row that already names a
   branch keeps it (co 1 -> 4, co 2 -> 29/2), the derivation only fills the NULLs.

2. Spell. Legacy `spell` is free text ('A1' | 'B1' | 'C'); sls uses spell_id -> spell_mst,
   whose branch comes from shift_mst.branch_id. Only branches 4, 29 and 87 have spells defined
   — every legacy row carrying a spell belongs to branch 4 or 29, so all 40 resolve. Anything
   unmatched (or blank '') lands as NULL, which the UI renders as an empty Spell cell.

3. Status. Legacy `status` is NULL on all 573 rows. A holiday that has holiday transactions was
   processed, so it maps to 3 (Approved) — the portal then blocks editing it and shows its
   processed_count; everything else lands as 1 (Open). 53 holidays qualify.

4. holiday_type. NULL (502 rows) and the junk value 0 (15 rows) both become 1 = Occasional;
   the backend only accepts 1/2/3 and would otherwise reject an edit of a migrated row.

5. Employee remap. Transaction eb_ids go through `vowsls_to_sls_transfer._map_pay_eb` (see the payscheme migration)
   — all 153,059 rows resolve. That map is many-to-one (the co1/co106 doubles), so active rows
   are deduped on (holiday_id, mapped eb) keeping the lowest legacy holiday_tran_id. 12 rows drop
   that way: 23,428 legacy active -> 23,416 inserted. Without the dedupe those employees would be
   credited holiday hours twice, which feeds the wages process.

6. created_by is a legacy varchar username ('EDP', 'NVPL005', ...) with no user-id equivalent in
   sls, so updated_by is left NULL. updated_date_time carries the legacy timestamp — for the
   transactions, whose own auto_date_time_insert is NULL on every active row, it carries their
   holiday's timestamp.

ROLLBACK:
  DELETE FROM sls.hrms_holiday_transactions;   -- table was empty before this migration
  DELETE FROM sls.holiday_master;              -- table was empty before this migration
  DROP TABLE vowsls_to_sls_transfer._map_holiday_branch;
  DELETE FROM vowsls_to_sls_transfer.migration_log WHERE step = 'vowsls_holiday';

Usage:
  python run_migrate_vowsls_holiday_to_sls.py            # dry run (builds the branch map, counts only)
  python run_migrate_vowsls_holiday_to_sls.py --commit   # apply
  python run_migrate_vowsls_holiday_to_sls.py --verify   # re-run the checks against what landed
"""
import json
import sys
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
VERIFY_ONLY = "--verify" in sys.argv

REPO = Path(__file__).resolve().parents[2]

ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, connect_timeout=30, read_timeout=1800,
                       write_timeout=1800)
cur = conn.cursor()
STATS: dict[str, object] = {}

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252


def log(msg):
    print(msg, flush=True)


def scalar(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def rows(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


# ── 1. company -> branch map ────────────────────────────────────────────────
# Built in both modes: every count below joins through it.

def build_branch_map():
    log("\n[1] company -> branch map")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_holiday_branch (
            company_id  INT NOT NULL PRIMARY KEY,
            branch_id   INT NOT NULL,
            branch_name VARCHAR(255),
            emp_count   INT,
            n_branches  INT
        ) ENGINE=InnoDB""")
    conn.commit()

    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_holiday_branch") == 0:
        # dominant branch = most employees; ties go to FACTORY, then lowest branch_id
        cur.execute("""
            INSERT INTO vowsls_to_sls_transfer._map_holiday_branch (company_id, branch_id, branch_name,
                                                 emp_count, n_branches)
            SELECT c.company_id, b.branch_id, b.branch_name, b.emp_count, c.n_branches
            FROM (
                SELECT DISTINCT h.company_id,
                       (SELECT COUNT(*) FROM sls.branch_mst x WHERE x.co_id = h.company_id)
                           AS n_branches
                FROM vowsls.holiday_master h
            ) c
            JOIN (
                SELECT b.branch_id, b.co_id, b.branch_name,
                       (SELECT COUNT(*) FROM sls.hrms_ed_personal_details p
                         WHERE p.branch_id = b.branch_id) AS emp_count
                FROM sls.branch_mst b
            ) b ON b.co_id = c.company_id
            WHERE b.branch_id = (
                SELECT b2.branch_id FROM sls.branch_mst b2
                WHERE b2.co_id = c.company_id
                ORDER BY (SELECT COUNT(*) FROM sls.hrms_ed_personal_details p2
                           WHERE p2.branch_id = b2.branch_id) DESC,
                         (b2.branch_name = 'FACTORY') DESC,
                         b2.branch_id ASC
                LIMIT 1)""")
        conn.commit()

    for r in rows("""SELECT company_id, branch_id, branch_name, emp_count, n_branches
                     FROM vowsls_to_sls_transfer._map_holiday_branch ORDER BY company_id"""):
        log(f"    co {r[0]:>4} -> branch {r[1]:>3} ({r[2]}, {r[3]} emp, "
            f"{r[4]} branch(es) in company)")

    unmapped = scalar("""SELECT COUNT(*) FROM vowsls.holiday_master h
                         WHERE COALESCE(h.branch_id, 0) = 0
                           AND h.company_id NOT IN (SELECT company_id FROM vowsls_to_sls_transfer._map_holiday_branch)""")
    STATS["branch_map_rows"] = scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_holiday_branch")
    STATS["unmappable_holidays"] = unmapped
    if unmapped:
        log(f"    !! {unmapped} holiday rows have a company with no sls branch — they will be SKIPPED")


# ── 2. holiday_master ───────────────────────────────────────────────────────
# A legacy branch_id is trusted only when it actually exists in sls.branch_mst
# under the same company (co 2 row 523 carries branch 2, which is co 1's HO in
# vowsls but does not belong to co 2 in sls — that falls back to the map).

HOLIDAY_SELECT = """
    SELECT h.id                                   AS holiday_id,
           COALESCE(hb.branch_id, mb.branch_id)   AS branch_id,
           h.holiday,
           h.holiday_date,
           COALESCE(NULLIF(h.holiday_type, 0), 1) AS holiday_type,
           h.period_start_date,
           h.period_end_date,
           (SELECT s.spell_id FROM sls.spell_mst s
              JOIN sls.shift_mst sh ON sh.shift_id = s.shift_id
             WHERE sh.branch_id = COALESCE(hb.branch_id, mb.branch_id)
               AND s.spell_code = NULLIF(TRIM(h.spell), '')
             ORDER BY s.spell_id LIMIT 1)         AS spell_id,
           NULL                                   AS updated_by,
           h.auto_datetime_insert                 AS updated_date_time,
           CASE WHEN EXISTS (SELECT 1 FROM vowsls.tbl_hrms_holiday_transactions t
                              WHERE t.holiday_id = h.id)
                THEN 3 ELSE 1 END                 AS status
    FROM vowsls.holiday_master h
    LEFT JOIN sls.branch_mst hb ON hb.branch_id = h.branch_id AND hb.co_id = h.company_id
    LEFT JOIN vowsls_to_sls_transfer._map_holiday_branch mb ON mb.company_id = h.company_id
    WHERE COALESCE(hb.branch_id, mb.branch_id) IS NOT NULL
"""


def migrate_holidays():
    log("\n[2] holiday_master")
    eligible = scalar(f"SELECT COUNT(*) FROM ({HOLIDAY_SELECT}) x")
    total = scalar("SELECT COUNT(*) FROM vowsls.holiday_master")
    log(f"    legacy rows {total}, eligible {eligible}")

    for r in rows(f"""SELECT status, COUNT(*) FROM ({HOLIDAY_SELECT}) x GROUP BY status"""):
        log(f"    status {r[0]} -> {r[1]} rows")
    resolved = scalar(f"SELECT COUNT(*) FROM ({HOLIDAY_SELECT}) x WHERE spell_id IS NOT NULL")
    carrying = scalar("SELECT COUNT(*) FROM vowsls.holiday_master WHERE TRIM(COALESCE(spell,'')) <> ''")
    log(f"    spell_id resolved on {resolved} of {carrying} rows carrying a spell")

    STATS["holiday_eligible"] = eligible
    if not COMMIT:
        log("    [dry] nothing written")
        return

    cur.execute(f"""
        INSERT IGNORE INTO sls.holiday_master
            (holiday_id, branch_id, holiday, holiday_date, holiday_type,
             period_start_date, period_end_date, spell_id, updated_by,
             updated_date_time, status)
        {HOLIDAY_SELECT}""")
    n = cur.rowcount
    conn.commit()
    STATS["holiday_inserted"] = n
    log(f"    inserted {n}")


# ── 3. hrms_holiday_transactions ────────────────────────────────────────────
# Active rows only, deduped on (holiday_id, mapped eb) keeping the lowest legacy
# tran id — vowsls_to_sls_transfer._map_pay_eb is many-to-one for the co1/co106 doubles.

TRANS_SELECT = """
    SELECT t.holiday_tran_id,
           t.holiday_id,
           m.new_eb_id                AS eb_id,
           t.holiday_hours,
           t.eligibility_date,
           hm.branch_id               AS branch_id,
           NULL                       AS updated_by,
           hm.updated_date_time,
           1                          AS is_active
    FROM vowsls.tbl_hrms_holiday_transactions t
    JOIN vowsls_to_sls_transfer._map_pay_eb m   ON m.legacy_eb_id = t.eb_id AND m.new_eb_id IS NOT NULL
    JOIN {holiday_src} hm ON hm.holiday_id = t.holiday_id
    WHERE t.is_active = 1
      AND t.holiday_tran_id = (
          SELECT MIN(t2.holiday_tran_id)
          FROM vowsls.tbl_hrms_holiday_transactions t2
          JOIN vowsls_to_sls_transfer._map_pay_eb m2 ON m2.legacy_eb_id = t2.eb_id
          WHERE t2.is_active = 1
            AND t2.holiday_id = t.holiday_id
            AND m2.new_eb_id = m.new_eb_id)
"""


def migrate_transactions():
    log("\n[3] hrms_holiday_transactions (active only)")
    legacy_active = scalar("""SELECT COUNT(*) FROM vowsls.tbl_hrms_holiday_transactions
                              WHERE is_active = 1""")
    log(f"    legacy active {legacy_active}")

    # Dry runs resolve the holiday's branch from the same SELECT step 2 would insert,
    # so the count is real before anything is written.
    select = TRANS_SELECT.format(
        holiday_src="sls.holiday_master" if COMMIT else f"({HOLIDAY_SELECT})")

    eligible = scalar(f"SELECT COUNT(*) FROM ({select}) x")
    log(f"    eligible after eb-remap + dedupe {eligible} (dropped {legacy_active - eligible})")
    STATS["trans_eligible"] = eligible
    if not COMMIT:
        log("    [dry] nothing written")
        return

    cur.execute(f"""
        INSERT IGNORE INTO sls.hrms_holiday_transactions
            (holiday_tran_id, holiday_id, eb_id, holiday_hours, eligibility_date,
             branch_id, updated_by, updated_date_time, is_active)
        {select}""")
    n = cur.rowcount
    conn.commit()
    STATS["trans_inserted"] = n
    log(f"    inserted {n}")


# ── 4. verification ─────────────────────────────────────────────────────────

def verify():
    log("\n[4] verification")
    checks = []

    hm = scalar("SELECT COUNT(*) FROM sls.holiday_master")
    checks.append(("holiday_master rows", hm, 573 - STATS.get("unmappable_holidays", 0)))

    orphan_branch = scalar("""SELECT COUNT(*) FROM sls.holiday_master h
                              LEFT JOIN sls.branch_mst b ON b.branch_id = h.branch_id
                              WHERE b.branch_id IS NULL""")
    checks.append(("holidays with an unknown branch", orphan_branch, 0))

    bad_type = scalar("SELECT COUNT(*) FROM sls.holiday_master WHERE holiday_type NOT IN (1,2,3)")
    checks.append(("holidays with an invalid holiday_type", bad_type, 0))

    bad_spell = scalar("""SELECT COUNT(*) FROM sls.holiday_master h
                          LEFT JOIN sls.spell_mst s ON s.spell_id = h.spell_id
                          WHERE h.spell_id IS NOT NULL AND s.spell_id IS NULL""")
    checks.append(("holidays pointing at a missing spell", bad_spell, 0))

    ht = scalar("SELECT COUNT(*) FROM sls.hrms_holiday_transactions")
    checks.append(("transaction rows", ht, STATS.get("trans_eligible")))

    dupes = scalar("""SELECT COUNT(*) FROM (
                        SELECT holiday_id, eb_id FROM sls.hrms_holiday_transactions
                        WHERE is_active = 1 GROUP BY holiday_id, eb_id HAVING COUNT(*) > 1) x""")
    checks.append(("employees credited twice for one holiday", dupes, 0))

    orphan_eb = scalar("""SELECT COUNT(*) FROM sls.hrms_holiday_transactions t
                          LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id = t.eb_id
                          WHERE p.eb_id IS NULL""")
    checks.append(("transactions for an unknown employee", orphan_eb, 0))

    orphan_hol = scalar("""SELECT COUNT(*) FROM sls.hrms_holiday_transactions t
                           LEFT JOIN sls.holiday_master h ON h.holiday_id = t.holiday_id
                           WHERE h.holiday_id IS NULL""")
    checks.append(("transactions for an unknown holiday", orphan_hol, 0))

    approved_no_trans = scalar("""SELECT COUNT(*) FROM sls.holiday_master h
                                  WHERE h.status = 3 AND NOT EXISTS (
                                      SELECT 1 FROM sls.hrms_holiday_transactions t
                                      WHERE t.holiday_id = h.holiday_id)""")
    checks.append(("approved holidays with no transactions "
                   "(legacy runs that were fully deactivated)", approved_no_trans, None))

    ok = True
    for label, got, want in checks:
        if want is None:
            log(f"    ~  {label}: {got}")
        elif got == want:
            log(f"    OK {label}: {got}")
        else:
            ok = False
            log(f"    !! {label}: {got} (expected {want})")

    log("\n    hours by branch (what the wages process will see):")
    for r in rows("""SELECT t.branch_id, b.branch_name, COUNT(*) rows_,
                            COUNT(DISTINCT t.eb_id) ebs, SUM(t.holiday_hours) hours
                     FROM sls.hrms_holiday_transactions t
                     JOIN sls.branch_mst b ON b.branch_id = t.branch_id
                     WHERE t.is_active = 1 GROUP BY 1,2 ORDER BY 3 DESC"""):
        log(f"      branch {r[0]:>3} {r[1]:<10} {r[2]:>7} rows  {r[3]:>6} employees  {r[4]:>10} hours")

    STATS["verified"] = ok
    return ok


# ── main ────────────────────────────────────────────────────────────────────

log(f"vowsls -> sls holiday migration  ({'COMMIT' if COMMIT else 'VERIFY' if VERIFY_ONLY else 'DRY RUN'})")

if VERIFY_ONLY:
    STATS["trans_eligible"] = None
    STATS["unmappable_holidays"] = 0
    verify()
else:
    build_branch_map()
    migrate_holidays()
    migrate_transactions()
    if COMMIT:
        verify()
        cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                       VALUES ('vowsls_holiday', 'committed', NOW(), NOW(), %s)""",
                    (json.dumps(STATS, default=str),))
        conn.commit()
        log("\nlogged to vowsls_to_sls_transfer.migration_log step 'vowsls_holiday'")
    else:
        log("\nDry run complete — nothing written. Re-run with --commit to apply.")

conn.close()
