"""Parity harness: the impact report's RANGE read vs the weaving DAY SLICE on one day.

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md §9 ("parity
gate"). The Loom Modification Impact report reads loom-days through
``weaving_range_loomday_sql`` (loom-quality-day rows, standards resolved in Python by
``build_loom_days``) instead of the per-day slice ``weaving_day_slice_sql``. This
script proves both agree per loom-day on a sample day:

    production_yds, production_kg  within 0.01
    actual_measured / capacity     within 0.01 (efficiency numerator = yds of the
                                   spell rows with std_prod_yds > 0, i.e. manned
                                   rows only — an unmanned spell's output on a day
                                   another spell was manned must not count)
    efficiency %                   within 0.05 points (None == None)
    working hours, cuts            within 0.01

READ-ONLY (SELECT only). Usage (from the repo root, uv venv):

    uv run python dbqueries/verify_weaving_range_parity.py --db dev3 --co 9 --branch 12 --date 2026-07-12
    uv run python dbqueries/verify_weaving_range_parity.py --db dev3 --co 9 --branch 12 --date 2026-07-12 --machines 30,31,32
    uv run python dbqueries/verify_weaving_range_parity.py --db sls --co 2 --branch 29 --date 2026-09-10 --machines <ids>

Without --machines every loom with an active row on the day is compared.
Connection defaults come from env/database.env (or --host/--port/--user/--password).
Exit code 0 = parity, 1 = drift found, 2 = usage / setup error.
"""

import argparse
import datetime
import os
import sys
from urllib.parse import quote_plus

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from sqlalchemy import create_engine, text  # noqa: E402

from src.juteProduction.loom_modification_impact import (  # noqa: E402
    build_loom_days,
    build_picks_index,
    build_speed_index,
)
from src.juteProduction.loom_modification_impact_query import (  # noqa: E402
    get_pick_rows_query,
    get_target_map_speed_rows_query,
    pick_range_index_hint,
    weaving_range_index_hint,
    weaving_range_loomday_sql,
)
from src.juteProduction.weaving_query import weaving_day_slice_sql  # noqa: E402

TOL_QTY = 0.01
TOL_EFF = 0.05


