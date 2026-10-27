"""Jute PO settlement (closing) engine.

Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md (§4, §5).

A jute PO is *settled* when its receipts meet the company's settlement rule
(all lorries in, or received weight within a fraction of one lorry — default
half a lorry — of the PO weight, spec §11.1); the engine then closes it with
``close_type = 'AUTO'``. An AUTO close is derived
state — a pure function of the current receipts — so the same engine reopens
the PO when a receipt is rejected / cancelled / returned, its weight edited or
it is re-pointed to another PO. Manual closes (SHORT / BULK) and the legacy
MIGRATED closes are intent, never reopened by the engine.

This module is the ONLY place that writes ``jute_po.status_id = 5`` or the
four close columns, and every real transition writes a ``jute_po_status_log``
row. Everything runs inside the caller's transaction (no commit here).

Public surface (all sync, SQLAlchemy ``text()`` with named binds):

    is_settled(receipts, rule)                       pure
    load_rule(db, co_id)                             row or defaults, tolerant of a missing table
    recompute_po_settlement(db, po_id, ...)          -> "closed" | "reopened" | "unchanged" | "skipped"
    settle_after_mr_change(db, mr_id, ...)           hook for MR / gate-entry endpoints
    recompute_branch(db, co_id, branch_id, ...)      -> {"closed": n, "reopened": n, "unchanged": n}
    manual_close_po(db, po_id, ...)                  SHORT / BULK close for the endpoints
    reopen_closed_po(db, po_id, ...)                 reopen from CLOSED, restoring the prior status
    save_rule(db, co_id, rule, user_id)              upsert jute_po_settle_rule
    get_status_log(db, po_id)                        log rows newest first, with names
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import List, Literal, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.common.utils import now_ist
from src.juteProcurement.constants import (
    JUTE_MR_RECEIVED_WEIGHT_SQL,
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    JUTE_PO_CLOSED_STATUS,
    JUTE_PO_LIVE_STATUSES,
    JUTE_PO_SETTLEMENT_SKIPPED_STATUSES,
)

logger = logging.getLogger(__name__)

Decision = Literal["closed", "reopened", "unchanged", "skipped"]

CLOSE_TYPE_AUTO = "AUTO"
CLOSE_TYPE_SHORT = "SHORT"
CLOSE_TYPE_BULK = "BULK"
CLOSE_TYPE_MIGRATED = "MIGRATED"
# Closes the engine never undoes on its own (spec §2 "sticky").
STICKY_CLOSE_TYPES = (CLOSE_TYPE_SHORT, CLOSE_TYPE_BULK, CLOSE_TYPE_MIGRATED)

SOURCE_AUTO = "AUTO"
SOURCE_MANUAL = "MANUAL"
SOURCE_BULK = "BULK"
SOURCE_BACKEND = "BACKEND"
SOURCE_MIGRATION = "MIGRATION"

# Fallback prior status when a CLOSED PO is reopened and no close row is logged.
DEFAULT_REOPEN_STATUS = 3

# Rule bounds accepted by PUT /settle_rule (spec §11.1: tolerance is a fraction of one lorry).
TOLERANCE_FRACTION_MIN, TOLERANCE_FRACTION_MAX = 0.0, 1.0
STALE_DAYS_MIN, STALE_DAYS_MAX = 1, 365
REMARK_MAX_LEN = 500

# MySQL error codes for "schema not migrated yet": 1054 unknown column,
# 1146 table doesn't exist.
_MISSING_SCHEMA_ERRNOS = (1054, 1146)


# =============================================================================
# Dataclasses + pure decision
# =============================================================================


@dataclass
class SettleRule:
    lorry_rule_enabled: bool = False  # owner 2026-09-25: weight decides; lorry count alone never settles
    # Weight tolerance as a fraction of ONE lorry (po_weight / vehicle_quantity):
    # 0.5 = half a lorry (owner: a 10 MT lorry -> 5 MT, a 16 MT lorry -> 8 MT).
    tolerance_lorry_fraction: float = 0.5
    stale_days: int = 30
    # True when the company has no jute_po_settle_rule row (defaults apply).
    is_default: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PoReceipts:
    vehicle_quantity: Optional[int]
    po_weight: Optional[float]
    received_lorries: int
    received_weight: float
    # Returned lorries (MR status 48) — informational only, never counted as received.
    returned_lorries: int = 0
    returned_weight: float = 0.0

    def summary(self) -> str:
        lorries_of = "-" if self.vehicle_quantity is None else str(int(self.vehicle_quantity))
        weight_of = "-" if self.po_weight is None else f"{float(self.po_weight):,.0f}"
        out = (
            f"lorries {int(self.received_lorries)}/{lorries_of}, "
            f"weight {float(self.received_weight):,.0f}/{weight_of}"
        )
        if int(self.returned_lorries or 0) > 0:
            out += f", returned {int(self.returned_lorries)}"
        return out


def weight_tolerance(r: PoReceipts, rule: SettleRule) -> float:
    """Absolute weight tolerance = tolerance_lorry_fraction × (po_weight / vehicle_quantity);
    0 when vehicle_quantity is NULL/0 (no lorry size to derive it from)."""
    vehicles = int(r.vehicle_quantity or 0)
    po_weight = float(r.po_weight or 0.0)
    if vehicles <= 0 or po_weight <= 0:
        return 0.0
    return float(rule.tolerance_lorry_fraction) * (po_weight / vehicles)


def is_settled(r: PoReceipts, rule: SettleRule) -> bool:
    """Spec §11.1:
        settled_by_lorries = lorry_rule_enabled AND vehicle_quantity > 0 AND received_lorries >= vehicle_quantity
        settled_by_weight  = po_weight > 0 AND received_weight >= po_weight − tolerance
    where tolerance = tolerance_lorry_fraction × (po_weight / vehicle_quantity), 0 without vehicles."""
    vehicles = int(r.vehicle_quantity or 0)
    lorries_ok = bool(rule.lorry_rule_enabled) and vehicles > 0 and int(r.received_lorries or 0) >= vehicles

    po_weight = float(r.po_weight or 0.0)
    # Round the threshold so 63,000 vs 70,000 − 7,000 never loses to float noise.
    threshold = round(po_weight - weight_tolerance(r, rule), 3)
    weight_ok = po_weight > 0 and float(r.received_weight or 0.0) >= threshold

    return lorries_ok or weight_ok


# =============================================================================
# SQL
# =============================================================================

_MR_EXCLUDED_SQL = ", ".join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)

# Receipt predicate (spec §2): an MR with a gate entry against the PO that is
# not rejected / cancelled / draft / returned — the same statuses the stock
# reports exclude. Returned lorries (48) are counted separately, for display.
_RECEIPTS_SQL = text(f"""
    SELECT COALESCE(SUM(CASE WHEN jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN 1 ELSE 0 END), 0) AS received_lorries,
           COALESCE(SUM(CASE WHEN jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN {JUTE_MR_RECEIVED_WEIGHT_SQL} END), 0) AS received_weight,
           COALESCE(SUM(CASE WHEN jm.is_returned = 1 AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN 1 ELSE 0 END), 0) AS returned_lorries,
           COALESCE(SUM(CASE WHEN jm.is_returned = 1 AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN jm.net_weight END), 0) AS returned_weight
    FROM jute_mr jm
    WHERE jm.po_id = :po_id
      AND jm.jute_gate_entry_no IS NOT NULL
