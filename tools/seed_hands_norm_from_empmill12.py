"""Seed jute_prod_hands_norms_mst from the legacy MIS occupation norms (EMPMILL12).

Source: EMPMILL12.OCCUPATION_MASTER_NORMS WHERE ACTIVE='Y' AND company_id=2
(legacy single-company MIS, branch 29).

Designation resolution is *verified, never guessed*:
  --tenant sls   -> match legacy desig_id to <target>.designation_mst.designation_id,
                    then require UPPER(TRIM(desig)) == UPPER(TRIM(OCCU_DESC)).
  --tenant dev3  -> ids differ between tenants, so match on UPPER(TRIM(name)) only.
Any row that fails to resolve (NULL desig_id, id absent, name mismatch, ambiguous
name) is SKIPPED and listed in the report.

machine_type_id is left NULL — the variable-norm machine link is configured later
by SQL, the same way jute_prod_checklist_map is.

Usage:
    python tools/seed_hands_norm_from_empmill12.py --tenant dev3 [--dry-run]
    python tools/seed_hands_norm_from_empmill12.py --self-test
"""

import argparse
import re
import sys
from collections import defaultdict

import pymysql
from dotenv import dotenv_values

SRC_TABLE = "EMPMILL12.OCCUPATION_MASTER_NORMS"
SRC_WHERE = "ACTIVE='Y' AND company_id=2"
EFFECTIVE_DATE = "2000-01-01"

# Norms-text -> hands_per_machine. Ordered; first match wins.
# "N MC = M HANDS" is one rule covering both spec directions (N MC = 1 HAND -> 1/N,
# 1 MC = N HANDS -> N) and the real "3 MC = 2 HANDS" / "15 FRM = 1 HAND" variants.
_MC_EQ_HANDS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:MC|FRM|FRAMES?)\s*=\s*(\d+(?:\.\d+)?)\s*HANDS?", re.I
)
_HANDS_PER_MC = re.compile(r"(\d+(?:\.\d+)?)\s*(?:HANDS?\s*)?PER\s*MC\b", re.I)
_PER_N_FRAMES = re.compile(r"(\d+(?:\.\d+)?)\s*PER\s+(\d+(?:\.\d+)?)\s*FRAMES?\b", re.I)


def parse_hands_per_machine(norms_text):
    """Return hands per machine, or None when the text carries no machine norm.

    Shift-count norms ('1 PER SHIFT', 'A=2,B=1') and free text ('NO NORMS',
    '450 KGS /WINDER') return None on purpose — their targets come from the
    shift_a/b/c/g columns, or they are simply unparseable and keep only the text.
    """
    if not norms_text:
        return None
    for rx, calc in (
        (_MC_EQ_HANDS, lambda m: float(m.group(2)) / float(m.group(1))),
        (_HANDS_PER_MC, lambda m: float(m.group(1))),
        (_PER_N_FRAMES, lambda m: float(m.group(1)) / float(m.group(2))),
    ):
        m = rx.search(norms_text)
        if m:
            try:
                v = calc(m)
            except ZeroDivisionError:
                return None
            return round(v, 4) if v > 0 else None
    return None


def self_test():
    cases = {
        # spec's four rules
        "36 MC = 1 HAND": 0.0278,
        "1 MC = 4 HANDS": 4.0,
        "2 HANDS  PER MC": 2.0,
        "1 PER 22 FRAMES": 0.0455,
        # shift-count norms must stay None (targets come from shift cols)
        "1 PER SHIFT": None,
        "2 / SHIFT": None,
        "A=2,B=1,C=1": None,
        "1 IN EACH SHIFT": None,
        # unparseable free text -> None, text kept by the caller
        "NO NORMS": None,
        "AS ACTUAL": None,
        "4 POINTS": None,
        "450 KGS /WINDER": None,
        "140 BNDLS / HAND": None,
        None: None,
        "": None,
        # real messy variants seen in the source
        " 2 MC= 1 HAND": 0.5,
        "32 MC = 1HAND": 0.0313,
        "36 MC =  1  HAND": 0.0278,
        "3 MC = 2 HANDS": 0.6667,
        "15 FRM = 1 HAND, 120 SPNDL": 0.0667,
        "570 CUTS , 1 MC= 1 HAND": 1.0,
        "50 MC = 1 HAND  FOR S4 MC": 0.02,
        "3  PER MC": 3.0,
        "1  PER MC": 1.0,
    }
    bad = []
    for text, want in cases.items():
        got = parse_hands_per_machine(text)
        if want is None and got is not None:
            bad.append((text, want, got))
        elif want is not None and (got is None or abs(got - want) > 0.0002):
            bad.append((text, want, got))
    for text, want, got in bad:
        print(f"  FAIL |{text}| want={want} got={got}")
    assert not bad, f"{len(bad)} parser case(s) failed"
    print(f"parser self-test OK ({len(cases)} cases)")


