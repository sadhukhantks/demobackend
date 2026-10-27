"""
Sync the ACTIVE component rows of a legacy vowsls pay scheme onto its sls mirror scheme.

Why: sls 2017 (njm_newbudli_payroll, co 106 / branch 87) was mirrored from the hand-made sls scheme 17, not from
legacy 169, so EPF / ESI / PTAX / NET / bank components are missing or parked at STATUS=0 and a 2017 payroll run
cannot cook NET. The engine (payRegister._evaluate_employee_formulas) resolves CODE tokens globally but only cooks
components that are active in the scheme, so a formula referencing an inactive component waits on a value that is
never produced and is reported as "cyclic dependency".

Rule per legacy active row (vowsls.tbl_pay_scheme_details, PAY_SCHEME_ID = L, STATUS = 1), component C:
  keep        sls has an active (S, C) row         -> untouched; FORMULA/TYPE/DEFAULT_VALUE diffs are reported only
  reactivate  sls has (S, C) rows, all STATUS = 0  -> highest ID gets STATUS=1 + FORMULA/TYPE/DEFAULT_VALUE from legacy
  insert      sls has no (S, C) row                -> new row, ID = legacy ID + 20,000,000 (INSERT IGNORE, idempotent)
Nothing on sls is ever deactivated; no scheme other than S is touched.

--overwrite-kept-defaults additionally copies the legacy DEFAULT_VALUE onto kept rows whose DEFAULT_VALUE differs.
DANGEROUS on a mirror scheme that was hand-tuned: sls 2017 carries the NJM rate constants (C_DARatePerMonth_,
C_inctime_, C_ratetime1_/2_, C_wrkhrs1_, C_genincrate_) and the eligibility flags as scheme defaults where legacy 169
holds 0, so the copy zeroes them. FORMULA/TYPE of kept rows are never touched by the flag (the 169/2017 formula diffs
are whitespace-only or algebraically identical) - they stay reported-only.

Safety: every CODE token in a copied formula must resolve to the same component ID on both sides (sls renamed the
four duplicated legacy codes: 76 C_LTAs_, 87 C_prod_incents_, 165 C_Arrearss_, 266 C_CTCs_) - otherwise abort.
Tokens that resolve to a component not active in S after the sync are listed as warnings.

Formulas are normalised on the way in: html.unescape (legacy PHP stored `&gt;` / `&lt;` / `&amp;`; sls stores raw
operators and payRegister does not unescape) and strip() (legacy 169 C_rsdamt_ ends in "\\t\\t\\n"). Empty -> NULL,
as the 2026-08-15 scheme migration did.

Rollback (commit mode snapshots the reactivated rows first; the +20M inserts are deleted by ID range):
    UPDATE sls.pay_scheme_details d JOIN vowsls_to_sls_transfer._bak_pay_scheme_details_<S>_<YYYYMMDD> b ON b.ID = d.ID
       SET d.STATUS = b.STATUS, d.FORMULA = b.FORMULA, d.TYPE = b.TYPE, d.DEFAULT_VALUE = b.DEFAULT_VALUE,
           d.updated_by = b.updated_by, d.updated_date_time = b.updated_date_time;
    DELETE FROM sls.pay_scheme_details WHERE PAY_SCHEME_ID = <S> AND ID BETWEEN 20000000 AND 29999999;

Usage:
    python run_sync_scheme_details_vowsls_to_sls.py --legacy 169 --sls 2017            # dry run
    python run_sync_scheme_details_vowsls_to_sls.py --legacy 169 --sls 2017 --commit   # backup + write + verify + log
    python run_sync_scheme_details_vowsls_to_sls.py --legacy 169 --sls 2017 --overwrite-kept-defaults --commit
"""
import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pymysql

