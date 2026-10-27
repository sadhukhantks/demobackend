#!/usr/bin/env python
"""
Decide whether /check-face and /attendance can drop to number_of_times_to_upsample=0.

WHY
    face_recognition.face_encodings() defaults to upsample=1, which doubles the
    image before HOG detection -- a 540x720 selfie is scanned at 1080x1440.
    Measured, that is ~4.7x the detection cost for a selfie where the face
    already fills the frame.  But a different detection box means a slightly
    different alignment, which means a slightly different 128-d embedding, and
    the live match threshold has almost no room (MATCH_THRESHOLD = 0.46 sits
    between a worst same-person distance of 0.437 and a closest
    different-person distance of 0.490).

    So this is not a change to ship on a stopwatch alone.  This script measures
    the accuracy cost against the real enrolled set.

WHAT IT COMPARES
    Every enrolled photo is replayed through the phone's punch pipeline
    (downscale to --maxdim, JPEG q80 -- see FaceValidateActivity.kt:378) and
    then matched with the production _match_face() logic under three regimes:

      A  stored=DB(up1)        live=up1     today's behaviour, the control
      B  stored=DB(up1)        live=up0     proposed change, no backfill
      C  stored=backfill(up0)  live=up0     proposed change + re-encode stored

    If B holds up, ship the one-line change.  If B regresses but C is clean,
    the change still works but needs a one-time re-encode of every stored
    embedding at upsample=0.  If C also regresses, drop the idea.

HONEST LIMIT
    The "live" image here is the enrolment photo, not a fresh selfie, so the
    absolute match rates are optimistic for all three regimes -- the same face
    in the same lighting at the same angle.  The A-vs-B-vs-C *comparison* is
    the valid signal: the optimism is identical across regimes, so any gap
    that opens up between them is caused by the upsample change alone.
    A green result here means "no regression", not "accuracy is 100%".

USAGE
    python -m scripts.validate_face_upsample --db sls
    python -m scripts.validate_face_upsample --db sls --branch 1 --maxdim 640

    Read-only.  Touches no rows, writes no files.
    Exit code 0 = safe to ship B, 2 = B regressed (see whether C is clean),
    3 = both regressed.
"""

import argparse
import base64
import io
import json
import sys

import cv2
import numpy as np

import face_recognition
import mysql.connector
from PIL import Image

from src.mobileapp.db import DB_CONFIG
from src.mobileapp.src.attendance.attendance import (
    MATCH_MARGIN, MATCH_THRESHOLD, _match_face,
)

# Enrolment re-encode size -- must match onboarding._MAX_IMAGE_DIM / _JPEG_QUALITY
# so the regime-C backfill reproduces what a real re-enrolment would store.
ENROL_DIM, ENROL_QUALITY = 800, 75

# What the phone actually uploads (FaceValidateActivity.kt / AttendanceActivity.kt).
PUNCH_QUALITY = 80