""")

_PO_SQL = text("""
    SELECT jp.jute_po_id, jp.status_id, jp.close_type, jp.vehicle_quantity, jp.weight,
           jp.branch_id, bm.co_id
    FROM jute_po jp
    INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
    WHERE jp.jute_po_id = :po_id
""")

_RULE_SQL = text("""
    SELECT lorry_rule_enabled, tolerance_lorry_fraction, stale_days
    FROM jute_po_settle_rule
    WHERE co_id = :co_id
""")

_RULE_UPSERT_SQL = text("""
    INSERT INTO jute_po_settle_rule
        (co_id, lorry_rule_enabled, tolerance_lorry_fraction, stale_days, updated_by)
    VALUES (:co_id, :lorry_rule_enabled, :tolerance_lorry_fraction, :stale_days, :updated_by)
    ON DUPLICATE KEY UPDATE
        lorry_rule_enabled = VALUES(lorry_rule_enabled),
        tolerance_lorry_fraction = VALUES(tolerance_lorry_fraction),
        stale_days = VALUES(stale_days),
        updated_by = VALUES(updated_by)
""")

_CLOSE_SQL = text("""
    UPDATE jute_po
    SET status_id = :closed, close_type = :close_type, closed_date = :now,
        closed_by = :user_id, close_remark = :remark
    WHERE jute_po_id = :po_id