def load_env_defaults():
    defaults = {}
    path = os.path.join(REPO_ROOT, "env", "database.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                defaults[k.strip()] = v.strip()
    return defaults


def parse_args():
    env = load_env_defaults()
    p = argparse.ArgumentParser(description="Range read vs day slice parity on one day (read-only).")
    p.add_argument("--host", default=env.get("DATABASE_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(env.get("DATABASE_PORT", 3306)))
    p.add_argument("--user", default=env.get("DATABASE_USER", "root"))
    p.add_argument("--password", default=env.get("DATABASE_PASSWORD", ""))
    p.add_argument("--db", required=True, help="Tenant DB (dev3 for QA; sls is production — read-only)")
    p.add_argument("--co", type=int, required=True, help="co_id")
    p.add_argument("--branch", type=int, default=None, help="branch_id (NULL-tolerant filter)")
    p.add_argument("--date", required=True, help="YYYY-MM-DD sample day")
    p.add_argument("--machines", default=None, help="comma-separated machine_ids (default: every loom with a row that day)")
    return p.parse_args()


def slice_loom_days(conn, co_id, branch_id, day, machines):
    """The slice's rows rolled up to (machine_id) for ONE day — the reference."""
    q = text(weaving_day_slice_sql(include_branch_filter=True))
    rows = conn.execute(q, {"co_id": co_id, "tran_date": day, "spell_id": None, "machine_id": None,
                            "branch_id": branch_id}).fetchall()
    acc = {}
    for r in rows:
        m = dict(r._mapping)
        mid = int(m["machine_id"])
        if machines and mid not in machines:
            continue
        a = acc.setdefault(mid, {"yds": 0.0, "kg": 0.0, "hours": 0.0, "cuts": 0.0,
                                 "actual_measured": 0.0, "capacity_measured": 0.0, "rows": 0})
        yds = float(m["production_yds"] or 0)
        std = float(m["std_prod_yds"] or 0)
        a["yds"] += yds
        a["kg"] += float(m["production_kg"] or 0)
        a["hours"] += float(m["working_hours"] or 0)
        a["cuts"] += float(m["cuts"] or 0)
        a["rows"] += 1
        if std > 0:
            a["actual_measured"] += yds
            a["capacity_measured"] += std
    for a in acc.values():
        a["eff"] = (a["actual_measured"] * 100.0 / a["capacity_measured"]) if a["capacity_measured"] > 0 else None
    return acc


def range_loom_days(conn, co_id, branch_id, day, machines):
    hint = pick_range_index_hint([r._mapping["Key_name"] for r in conn.execute(weaving_range_index_hint()).fetchall()])
    binds = {"co_id": co_id, "branch_id": branch_id, "date_from": day, "date_to": day}
    speed_rows = [dict(r._mapping) for r in conn.execute(get_target_map_speed_rows_query(), {"co_id": co_id}).fetchall()]
    pick_rows = [dict(r._mapping) for r in conn.execute(get_pick_rows_query(), binds).fetchall()]
    t0 = datetime.datetime.now()
    q_rows = [dict(r._mapping) for r in conn.execute(
        weaving_range_loomday_sql(hint), {**binds, "machines": machines, "control_machines": []}).fetchall()]
    ms = (datetime.datetime.now() - t0).total_seconds() * 1000
    lds = build_loom_days(q_rows, build_speed_index(speed_rows), build_picks_index(pick_rows))
    out = {}
    for ld in lds:
        out[ld["machine_id"]] = {
            "yds": ld["yds"], "kg": ld["kg"], "hours": ld["hours_run"], "cuts": ld["cuts"],
            "actual_measured": ld["actual_measured"], "capacity_measured": ld["capacity_measured"],
            "eff": (ld["actual_measured"] * 100.0 / ld["capacity_measured"]) if ld["capacity_measured"] > 0 else None,
            "rows": ld["rows_n"],
        }
    return out, ms, hint


def close(a, b, tol):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def main():
    args = parse_args()
    try:
        day = datetime.date.fromisoformat(args.date)
    except ValueError:
        print("bad --date", file=sys.stderr)
        return 2
    url = f"mysql+pymysql://{args.user}:{quote_plus(args.password)}@{args.host}:{args.port}/{args.db}"
    engine = create_engine(url)
    with engine.connect() as conn:
        if args.machines:
            machines = [int(x) for x in args.machines.split(",") if x.strip()]
        else:
            machines = [int(r[0]) for r in conn.execute(text(
                "SELECT DISTINCT wd.machine_id FROM jute_prod_weaving_daily wd "
                "WHERE wd.active = 1 AND wd.co_id = :co AND wd.tran_date = :d "
                "AND (:b IS NULL OR wd.branch_id = :b OR wd.branch_id IS NULL)"),
                {"co": args.co, "d": day, "b": args.branch}).fetchall()]
        if not machines:
            print(f"no active weaving rows for co {args.co} on {day} in {args.db}")
            return 2
        ref = slice_loom_days(conn, args.co, args.branch, day, set(machines))
        got, ms, hint = range_loom_days(conn, args.co, args.branch, day, machines)

    drift = []
    for mid in sorted(set(ref) | set(got)):
        a, b = ref.get(mid), got.get(mid)
        if a is None or b is None:
            drift.append((mid, "missing on one side", a, b))
            continue
        for k, tol in (("yds", TOL_QTY), ("kg", TOL_QTY), ("hours", TOL_QTY), ("cuts", TOL_QTY),
                       ("actual_measured", TOL_QTY), ("capacity_measured", TOL_QTY), ("eff", TOL_EFF)):
            if not close(a[k], b[k], tol):
                drift.append((mid, k, a[k], b[k]))
    n_eff = sum(1 for a in ref.values() if a["eff"] is not None)
    print(f"{args.db} co {args.co} branch {args.branch} {day}: {len(machines)} looms, "
          f"slice rows {sum(a['rows'] for a in ref.values())}, range rows {sum(b['rows'] for b in got.values())}, "
          f"range SQL {ms:.0f} ms (hint {hint or 'none'})")
    print(f"  Σ yds slice {sum(a['yds'] for a in ref.values()):.3f} / range {sum(b['yds'] for b in got.values()):.3f}; "
          f"Σ kg {sum(a['kg'] for a in ref.values()):.3f} / {sum(b['kg'] for b in got.values()):.3f}; "
          f"Σ hours {sum(a['hours'] for a in ref.values()):.2f} / {sum(b['hours'] for b in got.values()):.2f}; "
          f"looms with measurable efficiency {n_eff}")
    if drift:
        print(f"DRIFT on {len(drift)} values:")
        for mid, k, a, b in drift[:50]:
            print(f"  machine {mid} {k}: slice {a} range {b}")
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
