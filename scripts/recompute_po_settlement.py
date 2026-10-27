#!/usr/bin/env python
"""Recompute jute PO settlement for one branch from the command line.

    uv run python scripts/recompute_po_settlement.py --db dev3 --co 1 --branch 2 [--dry-run]

Runs the same engine as POST /api/jutePO/recompute_settlement
(src/juteProcurement/po_settlement.py) through a pymysql connection built from
env/database.env, prints one line per PO with the decision, and commits —
unless --dry-run, which rolls the transaction back so nothing is written.

Run from the vowerp3be folder (env/database.env is read by relative path).
Never prints credentials. Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "env" / "database.env"
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _load_env(path: Path) -> dict:
    """KEY=VALUE lines of env/database.env (environment variables win)."""
    values: dict = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_USER", "DATABASE_PASSWORD"):
        if os.environ.get(k):
            values[k] = os.environ[k]
    missing = [k for k in ("DATABASE_HOST", "DATABASE_USER", "DATABASE_PASSWORD") if not values.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} (env/database.env or environment)")
    values.setdefault("DATABASE_PORT", "3306")
    return values


def _engine(db_name: str):
    if not DB_NAME_RE.match(db_name):
        sys.exit(f"invalid database name: {db_name!r}")
    env = _load_env(ENV_FILE)
    from sqlalchemy import create_engine

    url = (
        f"mysql+pymysql://{quote_plus(env['DATABASE_USER'])}:{quote_plus(env['DATABASE_PASSWORD'])}"
        f"@{env['DATABASE_HOST']}:{env['DATABASE_PORT']}/{db_name}"
    )
    return create_engine(url, pool_pre_ping=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="tenant database, e.g. dev3")
    ap.add_argument("--co", required=True, type=int, help="co_id")
    ap.add_argument("--branch", required=True, type=int, help="branch_id")
    ap.add_argument("--user", type=int, default=None, help="user_id to record on closes/log rows (default NULL)")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and print, write nothing")
    args = ap.parse_args(argv)

    # Imported lazily so --help works without the project on sys.path.
    sys.path.insert(0, str(REPO_ROOT))
    from sqlalchemy.orm import Session

    from src.juteProcurement.po_settlement import load_rule, recompute_branch

    engine = _engine(args.db)
    trigger_ref = f"recompute co={args.co} br={args.branch} CLI{' dry-run' if args.dry_run else ''}"
    with Session(engine) as session:
        rule = load_rule(session, args.co)
        print(
            f"{args.db} co={args.co} branch={args.branch}: rule lorry={'on' if rule.lorry_rule_enabled else 'off'} "
            f"tolerance={rule.tolerance_lorry_fraction:g} lorry stale_days={rule.stale_days}"
            f"{' (defaults)' if rule.is_default else ''}{'  [DRY RUN]' if args.dry_run else ''}"
        )
        decisions: list = []
        counts = recompute_branch(
            session, args.co, args.branch, user_id=args.user, trigger_ref=trigger_ref, collector=decisions
        )
        for d in decisions:
            print(
                f"  PO {d.jute_po_id:>7}  status={d.status_id!s:<4} close_type={d.close_type or '-':<8} "
                f"settled={'yes' if d.settled else 'no ':<3} -> {d.decision:<9} {d.detail}"
            )
        if args.dry_run:
            session.rollback()
            print(f"DRY RUN — nothing written. Would be: {counts}")
        else:
            session.commit()
            print(f"Committed: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