""")

_REOPEN_SQL = text("""
    UPDATE jute_po
    SET status_id = :status_id, close_type = NULL, closed_date = NULL,
        closed_by = NULL, close_remark = NULL
    WHERE jute_po_id = :po_id
""")

_LOG_INSERT_SQL = text("""
    INSERT INTO jute_po_status_log
        (jute_po_id, old_status_id, new_status_id, close_type, source, trigger_ref, remark, user_id, created_at)
    VALUES (:po_id, :old_status_id, :new_status_id, :close_type, :source, :trigger_ref, :remark, :user_id, :now)
""")

# The status the PO had when it was last closed (any source), for reopen.
_PRIOR_STATUS_SQL = text("""
    SELECT old_status_id
    FROM jute_po_status_log
    WHERE jute_po_id = :po_id AND new_status_id = :closed
    ORDER BY log_id DESC
    LIMIT 1
""")

_MR_PO_SQL = text("SELECT po_id FROM jute_mr WHERE jute_mr_id = :mr_id")

_LIVE_SQL = ", ".join(str(s) for s in JUTE_PO_LIVE_STATUSES)

# Candidates of a branch recompute: live POs (may close) and AUTO-closed ones (may reopen).
_BRANCH_CANDIDATES_SQL = text(f"""
    SELECT jp.jute_po_id
    FROM jute_po jp
    INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
    WHERE bm.co_id = :co_id
      AND jp.branch_id = :branch_id
      AND (jp.status_id IN ({_LIVE_SQL})
           OR (jp.status_id = :closed AND jp.close_type = :auto))
    ORDER BY jp.jute_po_id
""")

_STATUS_LOG_SQL = text("""
    SELECT l.log_id, l.jute_po_id,
           l.old_status_id, so.status_name AS old_status_name,
           l.new_status_id, sn.status_name AS new_status_name,
           l.close_type, l.source, l.trigger_ref, l.remark,
           l.user_id, um.name AS user_name, l.created_at
    FROM jute_po_status_log l
    LEFT JOIN status_mst so ON so.status_id = l.old_status_id
    LEFT JOIN status_mst sn ON sn.status_id = l.new_status_id
    LEFT JOIN user_mst um ON um.user_id = l.user_id
    WHERE l.jute_po_id = :po_id
    ORDER BY l.log_id DESC
