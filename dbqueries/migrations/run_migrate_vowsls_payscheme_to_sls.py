"""
Migrate the full pay-scheme + payroll chain from legacy `vowsls` into `sls`.

Scope confirmed by the user (2026-08-14):
  * scheme config chain  — tbl_pay_scheme, tbl_pay_scheme_details, tbl_payscheme_master,
                           tbl_pay_employee_payscheme (CURRENT rows only), employee_payscheme,
                           tbl_pay_customer_employee_payscheme, processed_payscheme,
                           payscheme_parameter_category, tbl_pay_wages_mode, tbl_pay_generic
  * payroll history      — tbl_pay_period (5,731), tbl_pay_employee_structure (1.56M),
                           tbl_pay_components_custom (9.3M), tbl_pay_employee_payroll (26.0M)

KEY DECISIONS
-------------
1. eb_id remap. sls employees came from four earlier HRMS migrations; legacy eb_ids are NOT
   universally preserved (the co1 -> co106 remap moved ~4.4k employees to new eb_ids, and
   ~1.9k people exist TWICE in vowsls — once under co 1 and once under co 106 — but only once
   in sls). `vowsls_to_sls_transfer._map_pay_eb` resolves every legacy eb with this priority:
       r1_hrms_map    existing vowsls_to_sls_transfer._map_hrms_eb_smallco row              (authoritative)
       r2_code_branch trimmed emp_code + branch_id, unique hit
       r3_code_br_nam trimmed emp_code + branch_id + first name          (ties)
       r4_code_global trimmed emp_code, globally unique hit
       r5_identity    same eb_id present in sls AND first name matches
   `merged = 1` flags a new_eb_id claimed by more than one legacy eb (the co1/co106 doubles).
   Unresolved legacy ebs get new_eb_id = NULL and every pay row referencing them is SKIPPED.

2. Scheme ids cannot be preserved — sls.pay_scheme_master already holds 16 hand-recreated
   schemes (2026-07-27). `vowsls_to_sls_transfer._map_pay_scheme` maps the 13 legacy schemes whose CODE **and**
   company match an existing sls scheme to that existing payscheme_id (action='reuse', their
   hand-made pay_scheme_details are left untouched); every other legacy scheme is inserted
   with payscheme_id = legacy ID + SCHEME_OFFSET (action='new').

3. All other target PKs are legacy PK + a fixed offset. That makes the whole migration
   deterministic, restartable (INSERT IGNORE re-runs skip what landed), and trivially
   reversible (DELETE WHERE <pk> > offset). Offsets are chosen above every current sls max id.

4. AMOUNT precision. sls.pay_employee_payroll.AMOUNT and pay_employee_structure.AMOUNT are
   `float`; legacy is double(18,2)/decimal(16,6). float32 cannot hold the paise of the 21,298
   payroll + 898 structure rows above 167,772.16, so step `widen_amounts` ALTERs both columns
   to the legacy types BEFORE loading (both tables are still tiny). The ORM declares Float,
   which SQLAlchemy still returns as Python float, so runtime behaviour is unchanged.

ROLLBACK (in this order):
  DELETE FROM sls.pay_employee_payroll        WHERE ID > 100000000;
  DELETE FROM sls.pay_components_custom       WHERE ID > 100000000;
  DELETE FROM sls.pay_employee_structure      WHERE ID > 10000000;
  DELETE FROM sls.pay_generic                 WHERE id > 1000;
  DELETE FROM sls.pay_customer_employee_payscheme WHERE ID > 1000;
  DELETE FROM sls.pay_employee_payscheme      WHERE emp_payscheme_id > 10000;
  DELETE FROM sls.pay_period                  WHERE ID > 100000;
  DELETE FROM sls.pay_processed_payscheme     WHERE processed_payscheme_id > 1000;
  DELETE FROM sls.pay_scheme_parameter_category WHERE ppcid <= 5;
  DELETE FROM sls.pay_scheme_details          WHERE ID > 100000;
  DELETE FROM sls.pay_scheme_master           WHERE payscheme_id > 300;
  DELETE FROM sls.pay_wages_mode              WHERE ID = 3;
  DROP TABLE vowsls_to_sls_transfer._map_pay_period, vowsls_to_sls_transfer._map_pay_scheme_master, vowsls_to_sls_transfer._map_pay_scheme, vowsls_to_sls_transfer._map_pay_eb;
  -- NOTE: does NOT undo the AMOUNT column widening (harmless to leave).

Usage:
  # Dry run writes NOTHING to the business tables, but it does build the four sls._map_pay_*
  # scratch tables — every dry-run count joins through them. Drop those tables to rebuild.
  python run_migrate_vowsls_payscheme_to_sls.py                  # dry run (counts only)
  python run_migrate_vowsls_payscheme_to_sls.py --commit         # apply everything
  python run_migrate_vowsls_payscheme_to_sls.py --commit --only maps,config
  python run_migrate_vowsls_payscheme_to_sls.py --commit --only payroll   # resume the big copy
  python run_migrate_vowsls_payscheme_to_sls.py --export-maps    # rewrite the CSVs from the map tables

Phase groups for --only: maps | config | payroll | all (default)
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
EXPORT_ONLY = "--export-maps" in sys.argv
PHASES = "all"
if "--only" in sys.argv:
    PHASES = sys.argv[sys.argv.index("--only") + 1]

REPO = Path(__file__).resolve().parents[2]
MAP_DIR = Path(__file__).resolve().parent / "maps"

ENV = {}
for line in (REPO / "env" / "database.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

# target PK = legacy PK + offset; every offset sits above the current sls max id
SCHEME_OFFSET = 1_000          # sls max payscheme_id 17
SCHEME_MASTER_OFFSET = 300     # tbl_payscheme_master 1..3 -> 301..303
SCHEME_DETAIL_OFFSET = 100_000  # sls max 2,895
PERIOD_OFFSET = 100_000        # sls max 18
EPS_OFFSET = 100_000           # sls max emp_payscheme_id 85
EMPSCH_OFFSET = 10_000         # employee_payscheme 1..1101 -> 10,001..11,101
PROCESSED_OFFSET = 1_000       # sls empty
CUSTOMER_EPS_OFFSET = 1_000    # sls empty
GENERIC_OFFSET = 1_000         # sls empty
STRUCT_OFFSET = 10_000_000     # sls max 2,234 / legacy max 2,119,752
CUSTOM_OFFSET = 100_000_000    # sls max 1,532 / legacy max 10,389,261
PAYROLL_OFFSET = 100_000_000   # sls max 10,647 / legacy max 26,188,647

CHUNK = 500_000

conn = pymysql.connect(host=ENV["DATABASE_HOST"], user=ENV["DATABASE_USER"],
                       password=ENV["DATABASE_PASSWORD"], port=int(ENV["DATABASE_PORT"]),
                       autocommit=False, connect_timeout=30, read_timeout=3600, write_timeout=3600)
cur = conn.cursor()
STATS: dict[str, object] = {}


sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252


def log(msg):
    print(msg, flush=True)


def one(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()


def scalar(sql, args=None):
    return one(sql, args)[0]


def want(phase):
    return PHASES in ("all", phase)


def run(label, insert_sql, count_sql):
    """Small step: report the eligible source count, insert only under --commit."""
    eligible = scalar(count_sql)
    if not COMMIT:
        log(f"  [dry] {label}: {eligible} eligible rows")
        STATS[label] = eligible
        return 0
    cur.execute(insert_sql)
    n = cur.rowcount
    conn.commit()
    log(f"  {label}: inserted {n} (eligible {eligible})")
    STATS[label] = n
    return n


def run_chunked(label, insert_sql_tpl, count_sql, id_col, max_id):
    """Big step: INSERT IGNORE in legacy-PK ranges, committing per chunk so it can resume."""
    eligible = scalar(count_sql)
    if not COMMIT:
        log(f"  [dry] {label}: {eligible} eligible rows (would run in "
            f"{max_id // CHUNK + 1} chunks)")
        STATS[label] = eligible
        return 0
    total, lo = 0, 0
    while lo <= max_id:
        hi = lo + CHUNK
        cur.execute(insert_sql_tpl.replace("{RANGE}", f"{id_col} >= {lo} AND {id_col} < {hi}"))
        total += cur.rowcount
        conn.commit()
        lo = hi
        log(f"    {label}: {total:>9,} rows  (through legacy id {min(hi, max_id):,})")
    log(f"  {label}: inserted {total} (eligible {eligible})")
    if total != eligible:
        log(f"  !! {label}: {eligible - total} eligible rows did NOT land "
            f"(pre-existing PKs from an earlier run, or IGNOREd errors) — verify before trusting")
    STATS[label] = total
    return total


# ════════════════════════════════════════════════════════════ 1. eb map
def build_eb_map():
    # The _map_* tables are always written, dry run included: they hold no business data, and
    # every dry-run count below joins through them.
    log("\n── vowsls_to_sls_transfer._map_pay_eb ──")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_pay_eb (
            legacy_eb_id     int         NOT NULL PRIMARY KEY,
            new_eb_id        int         NULL,
            legacy_co_id     int         NULL,
            legacy_branch_id int         NULL,
            emp_code         varchar(50) NULL,
            match_rule       varchar(20) NOT NULL,
            merged           tinyint(1)  NOT NULL DEFAULT 0,
            KEY idx_map_pay_eb_new (new_eb_id)
        ) ENGINE=InnoDB""")
    conn.commit()
    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_eb"):
        log("  already built — reusing (drop the table to rebuild)")
        return

    cur.execute("""
        SELECT p.eb_id, p.company_id, UPPER(TRIM(COALESCE(p.first_name,''))),
               o.emp_code, o.branch_id
          FROM vowsls.tbl_hrms_ed_personal_details p
          LEFT JOIN vowsls.tbl_hrms_ed_official_details o
                 ON o.tbl_hrms_ed_official_detail_id = (
                      SELECT o2.tbl_hrms_ed_official_detail_id
                        FROM vowsls.tbl_hrms_ed_official_details o2
                       WHERE o2.eb_id = p.eb_id
                       ORDER BY o2.is_active DESC, o2.tbl_hrms_ed_official_detail_id DESC LIMIT 1)""")
    legacy = [dict(eb=r[0], co=r[1], fn=r[2], code=(r[3] or "").strip().upper(), br=r[4])
              for r in cur.fetchall()]

    cur.execute("""
        SELECT p.eb_id, UPPER(TRIM(COALESCE(p.first_name,''))), o.emp_code, o.branch_id
          FROM sls.hrms_ed_personal_details p
          LEFT JOIN sls.hrms_ed_official_details o
                 ON o.tbl_hrms_ed_official_detail_id = (
                      SELECT o2.tbl_hrms_ed_official_detail_id
                        FROM sls.hrms_ed_official_details o2
                       WHERE o2.eb_id = p.eb_id
                       ORDER BY o2.active DESC, o2.tbl_hrms_ed_official_detail_id DESC LIMIT 1)""")
    new = [dict(eb=r[0], fn=r[1], code=(r[2] or "").strip().upper(), br=r[3]) for r in cur.fetchall()]

    cur.execute("SELECT legacy_eb_id, new_eb_id FROM vowsls_to_sls_transfer._map_hrms_eb_smallco")
    hrms_map = dict(cur.fetchall())

    by_eb = {n["eb"]: n for n in new}
    by_code_br, by_code = defaultdict(list), defaultdict(list)
    for n in new:
        if n["code"]:
            by_code_br[(n["code"], n["br"])].append(n)
            by_code[n["code"]].append(n)

    rules, rows = Counter(), []
    for l in legacy:
        eb, target, rule = l["eb"], None, "unresolved"
        if eb in hrms_map:
            target, rule = hrms_map[eb], "r1_hrms_map"
        else:
            cands = by_code_br.get((l["code"], l["br"]), []) if l["code"] else []
            if len(cands) == 1:
                target, rule = cands[0]["eb"], "r2_code_branch"
            elif len(cands) > 1:
                named = [c for c in cands if c["fn"] == l["fn"]]
                if len(named) == 1:
                    target, rule = named[0]["eb"], "r3_code_br_nam"
            if target is None:
                cands = by_code.get(l["code"], []) if l["code"] else []
                if len(cands) == 1:
                    target, rule = cands[0]["eb"], "r4_code_global"
            if target is None and eb in by_eb and by_eb[eb]["fn"] == l["fn"]:
                target, rule = eb, "r5_identity"
        rules[rule] += 1
        rows.append((eb, target, l["co"], l["br"], l["code"] or None, rule))

    claimed = Counter(t for _, t, *_ in rows if t is not None)
    rows = [r + (1 if r[1] is not None and claimed[r[1]] > 1 else 0,) for r in rows]

    log(f"  legacy employees: {len(rows)}   rules: {dict(sorted(rules.items()))}")
    log(f"  distinct sls targets: {len(claimed)}   merged legacy duplicates: "
        f"{sum(r[6] for r in rows)}")
    cur.executemany("""INSERT INTO vowsls_to_sls_transfer._map_pay_eb
                       (legacy_eb_id, new_eb_id, legacy_co_id, legacy_branch_id, emp_code,
                        match_rule, merged) VALUES (%s,%s,%s,%s,%s,%s,%s)""", rows)
    conn.commit()
    log(f"  written: {len(rows)} rows, {rules['unresolved']} unresolved")
    STATS["_map_pay_eb"] = {"rows": len(rows), "rules": dict(rules)}


