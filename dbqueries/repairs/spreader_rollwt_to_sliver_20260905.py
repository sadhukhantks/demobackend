"""
ROLLWT2SLIVER-20260905 (TKT-000153 follow-up) — move sliver-cut readings that were
entered on the Spreader ROLL WEIGHT tab (R-08-04) over to the SLIVER WEIGHT tab
(R-08-03) on sls, then soft-delete them from the roll table.

Why: jute_sqc_spreader_roll_wt rows 3, 4, 5 (17/18/19-Aug) and 19 (05-Sep) hold
readings in the 1025-1942 range with CV 12.5-17.2%, while every genuine roll row
(6-18) sits at 56-69 kg with CV 2.0-5.1% — and the four odd rows carry
feeder_name NULL, which no genuine row does. That range and dispersion match
jute_sqc_spreader_sliver_wt exactly (1054-1942, CV 14.0-17.6%): they are 5-yd
sliver cuts in GRAMS put on the wrong tab. No unit maps 1448.9 -> ~59 kg, so they
are not mis-scaled roll weights. The roll tab goes clean on 20-Aug, the very day
the sliver tab's first row appears; 05-Sep is a single regression.

Rule (per row): INSERT one jute_sqc_spreader_sliver_wt row carrying co_id,
branch_id, entry_date, spell_id, mc_id, item_id, observed_weights (= roll_weights)
and mr_pcts across verbatim; category NULL and sample_length_yds 5.00 /
weight_basis 'LB/100YDS' from the R-08-03 defaults; std_mr_pct re-looked-up from
the item-keyed jute_spreader_quality_attr satellite; every calc_* RECOMPUTED by
importing compute_spreader_sliver_stats — the two reports do NOT share stats
(roll persists stdev of OBSERVED, sliver persists stdev of CORRECTED), so copying
the roll numbers across would be wrong. Then UPDATE the roll row active = 0.

None of the four dates collide with an existing sliver row (sliver has 20/21/22-Aug
and 01-Sep). This table is QC-only: src/juteSQC/query.py is its sole reader, no
production, stock or costing path consumes it.

Usage (repo root, venv active):
    python dbqueries/repairs/spreader_rollwt_to_sliver_20260905.py --check
    python dbqueries/repairs/spreader_rollwt_to_sliver_20260905.py --apply

--check is SELECT-only: prints the plan, writes
    spreader_rollwt_to_sliver_20260905_plan.csv
    spreader_rollwt_to_sliver_20260905_rollback.sql
--apply runs the whole plan in ONE transaction, then re-reads and asserts each new
sliver row round-trips its readings and its calc_* match a fresh recompute, and
that every source roll row is now active = 0. Any failure rolls back entirely.
Rollback = run the generated .sql (deletes the new sliver rows by id, restores
active = 1 on the roll rows).
"""
import csv
import json
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.juteSQC.spreader_sliver_wt import (  # noqa: E402
    DEFAULT_SAMPLE_LENGTH_YDS,
    DEFAULT_STD_MR_PCT,
    DEFAULT_WEIGHT_BASIS,
    compute_spreader_sliver_stats,
)

ENV = ROOT / "env" / "database.env"
DB = "sls"
TAG = "ROLLWT2SLIVER-20260905"
ROLL_IDS = (3, 4, 5, 19)
GRAM_SCALE_FLOOR = 500.0  # every misplaced reading is >= 1025; roll rows are 56-69

PLAN_CSV = Path(__file__).with_name("spreader_rollwt_to_sliver_20260905_plan.csv")
ROLLBACK_SQL = Path(__file__).with_name("spreader_rollwt_to_sliver_20260905_rollback.sql")