""")


# =============================================================================
# Helpers
# =============================================================================


def _mapping(row) -> dict:
    return dict(row._mapping) if row is not None else {}


def _is_missing_schema_error(exc: BaseException) -> bool:
    """True for MySQL 'unknown column' / 'table does not exist' — the tenant
    has not had 2026-09-25_jute_po_settlement.sql yet."""
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ())
    return bool(args) and args[0] in _MISSING_SCHEMA_ERRNOS


is_missing_settlement_schema = _is_missing_schema_error

MISSING_SCHEMA_MSG = (
    "PO settlement is not enabled on this tenant yet "
    "(run dbqueries/migrations/2026-09-25_jute_po_settlement.sql)"
)


def load_rule(db: Session, co_id) -> SettleRule:
    """The company's settlement rule, or the defaults when there is no row —
    or no table at all (a tenant without the migration). Never raises."""
    try:
        row = db.execute(_RULE_SQL, {"co_id": int(co_id)}).fetchone()
    except Exception as exc:  # pragma: no cover - exercised on un-migrated tenants
        if not _is_missing_schema_error(exc):
            logger.warning("jute_po_settle_rule lookup failed for co_id=%s: %s", co_id, exc)
        return SettleRule()
    m = _mapping(row)
    if not m or m.get("stale_days") is None:
        return SettleRule()
    return SettleRule(
        lorry_rule_enabled=bool(int(m.get("lorry_rule_enabled") or 0)),
        tolerance_lorry_fraction=float(m.get("tolerance_lorry_fraction")),
        stale_days=int(m.get("stale_days")),
        is_default=False,
    )


def save_rule(db: Session, co_id: int, rule: SettleRule, user_id) -> SettleRule:
    """Upsert the company's rule (no commit)."""
    db.execute(
        _RULE_UPSERT_SQL,
        {
            "co_id": int(co_id),
            "lorry_rule_enabled": 1 if rule.lorry_rule_enabled else 0,
            "tolerance_lorry_fraction": float(rule.tolerance_lorry_fraction),
            "stale_days": int(rule.stale_days),
            "updated_by": user_id,
        },
    )
    return SettleRule(
        lorry_rule_enabled=bool(rule.lorry_rule_enabled),
        tolerance_lorry_fraction=float(rule.tolerance_lorry_fraction),
        stale_days=int(rule.stale_days),
        is_default=False,
    )


def _load_receipts(db: Session, po_id: int, vehicle_quantity, po_weight) -> PoReceipts:
    m = _mapping(db.execute(_RECEIPTS_SQL, {"po_id": int(po_id)}).fetchone())
    return PoReceipts(
        vehicle_quantity=None if vehicle_quantity is None else int(vehicle_quantity),
        po_weight=None if po_weight is None else float(po_weight),
        received_lorries=int(m.get("received_lorries") or 0),
        received_weight=float(m.get("received_weight") or 0.0),
        returned_lorries=int(m.get("returned_lorries") or 0),
        returned_weight=float(m.get("returned_weight") or 0.0),
    )


def _log(db: Session, po_id: int, old_status, new_status: int, close_type, source: str,
         trigger_ref, remark, user_id) -> None:
    db.execute(
        _LOG_INSERT_SQL,
        {
            "po_id": int(po_id),
            "old_status_id": old_status,
            "new_status_id": int(new_status),
            "close_type": close_type,
            "source": source,
            "trigger_ref": (trigger_ref or None) and str(trigger_ref)[:100],
            "remark": (remark or None) and str(remark)[:REMARK_MAX_LEN],
            "user_id": user_id,
            "now": now_ist(),
        },
    )


def _prior_status(db: Session, po_id: int) -> int:
    """Status recorded as old_status_id on the latest row that closed the PO;
    DEFAULT_REOPEN_STATUS when there is none or it is not a live status."""
    m = _mapping(db.execute(_PRIOR_STATUS_SQL, {"po_id": int(po_id), "closed": JUTE_PO_CLOSED_STATUS}).fetchone())
    prior = m.get("old_status_id")
    if prior is None or int(prior) not in JUTE_PO_LIVE_STATUSES:
        return DEFAULT_REOPEN_STATUS
    return int(prior)


def _close(db: Session, po_id: int, old_status, close_type: str, remark: str, user_id) -> dict:
    now = now_ist()
    db.execute(
        _CLOSE_SQL,
        {
            "po_id": int(po_id),
            "closed": JUTE_PO_CLOSED_STATUS,
            "close_type": close_type,
            "now": now,
            "user_id": user_id,
            "remark": remark[:REMARK_MAX_LEN],
        },
    )
    return {
        "jute_po_id": int(po_id),
        "old_status_id": old_status,
        "status_id": JUTE_PO_CLOSED_STATUS,
        "close_type": close_type,
        "closed_date": now,
    }


def _reopen(db: Session, po_id: int) -> int:
    prior = _prior_status(db, po_id)
    db.execute(_REOPEN_SQL, {"po_id": int(po_id), "status_id": prior})
    return prior


# =============================================================================
# Engine
# =============================================================================


