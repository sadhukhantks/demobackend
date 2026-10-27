#!/usr/bin/env python
"""
Re-encode every stored face embedding at upsample=0 -- regime C from
scripts/validate_face_upsample.py.

ONLY RUN THIS IF validate_face_upsample.py EXITED 2
    Exit 2 means live-side upsample=0 regressed against the current (upsample=1)
    stored embeddings, but was clean once the stored side was re-encoded to
    match. This script performs that re-encode. If validation exited 0 you do
    not need this at all -- just change the code. If it exited 3, this will not
    save the change.

ORDERING -- THE PART THAT BITES
    The backfill and the upsample=0 code change must go out in the SAME window.
    They are not independently safe:

      backfill first, old code still live  ->  stored=up0 vs live=up1
      code first, not yet backfilled       ->  stored=up1 vs live=up0  (regime B)

    Both are the mismatch validation told you to avoid. Deploy together.
    In-memory caches clear themselves within face_cache._TTL_SECONDS (60 s) of
    the backfill, so no restart is needed for the data to be picked up.

SAFETY
    Dry-run by default -- prints what would change and writes nothing.
    --apply dumps every current embedding to a timestamped JSON file BEFORE the
    first UPDATE, then writes all rows in a single transaction so a partial
    backfill cannot happen. A half-backfilled table is the worst possible state:
    some workers match, some silently do not.
    --restore <file> puts the backup back, also in one transaction.

USAGE
    python -m scripts.backfill_face_upsample --db sls                  # dry run
    python -m scripts.backfill_face_upsample --db sls --apply
    python -m scripts.backfill_face_upsample --db sls --restore face_embedding_backup_sls_20260814T091500.json
"""

import argparse
import base64
import json
import sys
from datetime import datetime

import numpy as np

import mysql.connector

from src.mobileapp.db import DB_CONFIG
# Reuse the validation script's encode path verbatim -- the whole point of the
# backfill is to write exactly what regime C measured. A second copy of these
# would be free to drift away from what was actually validated.
from scripts.validate_face_upsample import ENROL_DIM, ENROL_QUALITY, encode, jpeg_roundtrip

ROWS_SQL = """
    SELECT f.emp_face_id,
           f.eb_id,
           o.emp_code,
           CONCAT(p.first_name, ' ', COALESCE(p.last_name, '')) AS name,
           f.face_embedding,
           f.photo_html
    FROM employee_face_mst f
    INNER JOIN hrms_ed_official_details o ON f.eb_id = o.eb_id
    INNER JOIN hrms_ed_personal_details p ON f.eb_id = p.eb_id
    WHERE p.active = 1 AND f.active = 1
      AND f.face_embedding IS NOT NULL
      AND f.photo_html IS NOT NULL
"""

UPDATE_SQL = "UPDATE employee_face_mst SET face_embedding = %s WHERE emp_face_id = %s"


def connect(db_name):
    return mysql.connector.connect(**dict(DB_CONFIG, database=db_name))


def write_all(db_name, pairs, what):
    """Apply [(embedding_json, emp_face_id), ...] in one transaction."""
    db = connect(db_name)
    cur = db.cursor()
    try:
        cur.executemany(UPDATE_SQL, pairs)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()
    print(f"{what}: {len(pairs)} rows committed")


