"""
Re-point sls pay_scheme_details formulas that still use a LEGACY component CODE which sls renamed.

Background: vowsls.tbl_pay_components had four CODEs shared by two components each. sls enforces unique
CODEs (uq_pay_components_code + payComponent.py duplicate check), so one twin of each pair was renamed with
an 's' suffix during migration:
    C_LTA_          -> 76  C_LTAs_          (156 keeps C_LTA_)
    C_prod_incent_  -> 87  C_prod_incents_  (79 is C_PROD_INCENT_, upper case)
    C_Arrears_      -> 165 C_Arrearss_      (68 is C_ARREARS_, upper case)
    C_CTC_          -> 266 C_CTCs_          (24 keeps C_CTC_)
Formulas were copied verbatim, so a scheme that contains the renamed twin still says the legacy code; the sls
evaluator (payRegister._evaluate_employee_formulas) resolves CODEs globally and case-sensitively, so the token
either points at the OTHER twin (outside the scheme -> component never computed, e.g. NET on 3163) or at
nothing (-> silently 0).

Rule applied per active formula containing the legacy token (exact case):
  * scheme contains only the renamed twin            -> replace token with the renamed code
  * scheme contains only the twin that kept the code -> no change
  * scheme contains both (only C_CTC_ in 1128)        -> choose 266 (the TYPE-0 input); 24 is the TYPE-3 summary
                                                        whose own formula references C_CTC_, which as 24 would be
                                                        a self-reference cycle
  * scheme contains neither                          -> report only
Originals are saved to vowsls_to_sls_transfer._bak_pay_scheme_details_formula_20260909 (one row per changed ID).

Rollback:
    UPDATE sls.pay_scheme_details d JOIN vowsls_to_sls_transfer._bak_pay_scheme_details_formula_20260909 b ON b.ID = d.ID
       SET d.FORMULA = b.FORMULA_BEFORE;

Usage:
    python run_repoint_dedup_component_codes.py            # dry run: prints every planned change
    python run_repoint_dedup_component_codes.py --commit
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pymysql

COMMIT = "--commit" in sys.argv
BAK = "vowsls_to_sls_transfer._bak_pay_scheme_details_formula_20260909"
# legacy token (exact case) -> (renamed twin id, renamed code, other twin id)
PAIRS = {
    "C_LTA_": (76, "C_LTAs_", 156),
    "C_prod_incent_": (87, "C_prod_incents_", 79),
    "C_Arrears_": (165, "C_Arrearss_", 68),
    "C_CTC_": (266, "C_CTCs_", 24),
}

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


# sanity: the renamed twins really carry the suffixed code, the other twins keep a distinct code
for tok, (rid, rcode, oid) in PAIRS.items():
    rows = {r["ID"]: r["CODE"] for r in q("select ID, CODE from sls.pay_components where ID in (%s, %s)", (rid, oid))}
    assert rows.get(rid) == rcode, f"{rid} is {rows.get(rid)!r}, expected {rcode!r}"
    assert rows.get(oid) != rcode, f"{oid} unexpectedly also {rcode!r}"
    log(f"{tok:15s} renamed twin {rid}={rows[rid]}  other twin {oid}={rows.get(oid)}")

# which twins each scheme contains (active detail rows)
contains = defaultdict(set)
for r in q("select distinct PAY_SCHEME_ID s, COMPONENT_ID c from sls.pay_scheme_details where STATUS = 1"):
    contains[r["s"]].add(r["c"])

changes, skipped = [], []
for tok, (rid, rcode, oid) in PAIRS.items():
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])")
    rows = q("""select d.ID, d.PAY_SCHEME_ID s, d.COMPONENT_ID c, d.FORMULA f, m.payscheme_code, m.co_id, m.branch_id
                from sls.pay_scheme_details d left join sls.pay_scheme_master m on m.payscheme_id = d.PAY_SCHEME_ID
                where d.STATUS = 1 and d.FORMULA like %s order by d.PAY_SCHEME_ID, d.ID""", (f"%{tok}%",))
    for r in rows:
        if not pat.search(r["f"] or ""):
            continue                      # substring hit only (e.g. C_LTA_ inside C_LTAM_)
        has_r, has_o = rid in contains[r["s"]], oid in contains[r["s"]]
        case_distinct = tok in ("C_prod_incent_", "C_Arrears_")   # other twin's code differs in case only
        if has_r and not has_o:
            why = f"scheme has only {rid}"
        elif has_r and has_o and case_distinct:
            why = f"exact-case token is {rid}'s legacy code (the other twin is upper case)"
        elif has_r and has_o and tok == "C_CTC_":
            why = "scheme has both; 266 is the input, 24 would self-reference"
        elif has_r and has_o:
            skipped.append((tok, r, "scheme has both twins - ambiguous"))
            continue
        elif has_o:
            continue                      # token already means the twin that kept the code
        else:
            skipped.append((tok, r, "scheme has neither twin"))
            continue
        changes.append({"ID": r["ID"], "scheme": r["s"], "scheme_code": r["payscheme_code"], "co": r["co_id"], "br": r["branch_id"],
                        "component": r["c"], "token": tok, "before": r["f"], "after": pat.sub(rcode, r["f"]), "why": why})

log(f"\n{len(changes)} formula(s) to change, {len(skipped)} left alone:")
by_scheme = defaultdict(list)
for c in changes:
    by_scheme[(c["scheme"], c["scheme_code"], c["co"], c["br"])].append(c)
for (s, code, co, br), cs in sorted(by_scheme.items()):
    log(f"  scheme {s} {code} (co {co}, branch {br}): {len(cs)} formula(s) [{cs[0]['why']}]")
    for c in cs:
        log(f"     detail {c['ID']} comp {c['component']}: {c['before']}")
        log(f"        -> {c['after']}")
for tok, r, why in skipped:
    log(f"  SKIP {tok} detail {r['ID']} scheme {r['s']} {r['payscheme_code']} comp {r['c']}: {why}: {r['f']}")

if not COMMIT:
    log("\nDry run - nothing written. Re-run with --commit to apply.")
    conn.close()
    sys.exit(0)

cur.execute(f"""create table if not exists {BAK} (
    ID int not null primary key, PAY_SCHEME_ID int, COMPONENT_ID int, TOKEN varchar(40),
    FORMULA_BEFORE varchar(599), FORMULA_AFTER varchar(599), changed_at timestamp default current_timestamp) engine=InnoDB""")
cur.executemany(f"insert ignore into {BAK} (ID, PAY_SCHEME_ID, COMPONENT_ID, TOKEN, FORMULA_BEFORE, FORMULA_AFTER) values (%s,%s,%s,%s,%s,%s)",
                [(c["ID"], c["scheme"], c["component"], c["token"], c["before"], c["after"]) for c in changes])
n = 0
for c in changes:
    cur.execute("update sls.pay_scheme_details set FORMULA = %s where ID = %s and FORMULA = %s", (c["after"], c["ID"], c["before"]))
    n += cur.rowcount
if n != len(changes):
    conn.rollback()
    log(f"\nexpected {len(changes)} updates, got {n} - ROLLED BACK")
    sys.exit(1)
# verify: no active formula in a touched scheme still carries a legacy token that maps to the renamed twin
left = 0
for tok, (rid, rcode, oid) in PAIRS.items():
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])")
    for r in q("select ID, PAY_SCHEME_ID s, FORMULA f from sls.pay_scheme_details where STATUS = 1 and FORMULA like %s", (f"%{tok}%",)):
        if pat.search(r["f"] or "") and rid in contains[r["s"]] and (oid not in contains[r["s"]] or tok == "C_CTC_"):
            left += 1
if left:
    conn.rollback()
    log(f"\nverification found {left} formula(s) still pointing at the wrong twin - ROLLED BACK")
    sys.exit(1)
cur.execute("""insert into vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
               values ('pay_scheme_formula_dedup_codes', 'committed', now(), now(), %s)""",
            (json.dumps({"changed": len(changes), "skipped": len(skipped), "schemes": sorted({c["scheme"] for c in changes}),
                         "backup": BAK}, default=str),))
conn.commit()
log(f"\nCOMMITTED {n} formula update(s); originals in {BAK}; migration_log step 'pay_scheme_formula_dedup_codes'")
conn.close()