def _env():
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def connect():
    e = _env()
    return pymysql.connect(
        host=e["DATABASE_HOST"], port=int(e.get("DATABASE_PORT", 3306)),
        user=e["DATABASE_USER"], password=e["DATABASE_PASSWORD"], database=DB,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


ROLL_SQL = """
SELECT spreader_roll_wt_id, co_id, branch_id, entry_date, spell_id, mc_id, item_id,
       feeder_name, roll_weights, mr_pcts, calc_avg_obs, active, updated_by
FROM jute_sqc_spreader_roll_wt
WHERE spreader_roll_wt_id IN %(ids)s
ORDER BY spreader_roll_wt_id
"""

# Same satellite _lookup_std_mr_pct reads (src/juteSQC/query.py::get_spreader_quality_std_query).
STD_SQL = "SELECT std_mr_pct FROM jute_spreader_quality_attr WHERE item_id = %(item_id)s"

CLASH_SQL = """
SELECT spreader_sliver_wt_id, entry_date, mc_id
FROM jute_sqc_spreader_sliver_wt
WHERE co_id = %(co_id)s AND entry_date = %(entry_date)s AND active = 1
"""

INSERT_SQL = """
INSERT INTO jute_sqc_spreader_sliver_wt
  (co_id, branch_id, entry_date, spell_id, category, mc_id, item_id,
   sample_length_yds, weight_basis, observed_weights, mr_pcts, std_mr_pct,
   calc_avg_obs, calc_avg_corr, calc_avg_mr, calc_stdev, calc_cv_pct,
   active, updated_by)
VALUES
  (%(co_id)s, %(branch_id)s, %(entry_date)s, %(spell_id)s, NULL, %(mc_id)s, %(item_id)s,
   %(sample_length_yds)s, %(weight_basis)s, %(observed_weights)s, %(mr_pcts)s, %(std_mr_pct)s,
   %(calc_avg_obs)s, %(calc_avg_corr)s, %(calc_avg_mr)s, %(calc_stdev)s, %(calc_cv_pct)s,
   1, %(updated_by)s)
"""

DEACTIVATE_SQL = """
UPDATE jute_sqc_spreader_roll_wt SET active = 0
WHERE spreader_roll_wt_id = %(id)s AND active = 1
"""


def _std_mr(cur, item_id):
    """Mirror _lookup_std_mr_pct: satellite value for the item, else base 16."""
    if item_id is None:
        return DEFAULT_STD_MR_PCT
    cur.execute(STD_SQL, {"item_id": int(item_id)})
    row = cur.fetchone()
    if row is None or row.get("std_mr_pct") is None:
        return DEFAULT_STD_MR_PCT
    return float(row["std_mr_pct"])


def build_plan(cur):
    cur.execute(ROLL_SQL, {"ids": ROLL_IDS})
    rows = cur.fetchall()
    missing = set(ROLL_IDS) - {r["spreader_roll_wt_id"] for r in rows}
    if missing:
        sys.exit(f"!! roll ids not found on {DB}: {sorted(missing)}")

    plan, refused = [], []
    for r in rows:
        rid = r["spreader_roll_wt_id"]
        if r["active"] != 1:
            refused.append(f"roll {rid}: already active={r['active']}, skipped")
            continue
        if r["feeder_name"] is not None:
            # The NULL feeder is half the evidence this row is misplaced. A named
            # feeder means the wrong-tab signature does not hold — leave it alone.
            refused.append(
                f"roll {rid}: feeder_name={r['feeder_name']!r} is not the wrong-tab signature, skipped"
            )
            continue
        obs = json.loads(r["roll_weights"])
        mr = json.loads(r["mr_pcts"])
        if len(obs) != len(mr):
            refused.append(f"roll {rid}: {len(obs)} weights vs {len(mr)} mr%, skipped")
            continue
        if not obs or min(obs) < GRAM_SCALE_FLOOR:
            refused.append(f"roll {rid}: min reading {min(obs) if obs else None} is roll-scale, skipped")
            continue

        cur.execute(CLASH_SQL, {"co_id": r["co_id"], "entry_date": r["entry_date"]})
        clash = [c["spreader_sliver_wt_id"] for c in cur.fetchall()]
        if clash:
            refused.append(f"roll {rid}: sliver row(s) already on {r['entry_date']}: {clash}, skipped")
            continue

        stats = compute_spreader_sliver_stats(obs, mr, _std_mr(cur, r["item_id"]))
        plan.append({
            "roll_id": rid,
            "co_id": r["co_id"], "branch_id": r["branch_id"], "entry_date": r["entry_date"],
            "spell_id": r["spell_id"], "mc_id": r["mc_id"], "item_id": r["item_id"],
            "sample_length_yds": DEFAULT_SAMPLE_LENGTH_YDS,
            "weight_basis": DEFAULT_WEIGHT_BASIS,
            "observed_weights": json.dumps([float(w) for w in obs]),
            "mr_pcts": json.dumps([float(m) for m in mr]),
            "std_mr_pct": stats["std_mr_pct"],
            "calc_avg_obs": stats["calc_avg_obs"],
            "calc_avg_corr": stats["calc_avg_corr"],
            "calc_avg_mr": stats["calc_avg_mr"],
            "calc_stdev": stats["calc_stdev"],
            "calc_cv_pct": stats["calc_cv_pct"],
            "updated_by": r["updated_by"],
            "old_roll_avg_obs": float(r["calc_avg_obs"]) if r["calc_avg_obs"] is not None else None,
        })
    return plan, refused


def summarise(plan, refused):
    print(f"{TAG} on {DB} — roll ids {list(ROLL_IDS)}")
    print(f"  to move: {len(plan)}   refused: {len(refused)}")
    for msg in refused:
        print(f"  ! {msg}")
    for p in plan:
        lbs = p["calc_avg_obs"] / 453.6 * 20
        print(
            f"  roll {p['roll_id']} {p['entry_date']} mc={p['mc_id']} item={p['item_id']}"
            f" -> sliver: avg_obs {p['calc_avg_obs']} g = {lbs:.2f} lbs/100yds,"
            f" avg_corr {p['calc_avg_corr']}, stdev {p['calc_stdev']}, cv {p['calc_cv_pct']}"
        )


def write_artifacts(plan, new_ids=None):
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(plan[0].keys()))
        w.writeheader()
        for p in plan:
            w.writerow(p)
    lines = [
        f"-- Rollback for {TAG} on {DB}.",
        "-- Deletes the sliver rows this repair inserted and reactivates the roll rows.",
        "START TRANSACTION;",
    ]
    if new_ids:
        ids = ", ".join(str(i) for i in new_ids)
        lines.append(f"DELETE FROM jute_sqc_spreader_sliver_wt WHERE spreader_sliver_wt_id IN ({ids});")
    else:
        lines.append("-- (new sliver ids are filled in by --apply)")
    roll_ids = ", ".join(str(p["roll_id"]) for p in plan)
    lines.append(f"UPDATE jute_sqc_spreader_roll_wt SET active = 1 WHERE spreader_roll_wt_id IN ({roll_ids});")
    lines.append("COMMIT;")
    ROLLBACK_SQL.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {PLAN_CSV.name}, {ROLLBACK_SQL.name}")