def do_restore(args):
    with open(args.restore, encoding="utf-8") as fh:
        backup = json.load(fh)
    if backup["db"] != args.db:
        sys.exit(f"backup is for db '{backup['db']}', not '{args.db}' -- refusing")
    pairs = [(row["face_embedding"], row["emp_face_id"]) for row in backup["rows"]]
    print(f"restoring {len(pairs)} embeddings captured {backup['taken_at']}")
    write_all(args.db, pairs, "restored")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="tenant database name, e.g. sls")
    ap.add_argument("--branch", type=int,
                    help="restrict to one branch_id. Matching is branch-scoped, so a "
                         "branch stays internally consistent -- but the code change is "
                         "global, so every branch you skip lands in regime B.")
    ap.add_argument("--limit", type=int, help="only the first N rows (smoke test)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this, nothing is modified.")
    ap.add_argument("--restore", metavar="FILE", help="restore embeddings from a backup file")
    ap.add_argument("--self-check", action="store_true",
                    help="verify backup/restore fidelity and exit")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return
    if not args.db:
        ap.error("--db is required (or pass --self-check)")
    if args.restore:
        do_restore(args)
        return

    sql, params = ROWS_SQL, []
    if args.branch is not None:
        sql += " AND o.branch_id = %s"
        params.append(args.branch)
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    db = connect(args.db)
    cur = db.cursor(dictionary=True)
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    cur.close()
    db.close()

    if not rows:
        sys.exit(f"No enrolled faces in {args.db}"
                 + (f" branch {args.branch}" if args.branch else ""))

    print(f"{len(rows)} enrolled faces in {args.db}"
          + (f" branch {args.branch}" if args.branch else "")
          + f", re-encoding at {ENROL_DIM}px q{ENROL_QUALITY} upsample=0\n")

    updates, backup_rows, drifts = [], [], []
    undetected, unreadable = [], []

    for i, r in enumerate(rows, 1):
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)}", file=sys.stderr)
        try:
            photo = base64.b64decode(r["photo_html"], validate=False)
            old = np.asarray(json.loads(r["face_embedding"]), dtype=np.float64)
        except (ValueError, TypeError):
            unreadable.append(r)
            continue
        if old.size != 128:
            unreadable.append(r)
            continue

        new = encode(jpeg_roundtrip(photo, ENROL_DIM, ENROL_QUALITY), upsample=0)
        if new is None:
            # Left at its upsample=1 value. This person's live punch will very
            # likely also fail to detect at upsample=0, so they need a new
            # enrolment photo -- rewriting the row would not fix them.
            undetected.append(r)
            continue

        drifts.append(float(np.linalg.norm(new - old)))
        backup_rows.append({"emp_face_id": r["emp_face_id"],
                            "face_embedding": r["face_embedding"]})
        updates.append((json.dumps(new.tolist()), r["emp_face_id"]))

    if drifts:
        d = np.array(drifts)
        print(f"\n  re-encoded            {len(updates)}")
        print(f"  drift old->new        mean {d.mean():.3f}  max {d.max():.3f}")
        print("                        (how far each embedding moved; compare to "
              "MATCH_THRESHOLD 0.46)")
    if undetected:
        print(f"\n  NO FACE AT upsample=0  {len(undetected)}  -- left unchanged, "
              f"these need re-enrolment with a clearer photo:")
        for r in undetected[:20]:
            print(f"      {r['emp_code']}  {r['name'].strip()}")
        if len(undetected) > 20:
            print(f"      ... and {len(undetected) - 20} more")
    if unreadable:
        print(f"\n  unreadable rows        {len(unreadable)}  -- left unchanged")

    if not updates:
        sys.exit("\nNothing to write.")

    if not args.apply:
        print(f"\nDRY RUN -- nothing written. Re-run with --apply to update "
              f"{len(updates)} rows.")
        return

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = f"face_embedding_backup_{args.db}_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"db": args.db, "taken_at": stamp, "rows": backup_rows}, fh)
        fh.flush()
    print(f"\nbackup written: {path}  ({len(backup_rows)} rows)")

    write_all(args.db, updates, "backfilled")
    print(f"\nDone. Deploy the upsample=0 code change now -- until you do, "
          f"matching is stored=up0 vs live=up1.")
    print(f"To undo: python -m scripts.backfill_face_upsample --db {args.db} "
          f"--restore {path}")


def self_check():
    """The dangerous part of this script is the backup: if it does not round-trip
    exactly, --restore silently returns corrupted embeddings and every worker
    stops matching. Check that path with no DB."""
    import tempfile, os

    rng = np.random.default_rng(0)
    originals = {i: rng.normal(size=128) for i in range(1, 6)}

    # Backup stores the raw DB string, exactly as this script reads it.
    backup_rows = [{"emp_face_id": k, "face_embedding": json.dumps(v.tolist())}
                   for k, v in originals.items()]
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"db": "t", "taken_at": "x", "rows": backup_rows}, fh)
        with open(path, encoding="utf-8") as fh:
            reloaded = json.load(fh)

        assert reloaded["db"] == "t"
        pairs = [(r["face_embedding"], r["emp_face_id"]) for r in reloaded["rows"]]
        assert len(pairs) == 5, pairs
        for emb_json, face_id in pairs:
            back = np.asarray(json.loads(emb_json), dtype=np.float64)
            # Bit-exact: float -> repr -> float must not lose a digit, or the
            # restored face sits a hair away from where it started.
            assert np.array_equal(back, originals[face_id]), f"drift on {face_id}"
            assert back.size == 128
    finally:
        os.unlink(path)

    # The UPDATE pairs must be (value, key) -- reversed, this writes ids into
    # the embedding column across the whole table.
    new = np.ones(128)
    payload, key = (json.dumps(new.tolist()), 42)
    assert UPDATE_SQL.index("face_embedding = %s") < UPDATE_SQL.index("emp_face_id = %s"), \
        "UPDATE placeholder order changed -- (value, key) tuples are now wrong"
    assert isinstance(payload, str) and isinstance(key, int)

    print("backup/restore fidelity OK", file=sys.stderr)


if __name__ == "__main__":
    main()