ROWS_SQL = """
    SELECT f.eb_id,
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


def jpeg_roundtrip(img_bytes, max_dim, quality):
    """Downscale + re-encode exactly as the client/enrolment path does, then
    decode through cv2 the way utils.decode_image() does on the live path."""
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, float(max_dim) / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)

    arr = np.frombuffer(buf.getvalue(), np.uint8)
    return cv2.cvtColor(cv2.imdecode(arr, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def encode(img_rgb, upsample):
    """One 128-d embedding, or None when no face is found.

    upsample=1 reproduces face_encodings()'s default; upsample=0 is the
    proposed change. Both are routed through known_face_locations so the only
    difference between them is the detection scale."""
    locs = face_recognition.face_locations(img_rgb, number_of_times_to_upsample=upsample)
    if not locs:
        return None
    encs = face_recognition.face_encodings(img_rgb, known_face_locations=locs[:1])
    return encs[0] if encs else None


def evaluate(label, stored_encs, stored_meta, probes):
    """Run every probe against the stored matrix with the production matcher.
    probes is a list of (eb_id, live_enc_or_None)."""
    stats = {
        "correct": 0, "wrong": 0, "no_face": 0,
        "rejected_threshold": 0, "rejected_margin": 0,
    }
    worst_same = 0.0          # highest same-person distance -> must stay < 0.46
    closest_diff = float("inf")  # lowest different-person distance -> must stay > 0.46

    for eb_id, live_enc in probes:
        if live_enc is None:
            stats["no_face"] += 1
            continue

        dists = face_recognition.face_distance(stored_encs, live_enc)
        same = [d for d, m in zip(dists, stored_meta) if m["eb_id"] == eb_id]
        diff = [d for d, m in zip(dists, stored_meta) if m["eb_id"] != eb_id]
        if same:
            worst_same = max(worst_same, min(same))   # the match that has to land
        if diff:
            closest_diff = min(closest_diff, min(diff))

        emp, best = _match_face(stored_encs, stored_meta, live_enc)
        if emp is None:
            # Reproduce which of the two production guards rejected it.
            if best > MATCH_THRESHOLD:
                stats["rejected_threshold"] += 1
            else:
                stats["rejected_margin"] += 1
        elif emp["eb_id"] == eb_id:
            stats["correct"] += 1
        else:
            stats["wrong"] += 1

    gap = closest_diff - worst_same if closest_diff < float("inf") else float("nan")
    print(f"\n  {label}")
    print(f"    matched correctly     {stats['correct']}")
    print(f"    MATCHED WRONG PERSON  {stats['wrong']}")
    print(f"    no face detected      {stats['no_face']}")
    print(f"    rejected (> {MATCH_THRESHOLD} )    {stats['rejected_threshold']}")
    print(f"    rejected (ambiguous)  {stats['rejected_margin']}")
    print(f"    worst same-person     {worst_same:.3f}   (must stay < {MATCH_THRESHOLD})")
    print(f"    closest diff-person   {closest_diff:.3f}   (must stay > {MATCH_THRESHOLD})")
    print(f"    separation gap        {gap:.3f}   (margin needs {MATCH_MARGIN})")
    return stats, worst_same, closest_diff


def clean(stats, worst_same, closest_diff):
    """A regime is shippable only if nobody is misidentified, nobody who used to
    match now fails, and the threshold still sits inside the same/diff gap."""
    return (stats["wrong"] == 0
            and stats["no_face"] == 0
            and stats["rejected_threshold"] == 0
            and stats["rejected_margin"] == 0
            and worst_same < MATCH_THRESHOLD < closest_diff)


def self_check():
    """Check the verdict logic on synthetic embeddings -- no DB, no face needed.

    This is the half of the script that can be wrong *quietly*: a miscounted
    guard or an inverted clean() would hand back a confident green light on a
    change that breaks matching. Distances below are Euclidean, matching
    face_distance()."""
    def vec(**axes):
        v = np.zeros(128)
        for i, val in axes.items():
            v[int(i)] = val
        return v

    # Five enrolled people. 0/1/2 are far apart; 13 and 14 sit 0.05 apart, which
    # is what the production ambiguity guard exists to catch.
    stored = np.vstack([vec(**{"0": 2.0}), vec(**{"1": 2.0}), vec(**{"2": 2.0}),
                        vec(**{"20": 5.0}), vec(**{"20": 5.0, "21": 0.05})])
    meta = [{"eb_id": n} for n in (10, 11, 12, 13, 14)]

    probes = [
        (10, vec(**{"0": 2.0, "10": 0.1})),               # lands on its own face
        (11, np.zeros(128)),                              # 2.0 from everyone
        (12, None),                                       # detector found nothing
        (13, vec(**{"20": 5.0, "22": 0.40})),             # 0.400 vs 0.403 -> coin flip
        (10, vec(**{"1": 2.0, "10": 0.1})),               # sits on somebody else
    ]
    stats, worst_same, closest_diff = evaluate("self-check", stored, meta, probes)

    assert stats["correct"] == 1, stats
    assert stats["rejected_threshold"] == 1, stats
    assert stats["no_face"] == 1, stats
    assert stats["rejected_margin"] == 1, stats
    assert stats["wrong"] == 1, stats
    assert not clean(stats, worst_same, closest_diff), "dirty run must not read clean"

    # And the all-good case must actually pass, or every run reports a false alarm.
    ok_stored = np.vstack([vec(**{"0": 2.0}), vec(**{"1": 2.0}), vec(**{"2": 2.0})])
    ok_meta = [{"eb_id": n} for n in (10, 11, 12)]
    ok_probes = [(10, vec(**{"0": 2.0, "10": 0.1})),
                 (11, vec(**{"1": 2.0, "10": 0.1})),
                 (12, vec(**{"2": 2.0, "10": 0.1}))]
    ok = evaluate("self-check (clean case)", ok_stored, ok_meta, ok_probes)
    assert ok[0]["correct"] == 3, ok[0]
    assert clean(*ok), "clean run must read clean"

    print("\nverdict logic OK", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="tenant database name, e.g. sls")
    ap.add_argument("--self-check", action="store_true",
                    help="verify this script's own verdict logic and exit")
    ap.add_argument("--branch", type=int, help="restrict to one branch_id")
    ap.add_argument("--limit", type=int, help="sample only the first N faces")
    ap.add_argument("--maxdim", type=int, default=720,
                    help="phone upload size to simulate (default 720)")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return
    if not args.db:
        ap.error("--db is required (or pass --self-check)")

    cfg = dict(DB_CONFIG, database=args.db)
    sql, params = ROWS_SQL, []
    if args.branch is not None:
        sql += " AND o.branch_id = %s"
        params.append(args.branch)
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    db = mysql.connector.connect(**cfg)
    cur = db.cursor(dictionary=True)
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    cur.close()
    db.close()

    if not rows:
        sys.exit(f"No enrolled faces found in {args.db}"
                 + (f" branch {args.branch}" if args.branch else ""))

    print(f"{len(rows)} enrolled faces from {args.db}"
          + (f" branch {args.branch}" if args.branch else "")
          + f", replayed at {args.maxdim}px q{PUNCH_QUALITY}")

    stored_db, meta, backfill, probes_up1, probes_up0 = [], [], [], [], []
    skipped = 0

    for i, r in enumerate(rows, 1):
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)}", file=sys.stderr)
        try:
            photo = base64.b64decode(r["photo_html"], validate=False)
            vec = np.asarray(json.loads(r["face_embedding"]), dtype=np.float64)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if vec.size != 128:
            skipped += 1
            continue

        # Regime C's stored side: what a re-enrolment at upsample=0 would save.
        enrol_img = jpeg_roundtrip(photo, ENROL_DIM, ENROL_QUALITY)
        back = encode(enrol_img, upsample=0)
        if back is None:
            # Cannot re-enrol this face at up0 -- keep the original so regime C
            # is judged on a full roster rather than a quietly shrunken one.
            back = vec

        # The probe: this photo as it would arrive from a phone punch.
        punch_img = jpeg_roundtrip(photo, args.maxdim, PUNCH_QUALITY)

        stored_db.append(vec)
        backfill.append(back)
        meta.append({"eb_id": r["eb_id"], "emp_code": r["emp_code"], "name": r["name"]})
        probes_up1.append((r["eb_id"], encode(punch_img, upsample=1)))
        probes_up0.append((r["eb_id"], encode(punch_img, upsample=0)))

    if skipped:
        print(f"  skipped {skipped} rows with unreadable embedding/photo")
    if not meta:
        sys.exit("No usable rows.")

    stored_db = np.vstack(stored_db)
    backfill = np.vstack(backfill)

    a = evaluate("A  stored=DB(up1)        live=up1   [control: today]",
                 stored_db, meta, probes_up1)
    b = evaluate("B  stored=DB(up1)        live=up0   [proposed, no backfill]",
                 stored_db, meta, probes_up0)
    c = evaluate("C  stored=backfill(up0)  live=up0   [proposed + re-encode]",
                 backfill, meta, probes_up0)

    print("\n" + "=" * 62)
    if not clean(*a):
        print("CONTROL A IS ALREADY DIRTY -- the current pipeline fails this")
        print("replay, so B and C cannot be judged against it. Investigate the")
        print("enrolled set before changing anything.")
        sys.exit(3)
    if clean(*b):
        print("SHIP B. upsample=0 costs nothing measurable on the enrolled set.")
        print("Change attendance.py to detect at upsample=0; no backfill needed.")
        sys.exit(0)
    if clean(*c):
        print("B REGRESSED, C IS CLEAN. upsample=0 works, but only if every")
        print("stored embedding is re-encoded at upsample=0 first. That is a")
        print("one-time backfill over employee_face_mst -- and the two must go")
        print("out together, or matching breaks in between.")
        sys.exit(2)
    print("DO NOT SHIP. upsample=0 degrades matching even with a backfill.")
    print("Keep upsample=1 and take the speed from the cache instead.")
    sys.exit(3)


if __name__ == "__main__":
    main()
