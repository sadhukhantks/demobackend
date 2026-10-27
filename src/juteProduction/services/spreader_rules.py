"""Business rules for spreader production entries.

Encapsulates the tricky group-assignment, item-lock, no-backdate, and
4-hour-window logic so both the entry router and the bin-state preview
endpoint share a single implementation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.juteProduction.constants import GROUP_WINDOW_HOURS
from src.juteProduction.services.blend import blend_label_from_db


@dataclass
class WindowResult:
    allowed: bool
    base_dt: Optional[datetime]
    allowed_end_dt: Optional[datetime]
    candidate_dt: datetime
    reason: str = ""


def _active_group_for_bin(db: Session, co_id: int, bin_id: int):
    """Return (entry_id_grp, blend) for the latest in-stock group in the bin,
    or (None, None). blend = (item_id, item_id_2, ratio_1, ratio_2)."""
    sql = text(
        """
        SELECT z.entry_id_grp, z.item_id, z.item_id_2, z.ratio_1, z.ratio_2
        FROM (
            SELECT p.entry_id_grp,
                   CAST(SUBSTRING_INDEX(
                       MIN(CONCAT(p.entry_dt, '|', LPAD(p.item_id, 9, '0'))), '|', -1
                   ) AS UNSIGNED) AS item_id,
                   MIN(p.item_id_2) AS item_id_2,
                   MIN(p.ratio_1) AS ratio_1,
                   MIN(p.ratio_2) AS ratio_2
            FROM spreader_prod_entry p
            WHERE p.co_id = :co_id AND p.bin_id = :bin_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) z
        JOIN (
            SELECT p.entry_id_grp,
                   SUM(p.no_of_rolls) AS produced,
                   COALESCE((
                       SELECT SUM(i.no_of_rolls)
                       FROM spreader_roll_issue i
                       WHERE i.entry_id_grp = p.entry_id_grp AND i.active = 1
                   ), 0) AS issued
            FROM spreader_prod_entry p
            WHERE p.co_id = :co_id AND p.bin_id = :bin_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) s ON s.entry_id_grp = z.entry_id_grp
        WHERE (s.produced - s.issued) > 0
        ORDER BY z.entry_id_grp DESC
        LIMIT 1
        """
    )
    row = db.execute(sql, {"co_id": co_id, "bin_id": bin_id}).fetchone()
    if not row:
        return None, None
    blend = (
        int(row[1]),
        int(row[2]) if row[2] is not None else None,
        int(row[3]) if row[3] is not None else 1,
        int(row[4]) if row[4] is not None else None,
    )
    return int(row[0]), blend


def _next_entry_id_grp(db: Session, co_id: int) -> int:
    sql = text("SELECT COALESCE(MAX(entry_id_grp), 0) FROM spreader_prod_entry WHERE co_id = :co_id")
    cur = db.execute(sql, {"co_id": co_id}).scalar() or 0
    return int(cur) + 1


def evaluate_4hr_window(db: Session, entry_id_grp: int, candidate_dt: datetime) -> Optional[WindowResult]:
    """Return whether `candidate_dt` falls inside the 4-hr window of `entry_id_grp`.

    Returns None if the group has no entries.
    """
    earliest_sql = text(
        """
        SELECT MIN(entry_dt) AS first_dt
        FROM spreader_prod_entry
        WHERE entry_id_grp = :grp AND active = 1
        """
    )
    row = db.execute(earliest_sql, {"grp": entry_id_grp}).fetchone()
    if not row or row[0] is None:
        return None

    base_dt = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
    candidate_dt = candidate_dt.replace(second=0, microsecond=0)
    # Same-day earliest hour determines the daily window; cross-midnight handled implicitly via entry_dt.
    same_day_sql = text(
        """
        SELECT MIN(entry_dt) AS first_dt
        FROM spreader_prod_entry
        WHERE entry_id_grp = :grp AND active = 1
          AND entry_date = :d
        """
    )
    same_row = db.execute(same_day_sql, {"grp": entry_id_grp, "d": candidate_dt.date()}).fetchone()
    if same_row and same_row[0]:
        sd = same_row[0] if isinstance(same_row[0], datetime) else datetime.fromisoformat(str(same_row[0]))
        base_dt = sd
    else:
        # Check previous day for cross-midnight continuation
        prev_sql = text(
            """
            SELECT MIN(entry_dt) AS first_dt
            FROM spreader_prod_entry
            WHERE entry_id_grp = :grp AND active = 1
              AND entry_date = :pd
            """
        )
        prev = db.execute(
            prev_sql, {"grp": entry_id_grp, "pd": candidate_dt.date() - timedelta(days=1)}
        ).fetchone()
        if prev and prev[0]:
            pd_dt = prev[0] if isinstance(prev[0], datetime) else datetime.fromisoformat(str(prev[0]))
            if (pd_dt + timedelta(hours=GROUP_WINDOW_HOURS)).date() == candidate_dt.date():
                base_dt = pd_dt
            else:
                base_dt = candidate_dt
        else:
            base_dt = candidate_dt

    allowed_end_dt = base_dt + timedelta(hours=GROUP_WINDOW_HOURS)
    # Backdated guard
    if candidate_dt < base_dt:
        return WindowResult(
            allowed=False,
            base_dt=base_dt,
            allowed_end_dt=allowed_end_dt,
            candidate_dt=candidate_dt,
            reason=(
                f"Backdated entry not allowed. Group window started "
                f"{base_dt:%Y-%m-%d %H:00}; candidate {candidate_dt:%Y-%m-%d %H:00} precedes it."
            ),
        )
    allowed = candidate_dt <= allowed_end_dt
    reason = (
        ""
        if allowed
        else (
            f"Group window closed. Base {base_dt:%Y-%m-%d %H:00} → allowed until "
            f"{allowed_end_dt:%Y-%m-%d %H:00}; candidate {candidate_dt:%Y-%m-%d %H:00} is outside the 4-hour window."
        )
    )
    return WindowResult(allowed=allowed, base_dt=base_dt, allowed_end_dt=allowed_end_dt, candidate_dt=candidate_dt, reason=reason)


def get_bin_state(db: Session, co_id: int, bin_id: int, candidate_dt: datetime) -> dict:
    """Snapshot the bin's open-group state and 4-hr window evaluation for the candidate datetime.

    Returns dict with: active_grp, locked_item_id, locked_item_id_2, locked_ratio_1,
    locked_ratio_2, locked_quality_label, window: { allowed, base_dt, allowed_end_dt, reason }.
    """
    grp, locked_blend = _active_group_for_bin(db, co_id, bin_id)
    window_info = None
    if grp is not None:
        win = evaluate_4hr_window(db, grp, candidate_dt)
        if win is not None:
            window_info = {
                "allowed": win.allowed,
                "base_dt": win.base_dt.isoformat() if win.base_dt else None,
                "allowed_end_dt": win.allowed_end_dt.isoformat() if win.allowed_end_dt else None,
                "candidate_dt": win.candidate_dt.isoformat(),
                "reason": win.reason,
            }
    locked_label = blend_label_from_db(db, *locked_blend) if locked_blend else None
    return {
        "active_grp": grp,
        "locked_item_id": locked_blend[0] if locked_blend else None,
        "locked_item_id_2": locked_blend[1] if locked_blend else None,
        "locked_ratio_1": locked_blend[2] if locked_blend else None,
        "locked_ratio_2": locked_blend[3] if locked_blend else None,
        "locked_quality_label": locked_label,
        "window": window_info,
    }


def assign_or_reuse_entry_id_grp(
    db: Session,
    co_id: int,
    bin_id: int,
    blend: Tuple[int, Optional[int], int, Optional[int]],
    candidate_dt: datetime,
) -> Tuple[int, bool]:
    """Decide whether the new entry joins the bin's open group or starts a fresh one.

    `blend` is the canonical (item_id, item_id_2, ratio_1, ratio_2) tuple.
    Raises ValueError on blend-lock, no-backdate or 4-hr-window violations.
    Returns (entry_id_grp, reused).
    """
    grp, locked_blend = _active_group_for_bin(db, co_id, bin_id)
    if grp is None:
        return _next_entry_id_grp(db, co_id), False

    if tuple(blend) != tuple(locked_blend):
        locked_label = blend_label_from_db(db, *locked_blend)
        raise ValueError(
            f"This bin already has an open group with a different quality: {locked_label}. "
            "Finish issuing those rolls before producing a different quality into the same bin."
        )

    win = evaluate_4hr_window(db, grp, candidate_dt)
    if win is None:
        # Should not happen — group existed but no entries — fall back to fresh group.
        return _next_entry_id_grp(db, co_id), False
    if not win.allowed:
        raise ValueError(win.reason)
    return grp, True