def apply(conn, cur, plan):
    new_ids = []
    for p in plan:
        params = {k: v for k, v in p.items() if k not in ("roll_id", "old_roll_avg_obs")}
        cur.execute(INSERT_SQL, params)
        new_ids.append(cur.lastrowid)
        if cur.execute(DEACTIVATE_SQL, {"id": p["roll_id"]}) != 1:
            conn.rollback()
            sys.exit(f"!! roll {p['roll_id']} was not deactivated — rolled back")
    return new_ids


def verify(cur, plan, new_ids):
    errors = []
    for p, sid in zip(plan, new_ids):
        cur.execute(
            "SELECT * FROM jute_sqc_spreader_sliver_wt WHERE spreader_sliver_wt_id = %(id)s",
            {"id": sid},
        )
        row = cur.fetchone()
        if row is None:
            errors.append(f"sliver {sid}: row missing after insert")
            continue
        if row["observed_weights"] != p["observed_weights"] or row["mr_pcts"] != p["mr_pcts"]:
            errors.append(f"sliver {sid}: readings did not round-trip")
        fresh = compute_spreader_sliver_stats(
            json.loads(row["observed_weights"]),
            json.loads(row["mr_pcts"]),
            float(row["std_mr_pct"]),
        )
        for col in ("calc_avg_obs", "calc_avg_corr", "calc_avg_mr", "calc_stdev", "calc_cv_pct"):
            if abs(float(row[col]) - fresh[col]) > 0.0001:
                errors.append(f"sliver {sid}: {col} {row[col]} != recompute {fresh[col]}")
        cur.execute(
            "SELECT active FROM jute_sqc_spreader_roll_wt WHERE spreader_roll_wt_id = %(id)s",
            {"id": p["roll_id"]},
        )
        if cur.fetchone()["active"] != 0:
            errors.append(f"roll {p['roll_id']}: still active")

    cur.execute(
        "SELECT MIN(calc_avg_obs) lo, MAX(calc_avg_obs) hi"
        " FROM jute_sqc_spreader_roll_wt WHERE active = 1"
    )
    rng = cur.fetchone()
    print(f"  active roll rows now span avg_obs {rng['lo']} .. {rng['hi']} kg")
    if rng["hi"] is not None and float(rng["hi"]) > GRAM_SCALE_FLOOR:
        errors.append(f"roll table still holds a gram-scale row: max avg_obs {rng['hi']}")
    return errors


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in ("--check", "--apply"):
        sys.exit("usage: spreader_rollwt_to_sliver_20260905.py [--check|--apply]")
    conn = connect()
    cur = conn.cursor()
    try:
        plan, refused = build_plan(cur)
        summarise(plan, refused)
        if not plan:
            sys.exit("nothing to do")
        write_artifacts(plan)
        if mode != "--apply":
            print("\nCheck-only mode, no changes. Run with --apply to execute.")
            return
        new_ids = apply(conn, cur, plan)
        errors = verify(cur, plan, new_ids)
        if errors:
            conn.rollback()
            print("\n".join(errors))
            sys.exit("!! post-verify failed — rolled back, nothing written")
        write_artifacts(plan, new_ids)
        conn.commit()
        print(f"\nCOMMITTED {len(new_ids)} moved rows, tag {TAG}. New sliver ids: {new_ids}")
        print(f"Rollback: {ROLLBACK_SQL.name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