@dataclass
class PoEvaluation:
    """What the engine saw and decided for one PO (also what the CLI prints)."""
    jute_po_id: int
    status_id: Optional[int]
    close_type: Optional[str]
    receipts: Optional[PoReceipts]
    settled: Optional[bool]
    decision: Decision
    detail: str


def recompute_po_settlement(
    db: Session,
    jute_po_id: int,
    *,
    user_id,
    source: str,
    trigger_ref: Optional[str],
    rule: Optional[SettleRule] = None,
    collector: Optional[List[PoEvaluation]] = None,
) -> Decision:
    """Bring one PO's AUTO settlement in line with its current receipts.

    Rules, in order (spec §4):
      1. not found / status in (2, 4, 6, 17, 20, 21) -> skipped
      2. CLOSED with a sticky close_type (SHORT, BULK, MIGRATED) -> skipped
      3. compute receipts
      4. live and settled -> close AUTO, log -> closed
      5. CLOSED AUTO and no longer settled -> reopen to the prior status, log -> reopened
      6. otherwise unchanged, nothing written (idempotent)
    """
    po = _mapping(db.execute(_PO_SQL, {"po_id": int(jute_po_id)}).fetchone())

    def _done(decision: Decision, detail: str, receipts=None, settled=None) -> Decision:
        if collector is not None:
            collector.append(PoEvaluation(
                jute_po_id=int(jute_po_id),
                status_id=po.get("status_id"),
                close_type=po.get("close_type"),
                receipts=receipts,
                settled=settled,
                decision=decision,
                detail=detail,
            ))
        return decision

    if not po:
        return _done("skipped", "PO not found")
    status = po.get("status_id")
    close_type = po.get("close_type")
    if status is None or int(status) in JUTE_PO_SETTLEMENT_SKIPPED_STATUSES:
        return _done("skipped", f"status {status} is not live")
    status = int(status)
    if status not in JUTE_PO_LIVE_STATUSES and status != JUTE_PO_CLOSED_STATUS:
        return _done("skipped", f"status {status} is not live")
    if status == JUTE_PO_CLOSED_STATUS and close_type in STICKY_CLOSE_TYPES:
        return _done("skipped", f"sticky {close_type} close")

    if rule is None:
        rule = load_rule(db, po.get("co_id"))
    receipts = _load_receipts(db, jute_po_id, po.get("vehicle_quantity"), po.get("weight"))
    settled = is_settled(receipts, rule)

    if status in JUTE_PO_LIVE_STATUSES and settled:
        remark = f"Auto-settled: {receipts.summary()}"
        _close(db, jute_po_id, status, CLOSE_TYPE_AUTO, remark, user_id)
        _log(db, jute_po_id, status, JUTE_PO_CLOSED_STATUS, CLOSE_TYPE_AUTO, source, trigger_ref, remark, user_id)
        return _done("closed", remark, receipts, settled)

    if status == JUTE_PO_CLOSED_STATUS and close_type == CLOSE_TYPE_AUTO and not settled:
        prior = _reopen(db, jute_po_id)
        remark = f"Reopened: receipts no longer settle the PO ({receipts.summary()})"
        _log(db, jute_po_id, JUTE_PO_CLOSED_STATUS, prior, None, source, trigger_ref, remark, user_id)
        return _done("reopened", f"{remark} -> status {prior}", receipts, settled)

    return _done("unchanged", receipts.summary(), receipts, settled)


def settle_after_mr_change(
    db: Session,
    jute_mr_id: int,
    *,
    user_id,
    trigger_ref: str,
    previous_po_id: Optional[int] = None,
) -> None:
    """Hook for every MR / gate-entry endpoint: recompute the MR's PO (and the
    PO it was moved away from). Runs in the caller's transaction.

    Tolerates a tenant that has not had the settlement migration (logs a
    warning and leaves the MR change alone) so MR operations never break on
    an un-migrated tenant.
    """
    try:
        row = _mapping(db.execute(_MR_PO_SQL, {"mr_id": int(jute_mr_id)}).fetchone())
        po_ids = {row.get("po_id"), previous_po_id} - {None}
        for po_id in sorted(int(p) for p in po_ids):
            recompute_po_settlement(
                db, po_id, user_id=user_id, source=SOURCE_AUTO, trigger_ref=trigger_ref
            )
    except Exception as exc:
        if _is_missing_schema_error(exc):
            logger.warning(
                "PO settlement skipped for MR %s (%s): settlement schema missing — "
                "run dbqueries/migrations/2026-09-25_jute_po_settlement.sql",
                jute_mr_id, trigger_ref,
            )
            return
        raise