# ════════════════════════════════════════════════════════════ 2. scheme maps
def build_scheme_maps():
    log("\n── vowsls_to_sls_transfer._map_pay_scheme / _map_pay_scheme_master / _map_pay_period ──")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_pay_scheme (
            legacy_scheme_id int         NOT NULL PRIMARY KEY,
            new_payscheme_id bigint      NOT NULL,
            legacy_code      varchar(50) NULL,
            legacy_co_id     int         NULL,
            action           varchar(10) NOT NULL
        ) ENGINE=InnoDB""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_pay_scheme_master (
            legacy_payscheme_id bigint      NOT NULL PRIMARY KEY,
            new_payscheme_id    bigint      NOT NULL,
            legacy_code         varchar(50) NULL,
            action              varchar(10) NOT NULL
        ) ENGINE=InnoDB""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vowsls_to_sls_transfer._map_pay_period (
            legacy_period_id int NOT NULL PRIMARY KEY,
            new_period_id    int NOT NULL
        ) ENGINE=InnoDB""")
    conn.commit()

    # scheme map — reuse an existing sls scheme when CODE *and* company match, else fresh id
    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_scheme"):
        log("  _map_pay_scheme: already built — reusing")
    else:
        reuse = scalar("""SELECT COUNT(*) FROM vowsls.tbl_pay_scheme s
                            JOIN sls.pay_scheme_master m
                              ON UPPER(TRIM(m.payscheme_code)) = UPPER(TRIM(s.CODE))
                             AND m.co_id = s.BUSINESSUNIT_ID""")
        total = scalar("SELECT COUNT(*) FROM vowsls.tbl_pay_scheme")
        cur.execute(f"""
            INSERT INTO vowsls_to_sls_transfer._map_pay_scheme
                   (legacy_scheme_id, new_payscheme_id, legacy_code, legacy_co_id, action)
            SELECT s.ID,
                   COALESCE(m.payscheme_id, s.ID + {SCHEME_OFFSET}),
                   s.CODE, s.BUSINESSUNIT_ID,
                   IF(m.payscheme_id IS NULL, 'new', 'reuse')
              FROM vowsls.tbl_pay_scheme s
              LEFT JOIN sls.pay_scheme_master m
                     ON UPPER(TRIM(m.payscheme_code)) = UPPER(TRIM(s.CODE))
                    AND m.co_id = s.BUSINESSUNIT_ID""")
        conn.commit()
        log(f"  _map_pay_scheme: {total} legacy schemes — {reuse} reuse, {total - reuse} new")

    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_scheme_master"):
        log("  _map_pay_scheme_master: already built — reusing")
    else:
        cur.execute(f"""
            INSERT INTO vowsls_to_sls_transfer._map_pay_scheme_master
                   (legacy_payscheme_id, new_payscheme_id, legacy_code, action)
            SELECT payscheme_id, payscheme_id + {SCHEME_MASTER_OFFSET}, payscheme_code, 'new'
              FROM vowsls.tbl_payscheme_master""")
        conn.commit()
        log(f"  _map_pay_scheme_master: {cur.rowcount} rows")

    if scalar("SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_period"):
        log("  _map_pay_period: already built — reusing")
    else:
        skipped = scalar("""SELECT COUNT(*) FROM vowsls.tbl_pay_period p
                             LEFT JOIN vowsls.tbl_pay_scheme s ON s.ID = p.PAYSCHEME_ID
                            WHERE s.ID IS NULL""")
        cur.execute(f"""
            INSERT INTO vowsls_to_sls_transfer._map_pay_period (legacy_period_id, new_period_id)
            SELECT p.ID, p.ID + {PERIOD_OFFSET}
              FROM vowsls.tbl_pay_period p
              JOIN vowsls_to_sls_transfer._map_pay_scheme m ON m.legacy_scheme_id = p.PAYSCHEME_ID""")
        conn.commit()
        log(f"  _map_pay_period: {cur.rowcount} rows ({skipped} skipped — scheme not in tbl_pay_scheme)")


# ════════════════════════════════════════════════════════════ 3. amount precision
def widen_amounts():
    log("\n── widen AMOUNT columns to legacy precision ──")
    for table, col, typ in [("pay_employee_payroll", "AMOUNT", "decimal(18,2)"),
                            ("pay_employee_structure", "AMOUNT", "decimal(16,6)")]:
        current = scalar("""SELECT column_type FROM information_schema.columns
                             WHERE table_schema='sls' AND table_name=%s AND column_name=%s""",
                         (table, col))
        if current == typ:
            log(f"  sls.{table}.{col} already {typ}")
            continue
        if not COMMIT:
            log(f"  [dry] would ALTER sls.{table}.{col}: {current} -> {typ}")
            continue
        cur.execute(f"ALTER TABLE sls.{table} MODIFY COLUMN {col} {typ} NULL")
        conn.commit()
        log(f"  sls.{table}.{col}: {current} -> {typ}")


# ════════════════════════════════════════════════════════════ 4. scheme config
def migrate_config():
    log("\n── scheme configuration ──")

    run("pay_wages_mode",
        """INSERT INTO sls.pay_wages_mode (ID, BUSINESSUNIT_ID, CODE, NAME, DESCRIPTION, STATUS, updated_by)
           SELECT w.ID, NULL, w.CODE, w.NAME, NULLIF(w.DESCRIPTION,''), 1, 0
             FROM vowsls.tbl_pay_wages_mode w
             LEFT JOIN sls.pay_wages_mode t ON t.ID = w.ID
            WHERE t.ID IS NULL""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_wages_mode w
             LEFT JOIN sls.pay_wages_mode t ON t.ID = w.ID WHERE t.ID IS NULL""")

    run("pay_scheme_master (from tbl_pay_scheme)",
        """INSERT INTO sls.pay_scheme_master
                  (payscheme_id, co_id, effective_from, payscheme_code, payscheme_name,
                   record_status, updated_by, wage_type, branch_id)
           SELECT m.new_payscheme_id, s.BUSINESSUNIT_ID, NULL, s.CODE, s.NAME,
                  s.STATUS, 0, s.WAGE_ID, NULL
             FROM vowsls.tbl_pay_scheme s
             JOIN vowsls_to_sls_transfer._map_pay_scheme m ON m.legacy_scheme_id = s.ID AND m.action = 'new'""",
        "SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_scheme WHERE action = 'new'")

    run("pay_scheme_master (from tbl_payscheme_master)",
        """INSERT INTO sls.pay_scheme_master
                  (payscheme_id, co_id, effective_from, payscheme_code, payscheme_name,
                   record_status, updated_by, wage_type, branch_id)
           SELECT m.new_payscheme_id, s.company_id, s.effective_from, s.payscheme_code,
                  s.payscheme_name, s.record_status, 0, s.wage_type, NULL
             FROM vowsls.tbl_payscheme_master s
             JOIN vowsls_to_sls_transfer._map_pay_scheme_master m ON m.legacy_payscheme_id = s.payscheme_id""",
        "SELECT COUNT(*) FROM vowsls.tbl_payscheme_master")

    # only for action='new' schemes — the 13 reused schemes keep their hand-made sls formulas.
    # FK fk_pay_scheme_dtl_component forces the component join.
    run("pay_scheme_details",
        f"""INSERT INTO sls.pay_scheme_details
                   (ID, COMPONENT_ID, FORMULA, PAY_SCHEME_ID, TYPE, STATUS, DEFAULT_VALUE, updated_by)
            SELECT d.ID + {SCHEME_DETAIL_OFFSET}, d.COMPONENT_ID, NULLIF(d.FORMULA,''),
                   m.new_payscheme_id, d.TYPE, d.STATUS, d.DEFAULT_VALUE, 0
              FROM vowsls.tbl_pay_scheme_details d
              JOIN vowsls_to_sls_transfer._map_pay_scheme m ON m.legacy_scheme_id = d.PAY_SCHEME_ID AND m.action = 'new'
              JOIN sls.pay_components c ON c.ID = d.COMPONENT_ID""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_scheme_details d
             JOIN vowsls_to_sls_transfer._map_pay_scheme m ON m.legacy_scheme_id = d.PAY_SCHEME_ID AND m.action = 'new'
             JOIN sls.pay_components c ON c.ID = d.COMPONENT_ID""")

    run("pay_scheme_parameter_category",
        """INSERT INTO sls.pay_scheme_parameter_category (ppcid, category_name, updated_by)
           SELECT c.ppcid, c.category_name, 0
             FROM vowsls.payscheme_parameter_category c
             LEFT JOIN sls.pay_scheme_parameter_category t ON t.ppcid = c.ppcid
            WHERE t.ppcid IS NULL""",
        """SELECT COUNT(*) FROM vowsls.payscheme_parameter_category c
             LEFT JOIN sls.pay_scheme_parameter_category t ON t.ppcid = c.ppcid
            WHERE t.ppcid IS NULL""")

    # is_active=0 rows dropped — sls.pay_processed_payscheme has no is_active column
    run("pay_processed_payscheme",
        f"""INSERT INTO sls.pay_processed_payscheme
                   (processed_payscheme_id, basic_pay, co_id, da, empresi, emprpf, esi, from_date,
                    gross, hra, net, othear, othded, payscheme_id, pf, status_id, to_date,
                    wage_type, updated_by)
            SELECT p.processed_payscheme_id + {PROCESSED_OFFSET}, p.basic_pay, p.company_id, p.da,
                   p.empresi, p.emprpf, p.esi, p.from_date, p.gross, p.hra, p.net, p.othear,
                   p.othded, m.new_payscheme_id, p.pf, p.status, p.to_date, p.wage_type, 0
              FROM vowsls.processed_payscheme p
              JOIN vowsls_to_sls_transfer._map_pay_scheme_master m ON m.legacy_payscheme_id = p.payscheme_id
             WHERE p.is_active = 1""",
        """SELECT COUNT(*) FROM vowsls.processed_payscheme p
             JOIN vowsls_to_sls_transfer._map_pay_scheme_master m ON m.legacy_payscheme_id = p.payscheme_id
            WHERE p.is_active = 1""")

    # Current assignments only (STATUS=1); one row per (sls employee, sls scheme), latest wins.
    # Employees who already carry a live sls-native assignment are skipped entirely (44 of the
    # 45 such employees also appear in the legacy set) — the sls row is the authoritative one,
    # and a second STATUS=1 row would duplicate them in every payroll query.
    # `live` is a derived table because MySQL forbids sub-selecting the INSERT target directly.
    run("pay_employee_payscheme (current)",
        f"""INSERT INTO sls.pay_employee_payscheme
                   (emp_payscheme_id, ID, EMPLOYEEID, PAY_SCHEME_ID, STATUS, updated_by)
            SELECT e.ID + {EPS_OFFSET}, e.ID, eb.new_eb_id, ms.new_payscheme_id, 1, 0
              FROM (SELECT MAX(e2.ID) AS ID
                      FROM vowsls.tbl_pay_employee_payscheme e2
                      JOIN vowsls_to_sls_transfer._map_pay_eb eb2 ON eb2.legacy_eb_id = e2.EMPLOYEEID
                                              AND eb2.new_eb_id IS NOT NULL
                      JOIN vowsls_to_sls_transfer._map_pay_scheme ms2 ON ms2.legacy_scheme_id = e2.PAY_SCHEME_ID
                     WHERE e2.STATUS = 1
                     GROUP BY eb2.new_eb_id, ms2.new_payscheme_id) pick
              JOIN vowsls.tbl_pay_employee_payscheme e ON e.ID = pick.ID
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = e.EMPLOYEEID
              JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = e.PAY_SCHEME_ID
              LEFT JOIN (SELECT DISTINCT EMPLOYEEID FROM sls.pay_employee_payscheme
                          WHERE STATUS = 1) live ON live.EMPLOYEEID = eb.new_eb_id
             WHERE live.EMPLOYEEID IS NULL""",
        """SELECT COUNT(*) FROM (
               SELECT MAX(e2.ID) AS ID
                 FROM vowsls.tbl_pay_employee_payscheme e2
                 JOIN vowsls_to_sls_transfer._map_pay_eb eb2 ON eb2.legacy_eb_id = e2.EMPLOYEEID
                                         AND eb2.new_eb_id IS NOT NULL
                 JOIN vowsls_to_sls_transfer._map_pay_scheme ms2 ON ms2.legacy_scheme_id = e2.PAY_SCHEME_ID
                 LEFT JOIN (SELECT DISTINCT EMPLOYEEID FROM sls.pay_employee_payscheme
                             WHERE STATUS = 1) live ON live.EMPLOYEEID = eb2.new_eb_id
                WHERE e2.STATUS = 1 AND live.EMPLOYEEID IS NULL
                GROUP BY eb2.new_eb_id, ms2.new_payscheme_id) x""")

    run("pay_employee_payscheme (employee_payscheme)",
        f"""INSERT INTO sls.pay_employee_payscheme
                   (emp_payscheme_id, EMPLOYEEID, payscheme_id, co_id, effect_from,
                    record_status, updated_by)
            SELECT e.emp_payscheme_id + {EMPSCH_OFFSET}, eb.new_eb_id, m.new_payscheme_id,
                   e.company_id, e.effect_from, e.record_status, 0
              FROM (SELECT MAX(e2.emp_payscheme_id) AS emp_payscheme_id
                      FROM vowsls.employee_payscheme e2
                      JOIN vowsls_to_sls_transfer._map_pay_eb eb2 ON eb2.legacy_eb_id = e2.emp_id
                                              AND eb2.new_eb_id IS NOT NULL
                      JOIN vowsls_to_sls_transfer._map_pay_scheme_master m2 ON m2.legacy_payscheme_id = e2.payscheme_id
                     GROUP BY eb2.new_eb_id, m2.new_payscheme_id) pick
              JOIN vowsls.employee_payscheme e ON e.emp_payscheme_id = pick.emp_payscheme_id
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = e.emp_id
              JOIN vowsls_to_sls_transfer._map_pay_scheme_master m ON m.legacy_payscheme_id = e.payscheme_id""",
        """SELECT COUNT(*) FROM (
               SELECT MAX(e2.emp_payscheme_id) AS emp_payscheme_id
                 FROM vowsls.employee_payscheme e2
                 JOIN vowsls_to_sls_transfer._map_pay_eb eb2 ON eb2.legacy_eb_id = e2.emp_id
                                         AND eb2.new_eb_id IS NOT NULL
                 JOIN vowsls_to_sls_transfer._map_pay_scheme_master m2 ON m2.legacy_payscheme_id = e2.payscheme_id
                GROUP BY eb2.new_eb_id, m2.new_payscheme_id) x""")

    run("pay_customer_employee_payscheme",
        f"""INSERT IGNORE INTO sls.pay_customer_employee_payscheme
                   (ID, CUSTOMERID, EMPLOYEEID, PAY_SCHEME_ID, STATUS, updated_by)
            SELECT c.ID + {CUSTOMER_EPS_OFFSET}, c.CUSTOMERID, eb.new_eb_id,
                   ms.new_payscheme_id, c.STATUS, 0
              FROM vowsls.tbl_pay_customer_employee_payscheme c
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = c.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
              JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = c.PAY_SCHEME_ID""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_customer_employee_payscheme c
             JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = c.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
             JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = c.PAY_SCHEME_ID""")

    run("pay_generic",
        f"""INSERT INTO sls.pay_generic (id, date_time, component_id, VALUE, employeeId, updated_by)
            SELECT g.id + {GENERIC_OFFSET}, g.date_time, g.component_id, g.VALUE, eb.new_eb_id, 0
              FROM vowsls.tbl_pay_generic g
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = g.employeeId AND eb.new_eb_id IS NOT NULL""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_generic g
             JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = g.employeeId AND eb.new_eb_id IS NOT NULL""")

    run("pay_period",
        f"""INSERT INTO sls.pay_period
                   (ID, FROM_DATE, TO_DATE, PAYSCHEME_ID, STATUS, branch_id, COMPANY_ID, updated_by)
            SELECT mp.new_period_id, p.FROM_DATE, p.TO_DATE, ms.new_payscheme_id, p.STATUS,
                   NULLIF(p.branch_id, 0), p.COMPANY_ID, 0
              FROM vowsls.tbl_pay_period p
              JOIN vowsls_to_sls_transfer._map_pay_period mp ON mp.legacy_period_id = p.ID
              JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = p.PAYSCHEME_ID""",
        "SELECT COUNT(*) FROM vowsls_to_sls_transfer._map_pay_period")


# ════════════════════════════════════════════════════════════ 5. payroll history
def migrate_payroll():
    log("\n── payroll history (chunked) ──")

    max_struct = scalar("SELECT MAX(ID) FROM vowsls.tbl_pay_employee_structure")
    run_chunked(
        "pay_employee_structure",
        f"""INSERT IGNORE INTO sls.pay_employee_structure
                   (ID, EMPLOYEEID, PAYSCHEME_ID, COMPONENT_ID, AMOUNT, STATUS,
                    EFFECTIVE_FROM, ENDS_ON, REMARKS, updated_by)
            SELECT s.ID + {STRUCT_OFFSET}, eb.new_eb_id, ms.new_payscheme_id, s.COMPONENT_ID,
                   s.AMOUNT, s.STATUS, s.EFFECTIVE_FROM, s.ENDS_ON, NULLIF(s.REMARKS,''), 0
              FROM vowsls.tbl_pay_employee_structure s
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = s.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
              JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = s.PAYSCHEME_ID
             WHERE {{RANGE}}""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_employee_structure s
             JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = s.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
             JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = s.PAYSCHEME_ID""",
        "s.ID", max_struct)

    # LEFT JOIN on the period: custom rows carry FROM_DATE/TO_DATE and stay meaningful without a
    # period, so a dangling PAY_PERIOD_ID is nulled rather than dropping the row.
    max_custom = scalar("SELECT MAX(ID) FROM vowsls.tbl_pay_components_custom")
    run_chunked(
        "pay_components_custom",
        f"""INSERT IGNORE INTO sls.pay_components_custom
                   (ID, COMPONENT_ID, VALUE, EMPLOYEEID, STATUS, PAY_PERIOD_ID,
                    FROM_DATE, TO_DATE, updated_by)
            SELECT c.ID + {CUSTOM_OFFSET}, c.COMPONENT_ID, c.VALUE, eb.new_eb_id, c.STATUS,
                   mp.new_period_id, c.FROM_DATE, c.TO_DATE, 0
              FROM vowsls.tbl_pay_components_custom c
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = c.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
              LEFT JOIN vowsls_to_sls_transfer._map_pay_period mp ON mp.legacy_period_id = c.PAY_PERIOD_ID
             WHERE {{RANGE}}""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_components_custom c
             JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = c.EMPLOYEEID AND eb.new_eb_id IS NOT NULL""",
        "c.ID", max_custom)

    # INNER JOIN on the period: a payroll line without a pay period is unusable history.
    max_payroll = scalar("SELECT MAX(ID) FROM vowsls.tbl_pay_employee_payroll")
    run_chunked(
        "pay_employee_payroll",
        f"""INSERT IGNORE INTO sls.pay_employee_payroll
                   (ID, EMPLOYEEID, COMPONENT_ID, PAYPERIOD_ID, AMOUNT, STATUS,
                    BUSINESSUNIT_ID, PAYSCHEME_ID, SOURCE, updated_by)
            SELECT r.ID + {PAYROLL_OFFSET}, eb.new_eb_id, r.COMPONENT_ID, mp.new_period_id,
                   r.AMOUNT, r.STATUS, r.BUSINESSUNIT_ID, ms.new_payscheme_id, r.SOURCE, 0
              FROM vowsls.tbl_pay_employee_payroll r
              JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = r.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
              JOIN vowsls_to_sls_transfer._map_pay_period mp ON mp.legacy_period_id = r.PAYPERIOD_ID
              JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = r.PAYSCHEME_ID
             WHERE {{RANGE}}""",
        """SELECT COUNT(*) FROM vowsls.tbl_pay_employee_payroll r
             JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = r.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
             JOIN vowsls_to_sls_transfer._map_pay_period mp ON mp.legacy_period_id = r.PAYPERIOD_ID
             JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = r.PAYSCHEME_ID""",
        "r.ID", max_payroll)


# ════════════════════════════════════════════════════════════ 6. CSV export
def export_maps():
    log("\n── CSV export ──")
    MAP_DIR.mkdir(exist_ok=True)
    for name, sql in [
        ("vowsls_sls_pay_eb_map", """
            SELECT legacy_eb_id, new_eb_id, legacy_co_id, legacy_branch_id, emp_code,
                   match_rule, merged FROM vowsls_to_sls_transfer._map_pay_eb ORDER BY legacy_eb_id"""),
        ("vowsls_sls_pay_scheme_map", """
            SELECT legacy_scheme_id, new_payscheme_id, legacy_code, legacy_co_id, action
              FROM vowsls_to_sls_transfer._map_pay_scheme ORDER BY legacy_scheme_id"""),
        ("vowsls_sls_pay_scheme_master_map", """
            SELECT legacy_payscheme_id, new_payscheme_id, legacy_code, action
              FROM vowsls_to_sls_transfer._map_pay_scheme_master ORDER BY legacy_payscheme_id"""),
        ("vowsls_sls_pay_period_map", """
            SELECT legacy_period_id, new_period_id FROM vowsls_to_sls_transfer._map_pay_period ORDER BY legacy_period_id"""),
    ]:
        try:
            cur.execute(sql)
        except pymysql.err.ProgrammingError:
            log(f"  {name}: map table missing — skipped")
            continue
        rows = cur.fetchall()
        path = MAP_DIR / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            w.writerows(rows)
        log(f"  {path.relative_to(REPO)}: {len(rows)} rows")


# ════════════════════════════════════════════════════════════ verification
def verify():
    log("\n── verification ──")
    checks = [
        ("orphan scheme in pay_scheme_details", """
            SELECT COUNT(*) FROM sls.pay_scheme_details d
              LEFT JOIN sls.pay_scheme_master m ON m.payscheme_id = d.PAY_SCHEME_ID
             WHERE m.payscheme_id IS NULL"""),
        ("orphan employee in pay_employee_payscheme", """
            SELECT COUNT(*) FROM sls.pay_employee_payscheme e
              LEFT JOIN sls.hrms_ed_personal_details p ON p.eb_id = e.EMPLOYEEID
             WHERE e.EMPLOYEEID IS NOT NULL AND p.eb_id IS NULL"""),
        ("duplicate current scheme per employee", """
            SELECT COUNT(*) FROM (SELECT EMPLOYEEID FROM sls.pay_employee_payscheme
                                   WHERE STATUS = 1 GROUP BY 1 HAVING COUNT(*) > 1) x"""),
        ("orphan period in pay_employee_payroll", """
            SELECT COUNT(*) FROM sls.pay_employee_payroll r
              LEFT JOIN sls.pay_period p ON p.ID = r.PAYPERIOD_ID
             WHERE r.PAYPERIOD_ID IS NOT NULL AND p.ID IS NULL"""),
        ("orphan scheme in pay_period", """
            SELECT COUNT(*) FROM sls.pay_period p
              LEFT JOIN sls.pay_scheme_master m ON m.payscheme_id = p.PAYSCHEME_ID
             WHERE p.PAYSCHEME_ID IS NOT NULL AND m.payscheme_id IS NULL"""),
        # CAST on the legacy side is required: tbl_pay_employee_payroll.AMOUNT is double(18,2),
        # and SUM() over 26M binary doubles accumulates ~14 paise of its own float error.
        # Comparing decimal-to-decimal is the honest check — it reads 0.00 when every row matches.
        ("payroll amount sum drift vs legacy (rupees)", """
            SELECT ABS(
                     (SELECT COALESCE(SUM(AMOUNT),0) FROM sls.pay_employee_payroll WHERE ID > 100000000)
                   - (SELECT COALESCE(SUM(CAST(r.AMOUNT AS DECIMAL(18,2))),0)
                        FROM vowsls.tbl_pay_employee_payroll r
                        JOIN vowsls_to_sls_transfer._map_pay_eb eb ON eb.legacy_eb_id = r.EMPLOYEEID AND eb.new_eb_id IS NOT NULL
                        JOIN vowsls_to_sls_transfer._map_pay_period mp ON mp.legacy_period_id = r.PAYPERIOD_ID
                        JOIN vowsls_to_sls_transfer._map_pay_scheme ms ON ms.legacy_scheme_id = r.PAYSCHEME_ID))"""),
    ]
    for label, sql in checks:
        try:
            log(f"  {label}: {scalar(sql)}")
        except pymysql.err.Error as exc:
            log(f"  {label}: SKIPPED ({exc.args[1][:60]})")


# ════════════════════════════════════════════════════════════ main
if EXPORT_ONLY:
    export_maps()
    sys.exit(0)

log(f"mode: {'COMMIT' if COMMIT else 'DRY RUN (no writes)'}   phases: {PHASES}")

if want("maps"):
    build_eb_map()
    build_scheme_maps()
if want("config"):
    widen_amounts()
    migrate_config()
if want("payroll"):
    migrate_payroll()

if COMMIT:
    export_maps()
    verify()
    cur.execute("""INSERT INTO vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
                   VALUES ('vowsls_payscheme_payroll', 'committed', NOW(), NOW(), %s)""",
                (json.dumps({"phases": PHASES, "stats": STATS}, default=str),))
    conn.commit()
    log("\nlogged to vowsls_to_sls_transfer.migration_log step 'vowsls_payscheme_payroll'")
else:
    log("\nDry run complete — nothing written. Re-run with --commit to apply.")
conn.close()