ap = argparse.ArgumentParser()
ap.add_argument("--legacy", type=int, required=True, help="vowsls PAY_SCHEME_ID")
ap.add_argument("--sls", type=int, required=True, help="sls PAY_SCHEME_ID (mirror)")
ap.add_argument("--commit", action="store_true")
ap.add_argument("--overwrite-kept-defaults", action="store_true",
                help="also copy legacy DEFAULT_VALUE onto already-active rows that differ (see module docstring)")
A = ap.parse_args()
COMMIT, L, S = A.commit, A.legacy, A.sls
OFFSET = 20_000_000
BAK = f"vowsls_to_sls_transfer._bak_pay_scheme_details_{S}_{date.today():%Y%m%d}"
STEP = f"pay_scheme_details_{L}_to_{S}"
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")   # payRegister._IDENT_RE - codes may carry inner underscores

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
STATS = {"legacy_scheme": L, "sls_scheme": S}


def log(m):
    print(m, flush=True)


def q(sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def ddiff(l, s):
    return abs(float(l["DEFAULT_VALUE"] or 0) - float(s["DEFAULT_VALUE"] or 0)) >= 1e-6


def diffs(l, s):
    """Field names that differ (legacy vs sls), with values; whitespace-only formula diffs are labelled."""
    d = []
    lf, sf = l["FORMULA"] or "", s["FORMULA"] or ""
    if lf != sf:
        d.append(("FORMULA (whitespace only)" if re.sub(r"\s+", "", lf) == re.sub(r"\s+", "", sf) else "FORMULA") + f" sls {sf!r} | legacy {lf!r}")
    if str(l["TYPE"]) != str(s["TYPE"]):
        d.append(f"TYPE sls {s['TYPE']!r} | legacy {l['TYPE']!r}")
    if ddiff(l, s):
        d.append(f"DEFAULT_VALUE sls {s['DEFAULT_VALUE']} | legacy {l['DEFAULT_VALUE']}")
    return d


# -- 1. classify -----------------------------------------------------------------------
legacy = {r["COMPONENT_ID"]: r for r in q("""select ID, COMPONENT_ID, FORMULA, TYPE, DEFAULT_VALUE from vowsls.tbl_pay_scheme_details
                                            where PAY_SCHEME_ID = %s and STATUS = 1 order by ID""", (L,))}
assert len(legacy) == cur.rowcount, f"legacy scheme {L} has duplicate active components"
for r in legacy.values():
    r["FORMULA"] = html.unescape(r["FORMULA"] or "").strip() or None
sls = defaultdict(list)
for r in q("select ID, COMPONENT_ID, FORMULA, TYPE, STATUS, DEFAULT_VALUE from sls.pay_scheme_details where PAY_SCHEME_ID = %s order by ID", (S,)):
    sls[r["COMPONENT_ID"]].append(r)
code = {r["ID"]: r["CODE"] for r in q("select ID, CODE from sls.pay_components")}
dup_active = [c for c, rs in sls.items() if sum(r["STATUS"] == 1 for r in rs) > 1]
assert not dup_active, f"sls scheme {S} already has duplicate active rows for components {dup_active}"
keep, react, ins = {}, {}, []
for c in legacy:
    active = [r for r in sls.get(c, ()) if r["STATUS"] == 1]
    if active:
        keep[c] = active[0]
    elif sls.get(c):
        react[c] = sls[c][-1]          # rows are ID-ordered -> highest ID
    else:
        ins.append(c)
sls_only = sorted({c for c, rs in sls.items() if any(r["STATUS"] == 1 for r in rs)} - set(legacy))
n_act = sum(r["STATUS"] == 1 for rs in sls.values() for r in rs)
log(f"legacy {L}: {len(legacy)} active rows | sls {S}: {n_act} active, {sum(map(len, sls.values())) - n_act} inactive")
log(f"classification: keep {len(keep)}, reactivate {len(react)}, insert {len(ins)}")
if sls_only:
    log(f"WARNING: active on sls {S} but not in legacy {L} (left active, verification will fail): {sls_only}")
for c, s in react.items():
    l = legacy[c]
    log(f"  REACTIVATE comp {c:4d} {code.get(c)!s:24s} sls ID {s['ID']}: TYPE {l['TYPE']} DEFAULT {l['DEFAULT_VALUE']} FORMULA {l['FORMULA']!r}")
for c in ins:
    l = legacy[c]
    log(f"  INSERT     comp {c:4d} {code.get(c)!s:24s} new ID {l['ID'] + OFFSET}: TYPE {l['TYPE']} DEFAULT {l['DEFAULT_VALUE']} FORMULA {l['FORMULA']!r}")
over = {c: s for c, s in keep.items() if ddiff(legacy[c], s)} if A.overwrite_kept_defaults else {}
kept_diffs = [(c, diffs(legacy[c], s)) for c, s in keep.items() if diffs(legacy[c], s)]
log(f"kept rows differing from legacy ({'DEFAULT_VALUE overwritten, FORMULA/TYPE' if over else 'all'} left as-is): {len(kept_diffs)} of {len(keep)}")
for c, d in kept_diffs[:20]:
    log(f"  {'OVERWRITE' if c in over else 'DIFF     '} comp {c:4d} {code.get(c)!s:24s} {'; '.join(d)}")
if over:
    log(f"overwrite-kept-defaults: {len(over)} kept rows will take the legacy DEFAULT_VALUE")

new_ids = [legacy[c]["ID"] + OFFSET for c in ins]
clash = [r["ID"] for r in q("select ID from sls.pay_scheme_details where ID in %s", (new_ids,))] if new_ids else []
if clash:
    log(f"ABORT: sls IDs already in use for the planned inserts: {clash}")
    sys.exit(1)

# -- 2. formula token safety (copied formulas only) ------------------------------------
l_codes = defaultdict(set)
for r in q("select ID, CODE from vowsls.tbl_pay_components"):
    l_codes[(r["CODE"] or "").strip()].add(r["ID"])
s_codes = {(r["CODE"] or "").strip(): r["ID"] for r in q("select ID, CODE from sls.pay_components")}
final_active = set(legacy) | set(sls_only)
bad, warn = [], []
for c in list(react) + ins:
    # ponytail: c<id> tokens skipped - IDs are shared between vowsls and sls, only CODEs were renamed
    for tok in sorted({t for t in IDENT.findall(legacy[c]["FORMULA"] or "") if t.lower().startswith("c_")}):
        cands = l_codes.get(tok, set())
        in_scheme = cands & set(legacy)           # legacy engine is scheme-scoped: the twin inside L wins
        lid = next(iter(in_scheme)) if len(in_scheme) == 1 else (next(iter(cands)) if len(cands) == 1 else None)
        sid = s_codes.get(tok)
        if sid is None or lid is None or sid != lid:
            bad.append(f"comp {c} token {tok}: vowsls {sorted(cands) or None} vs sls {sid}")
        elif sid not in final_active:
            warn.append(f"comp {c} token {tok} -> {sid} not active in {S} after sync (engine will report cyclic dependency)")
for w in warn:
    log(f"WARNING: {w}")
if bad:
    log("ABORT: formula tokens do not resolve to the same component on sls:\n  " + "\n  ".join(bad))
    sys.exit(1)
STATS.update(keep=len(keep), reactivate={c: r["ID"] for c, r in react.items()}, insert=dict(zip(ins, new_ids)),
             overwrite_defaults={c: [s["DEFAULT_VALUE"], legacy[c]["DEFAULT_VALUE"]] for c, s in over.items()},
             kept_diffs=len(kept_diffs), sls_only_active=sls_only, token_warnings=warn)

if not COMMIT:
    log("\nDry run - nothing written. Re-run with --commit to apply.")
    conn.close()
    sys.exit(0)
if not react and not ins and not over:
    log("\nNothing to do.")
    sys.exit(0)

# -- 3. backup + write (server-side, same MySQL server) --------------------------------
# ponytail: CREATE TABLE is an implicit commit and fails if the table exists - a same-day re-run must drop/rename it
cur.execute(f"create table {BAK} as select * from sls.pay_scheme_details where ID in %s",
            ([r["ID"] for r in react.values()] + [s["ID"] for s in over.values()] or [-1],))
STATS["backup"] = BAK
# ponytail: 18-odd rows, so the normalised formulas travel as parameters instead of a server-side INSERT...SELECT
n_upd = cur.executemany("""update sls.pay_scheme_details
    set STATUS = 1, FORMULA = %s, TYPE = %s, DEFAULT_VALUE = %s, updated_by = 0, updated_date_time = now()
    where ID = %s and PAY_SCHEME_ID = %s and STATUS = 0""",
    [(legacy[c]["FORMULA"], legacy[c]["TYPE"], legacy[c]["DEFAULT_VALUE"], r["ID"], S) for c, r in react.items()]) or 0
n_ins = cur.executemany("""insert ignore into sls.pay_scheme_details
    (ID, COMPONENT_ID, FORMULA, PAY_SCHEME_ID, TYPE, STATUS, DEFAULT_VALUE, updated_by, updated_date_time)
    values (%s, %s, %s, %s, %s, 1, %s, 0, now())""",
    [(legacy[c]["ID"] + OFFSET, c, legacy[c]["FORMULA"], S, legacy[c]["TYPE"], legacy[c]["DEFAULT_VALUE"]) for c in ins]) or 0
n_over = cur.executemany("""update sls.pay_scheme_details
    set DEFAULT_VALUE = %s, updated_by = 0, updated_date_time = now()
    where ID = %s and PAY_SCHEME_ID = %s and STATUS = 1""",
    [(legacy[c]["DEFAULT_VALUE"], s["ID"], S) for c, s in over.items()]) or 0
STATS.update(updated=n_upd, inserted=n_ins, defaults_overwritten=n_over)
log(f"updated {n_upd}, inserted {n_ins}, defaults overwritten {n_over} (backup {BAK})")

# -- 4. verify, rollback on failure ----------------------------------------------------
s_active = {r["COMPONENT_ID"] for r in q("select COMPONENT_ID from sls.pay_scheme_details where PAY_SCHEME_ID = %s and STATUS = 1", (S,))}
dup = q("select COMPONENT_ID from sls.pay_scheme_details where PAY_SCHEME_ID = %s and STATUS = 1 group by 1 having count(*) > 1", (S,))
target = {c: r["ID"] for c, r in react.items()} | dict(zip(ins, new_ids)) | {c: s["ID"] for c, s in over.items()}
after = {r["ID"]: r for r in q("select ID, COMPONENT_ID, FORMULA, TYPE, STATUS, DEFAULT_VALUE from sls.pay_scheme_details where ID in %s",
                               (list(target.values()),))}
# overwritten rows are only guaranteed to match on DEFAULT_VALUE - their FORMULA/TYPE were deliberately left alone
bad_rows = [c for c, i in target.items()
            if i not in after or after[i]["STATUS"] != 1 or after[i]["COMPONENT_ID"] != c
            or (ddiff(legacy[c], after[i]) if c in over else diffs(legacy[c], after[i]))]
STATS.update(verify=dict(active_sets_equal=s_active == set(legacy), duplicate_active=len(dup), bad_rows=bad_rows,
                         changed=n_upd + n_ins + n_over, expected=len(target)))
log(f"verify: {STATS['verify']}")
if not (s_active == set(legacy) and not dup and not bad_rows and n_upd + n_ins + n_over == len(target)):
    conn.rollback()
    log(f"\nVERIFICATION FAILED - rolled back, nothing kept (backup table {BAK} remains).")
    sys.exit(1)
cur.execute("""insert into vowsls_to_sls_transfer.migration_log (step, status, started_at, finished_at, details)
               values (%s, 'committed', now(), now(), %s)""", (STEP, json.dumps(STATS, default=str)))
conn.commit()
log(f"\nCOMMITTED and logged to vowsls_to_sls_transfer.migration_log step '{STEP}'")
conn.close()
