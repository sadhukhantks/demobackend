"""Blend (dual-quality) helpers for spreader rolls.

A roll may carry one or two jute qualities. Canonical storage order is
dominant ratio first; on a ratio tie, the lower item_id comes first, so
JTD5/ATD5 (2:1) and ATD5/JTD5 (1:2) are the same stored blend.
"""

import math
from typing import Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def canonical_blend(
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> Tuple[int, Optional[int], int, Optional[int]]:
    if item_id_2 is None:
        # A pure roll has no ratio to speak of: force 1 so a crafted {item_id, ratio_1: 3}
        # payload stores and locks as the same tuple as a normal pure payload.
        return int(item_id), None, 1, None
    i1, i2 = int(item_id), int(item_id_2)
    r1, r2 = int(ratio_1 or 1), int(ratio_2)
    if r2 > r1 or (r2 == r1 and i2 < i1):
        return i2, i1, r2, r1
    return i1, i2, r1, r2


def blend_label(
    item_name: Optional[str],
    item_name_2: Optional[str],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> str:
    if not item_name_2:
        return item_name or ""
    # ponytail: malformed import rows can carry a NULL ratio (the API validator forbids it);
    # label-only default of 1, the stored ratio is never rewritten.
    return f"{item_name}/{item_name_2} ({int(ratio_1 or 1)}:{int(ratio_2 or 1)})"


def blend_label_from_db(
    db: Session,
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
) -> str:
    ids = [int(item_id)] + ([int(item_id_2)] if item_id_2 is not None else [])
    rows = db.execute(
        text("SELECT item_id, item_name FROM item_mst WHERE item_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": ids},
    ).fetchall()
    names = {int(r[0]): r[1] for r in rows}
    return blend_label(
        names.get(int(item_id), f"#{item_id}"),
        names.get(int(item_id_2), f"#{item_id_2}") if item_id_2 is not None else None,
        ratio_1,
        ratio_2,
    )


def target_maturity_hours(
    maturity_map: dict,
    item_id: int,
    item_id_2: Optional[int],
    ratio_1: Optional[int],
    ratio_2: Optional[int],
    default: int,
) -> int:
    m1 = maturity_map.get(int(item_id))
    if item_id_2 is None:
        return int(m1) if m1 is not None else int(default)
    m2 = maturity_map.get(int(item_id_2))
    if m1 is None and m2 is None:
        return int(default)
    if m1 is None:
        return int(m2)
    if m2 is None:
        return int(m1)
    r1, r2 = int(ratio_1 or 1), int(ratio_2 or 1)
    # floor(x + .5) mirrors MySQL ROUND() (half away from zero) used by the SQL
    # maturity report; Python's round() is banker's and would disagree on .5 ties.
    return math.floor((int(m1) * r1 + int(m2) * r2) / (r1 + r2) + 0.5)