def recompute_branch(
    db: Session,
    co_id: int,
    branch_id: int,
    *,
    user_id,
    source: str = SOURCE_BACKEND,
    trigger_ref: Optional[str],
    collector: Optional[List[PoEvaluation]] = None,
) -> dict:
    """Recompute every live + AUTO-closed PO of a branch. Returns the counts."""
    rule = load_rule(db, co_id)
    rows = db.execute(
        _BRANCH_CANDIDATES_SQL,
        {"co_id": int(co_id), "branch_id": int(branch_id), "closed": JUTE_PO_CLOSED_STATUS, "auto": CLOSE_TYPE_AUTO},
    ).fetchall()
    counts = {"closed": 0, "reopened": 0, "unchanged": 0}
    for r in rows:
        decision = recompute_po_settlement(
            db, int(r._mapping["jute_po_id"]), user_id=user_id, source=source,
            trigger_ref=trigger_ref, rule=rule, collector=collector,
        )
        counts["unchanged" if decision == "skipped" else decision] += 1
    return counts


# =============================================================================
# Manual actions (used by the jutePO endpoints)
# =============================================================================


def manual_close_po(
    db: Session,
    jute_po_id: int,
    *,
    co_id: int,
    user_id,
    remark: str,
    close_type: str = CLOSE_TYPE_SHORT,
    source: str = SOURCE_MANUAL,
    trigger_ref: Optional[str] = None,
    branch_id: Optional[int] = None,
) -> dict:
    """Short-close a live PO. Returns {"ok": True, **close fields} or
    {"ok": False, "reason": str, "not_found": bool}. No commit."""
    po = _mapping(db.execute(_PO_SQL, {"po_id": int(jute_po_id)}).fetchone())
    if not po or int(po.get("co_id") or -1) != int(co_id) or (
        branch_id is not None and int(po.get("branch_id") or -1) != int(branch_id)
    ):
        return {"ok": False, "not_found": True, "reason": "Jute PO not found"}
    status = po.get("status_id")
    if status is None or int(status) not in JUTE_PO_LIVE_STATUSES:
        return {
            "ok": False,
            "not_found": False,
            "reason": f"Only Open or Approved POs can be closed (status {status})",
        }
    result = _close(db, jute_po_id, int(status), close_type, remark, user_id)
    _log(db, jute_po_id, int(status), JUTE_PO_CLOSED_STATUS, close_type, source, trigger_ref, remark, user_id)
    result["ok"] = True
    return result


def reopen_closed_po(db: Session, jute_po_id: int, *, user_id, trigger_ref: Optional[str] = None) -> int:
    """Reopen a CLOSED PO (any close_type) to the status it had before the
    close (fallback 3), clear the close columns, log MANUAL. Returns the
    restored status. Caller has already checked status_id == 5. No commit."""
    prior = _reopen(db, jute_po_id)
    _log(db, jute_po_id, JUTE_PO_CLOSED_STATUS, prior, None, SOURCE_MANUAL, trigger_ref,
         "Reopened by user", user_id)
    return prior


def get_status_log(db: Session, jute_po_id: int) -> List[dict]:
    """Status log rows of a PO, newest first, with status and user names."""
    rows = db.execute(_STATUS_LOG_SQL, {"po_id": int(jute_po_id)}).fetchall()
    return [dict(r._mapping) for r in rows]


CLOSE_INFO_KEYS = ("closed_date", "closed_by", "closed_by_name", "close_type", "close_remark")