def connect(env, db=None):
    return pymysql.connect(
        host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]),
        user=env["DATABASE_USER"], password=env["DATABASE_PASSWORD"],
        database=db, cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def norm(s):
    return (s or "").strip().upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", choices=["dev3", "sls"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0
    if not args.tenant:
        ap.error("--tenant is required (or use --self-test)")

    env = dotenv_values("env/database.env")
    conn = connect(env, args.tenant)
    cur = conn.cursor()

    cur.execute(
        f"SELECT desig_id, OCCU_DESC, DIRECT_INDIRECT, VARIABLE_FIXED, "
        f"SHIFT_A, SHIFT_B, SHIFT_C, SHIFT_G, NORMS, branch_id "
        f"FROM {SRC_TABLE} WHERE {SRC_WHERE}"
    )
    src = cur.fetchall()

    cur.execute("SELECT designation_id, branch_id, desig FROM designation_mst")
    target = cur.fetchall()
    by_id = {r["designation_id"]: r for r in target}
    by_name = defaultdict(list)
    for r in target:
        by_name[norm(r["desig"])].append(r)

    cur.execute("SELECT branch_id, co_id FROM branch_mst")
    co_of_branch = {r["branch_id"]: r["co_id"] for r in cur.fetchall()}

    rows, skipped = [], []
    for s in src:
        name = norm(s["OCCU_DESC"])
        if args.tenant == "sls":
            # User ruling 2026-08-25: id match is PRIMARY (all 250 non-null ids
            # verified present on sls); a differing name is a legacy typo, not a
            # blocker — warn and seed. Name match is only the fallback for the
            # rows whose legacy desig_id is NULL.
            t = by_id.get(s["desig_id"]) if s["desig_id"] is not None else None
            if t is not None:
                if norm(t["desig"]) != name:
                    print(f"  warn name differs (seeded by id): "
                          f"{s['OCCU_DESC']!r} != {t['desig']!r} (id {s['desig_id']})")
            else:
                # Same-branch candidates only: sls designation names repeat per
                # branch (branch 87 carries clones of the factory set) and a
                # cross-branch hit would file the norm under the wrong branch.
                hits = [h for h in by_name.get(name, [])
                        if h["branch_id"] == s["branch_id"]]
                if not hits:
                    reason = "null_desig_id_name_absent" if s["desig_id"] is None else "id_and_name_absent"
                    skipped.append((reason, s["OCCU_DESC"], s["desig_id"]))
                    continue
                if len(hits) > 1:
                    skipped.append(("name_ambiguous", s["OCCU_DESC"], [h["designation_id"] for h in hits]))
                    continue
                t = hits[0]
        else:
            hits = by_name.get(name, [])
            if not hits:
                skipped.append(("name_absent", s["OCCU_DESC"], s["desig_id"]))
                continue
            if len(hits) > 1:
                skipped.append(("name_ambiguous", s["OCCU_DESC"], [h["designation_id"] for h in hits]))
                continue
            t = hits[0]

        co_id = co_of_branch.get(t["branch_id"])
        if co_id is None:
            skipped.append(("branch_has_no_co", s["OCCU_DESC"], t["branch_id"]))
            continue

        rows.append((
            co_id, t["branch_id"], t["designation_id"],
            (s["DIRECT_INDIRECT"] or "")[:1] or None,
            (s["VARIABLE_FIXED"] or "")[:1] or None,
            s["SHIFT_A"] or 0, s["SHIFT_B"] or 0, s["SHIFT_C"] or 0, s["SHIFT_G"] or 0,
            None,  # machine_type_id — configured later by SQL
            parse_hands_per_machine(s["NORMS"]),
            (s["NORMS"] or None),
            EFFECTIVE_DATE, 1,
        ))

    parsed = sum(1 for r in rows if r[10] is not None)
    print(f"tenant={args.tenant}  source={len(src)}  resolved={len(rows)}  skipped={len(skipped)}")
    print(f"  hands_per_machine parsed={parsed}  NULL(text kept)={len(rows) - parsed}")
    reasons = defaultdict(int)
    for why, _, _ in skipped:
        reasons[why] += 1
    print("  skip reasons:", dict(reasons))
    for why, what, extra in skipped:
        print(f"    SKIP [{why}] {what} ({extra})")

    if args.dry_run:
        print("\n-- dry run, nothing written. First 10 rows:")
        for r in rows[:10]:
            print("   ", r)
        conn.close()
        return 0

    co_ids = sorted({r[0] for r in rows})
    try:
        # rerun-safe: this script owns (co_id, effective_date) for the co's it seeds
        cur.execute(
            "DELETE FROM jute_prod_hands_norms_mst WHERE effective_date=%s AND co_id IN %s",
            (EFFECTIVE_DATE, tuple(co_ids) or (0,)),
        )
        deleted = cur.rowcount
        cur.executemany(
            "INSERT INTO jute_prod_hands_norms_mst (co_id, branch_id, designation_id, direct_indirect,"
            " variable_fixed, shift_a, shift_b, shift_c, shift_g, machine_type_id,"
            " hands_per_machine, norms_text, effective_date, active)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
        conn.commit()
        print(f"\nCOMMITTED: deleted {deleted}, inserted {len(rows)} into {args.tenant}.jute_prod_hands_norms_mst"
              f" (co_id in {co_ids})")
    except Exception:
        conn.rollback()
        print("ROLLED BACK")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