_CLOSE_INFO_SQL = text("""
    SELECT jp.closed_date, jp.closed_by, um.name AS closed_by_name, jp.close_type, jp.close_remark
    FROM jute_po jp
    LEFT JOIN user_mst um ON um.user_id = jp.closed_by
    WHERE jp.jute_po_id = :po_id
""")

_RETURNED_LORRIES_SQL = text(f"""
    SELECT COUNT(*) AS returned_lorries
    FROM jute_mr jm
    WHERE jm.po_id = :po_id
      AND jm.jute_gate_entry_no IS NOT NULL
      AND jm.is_returned = 1
      AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL})
""")


def get_po_close_info(db: Session, jute_po_id: int) -> dict:
    """The PO's close columns plus the closer's display name and the count of
    returned lorries (returned MRs with a gate entry), for the PO page's
    Closed / summary panel. Close columns are None on a tenant without the
    settlement migration (kept out of get_jute_po_by_id_query so that
    endpoint keeps working there); returned_lorries only needs jute_mr, so it
    is 0 on a tenant that has no status 48 yet."""
    info = {k: None for k in CLOSE_INFO_KEYS}
    try:
        row = db.execute(_CLOSE_INFO_SQL, {"po_id": int(jute_po_id)}).fetchone()
        m = _mapping(row)
        info.update({k: m.get(k) for k in CLOSE_INFO_KEYS})
    except Exception as exc:
        if not _is_missing_schema_error(exc):
            raise
    returned = _mapping(db.execute(_RETURNED_LORRIES_SQL, {"po_id": int(jute_po_id)}).fetchone())
    info["returned_lorries"] = int(returned.get("returned_lorries") or 0)
    return info


# =============================================================================
# Edit-access guard for the manual actions (close / bulk close / reopen closed /
# recompute / rule save). The sidebar hides these buttons behind
# hasMenuAccess(path, "edit"); this is the server-side half of that check.
# =============================================================================

# The Jute PO menu — used when the caller does not say which page it acts from.
DEFAULT_PO_MENU_PATH = "jutePurchase/po"

_MENU_BY_PATH_SQL = text(
    "SELECT menu_id FROM menu_mst "
    "WHERE active = 1 AND LOWER(TRIM(BOTH '/' FROM menu_path)) = :path "
    "ORDER BY menu_id LIMIT 1"
)


def normalise_menu_path(path: Optional[str]) -> str:
    """'/dashboardportal/jutePurchase/reports/x/' -> 'jutepurchase/reports/x'."""
    cleaned = (path or "").strip().strip("/")
    if cleaned.lower().startswith("dashboardportal/"):
        cleaned = cleaned[len("dashboardportal/"):]
    return cleaned.strip("/").lower()


def resolve_menu_id(db: Session, menu_path: Optional[str]) -> Optional[int]:
    """menu_mst.menu_id for the page path, walking up the path segments the same
    way the sidebar's hasMenuAccess does (a report page without its own menu row
    resolves to its hub). None when nothing matches."""
    path = normalise_menu_path(menu_path)
    while path:
        row = db.execute(_MENU_BY_PATH_SQL, {"path": path}).fetchone()
        if row:
            return int(row._mapping["menu_id"])
        path = path.rsplit("/", 1)[0] if "/" in path else ""
    return None


def edit_access_check(db: Session, user_id: Optional[int], branch_ids, menu_path: Optional[str]) -> bool:
    """True when the user holds edit access (access_type_id >= 4) on the menu the
    page path resolves to, for at least one of ``branch_ids``. Falls back to the
    Jute PO menu when the path resolves to nothing."""
    from src.common.approval_utils import _user_has_edit_access  # local: avoids an import cycle

    if not user_id:
        return False
    menu_id = resolve_menu_id(db, menu_path) or resolve_menu_id(db, DEFAULT_PO_MENU_PATH)
    if menu_id is None:
        return False
    for branch_id in branch_ids:
        if branch_id is None:
            continue
        if _user_has_edit_access(int(user_id), int(branch_id), menu_id, db):
            return True
    return False

