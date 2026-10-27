"""Jute PO number allocation guard (2026-10-03).

Jute POs are numbered ``MAX(po_no) + 1`` per branch within the April-March
financial year of ``po_date`` and printed as
``{co_prefix}/{branch_prefix}/JPO/{YY-YY}/{po_no:05d}``. The ERP
(``jutePO.py``) and the sls-only transfer app (``juteTransfer``,
``src/jutetransfer/po_ops.py``) both write ``jute_po`` rows with that rule in
their own transactions, so two saves in the same second ended with one printed
number on two POs, silently. Two layers now stop that:

1. **A MySQL named lock** around "read MAX + insert + commit"
   (``PoNumberLock`` below). User locks are server-wide, so they serialise
   every application that follows the protocol, with no schema change.
2. **A unique key** ``UNIQUE (branch_id, fy_start_year, po_no)`` on
   ``jute_po`` (``dbqueries/migrations/2026-10-03b_jute_po_no_unique.sql``,
   ``fy_start_year`` is a STORED generated column derived from ``po_date``).
   It is the hard guarantee against writers that skip the lock. The save
   paths treat its duplicate-key error (``is_duplicate_po_no_error``) as
   "take the next number and retry once".

LOCK PROTOCOL (the transfer app must copy this exactly)
=======================================================

Lock name   ``CONCAT('jute_po_no:', DATABASE(), ':', <branch_id>)``
            e.g. ``jute_po_no:sls:29``. DATABASE() keeps tenants on one MySQL
            server apart (user locks are server-wide, not per schema).
Timeout     ``SELECT GET_LOCK(<name>, 5)`` -- 5 seconds. Proceed only on 1.
            0 = another writer still holds it after 5 s: abort the save and
            let the user retry (the ERP answers HTTP 409). NULL = error.
            Never allocate a number without the lock.
Fresh read  The ``SELECT COALESCE(MAX(po_no), 0) + 1 FROM jute_po WHERE
            branch_id = ? AND po_date BETWEEN <FY start> AND <FY end>`` must
            see every PO committed before the lock was granted. Under
            REPEATABLE READ (the server default) a transaction keeps the
            snapshot of its first read, so one of: (a) take the lock before the
            writing transaction's first read, (b) run the MAX on a connection
            whose transaction starts after GET_LOCK (the ERP uses the lock
            connection itself and rolls it back after the read), or (c) make
            it a locking read (``... FOR UPDATE``) inside an older transaction.
            A second PO for the same branch and FY inside one transaction must
            count the first one ((a) and (c) do, (b) needs the caller to).
Order       GET_LOCK -> read MAX -> INSERT -> COMMIT -> RELEASE_LOCK.
Release     ``SELECT RELEASE_LOCK(<name>)`` only AFTER the COMMIT (or after
            the ROLLBACK when the save failed), on the same connection that
            took the lock -- user locks belong to the connection, ROLLBACK does
            not free them, and a pooled connection returned to the pool while
            still holding one blocks every later save of that branch. So never
            take it on a connection a context manager closes before you can
            release it (juteTransfer's get_transaction() does): use a second
            connection around the whole transaction. If the lock connection is
            lost the server frees the lock by itself.
Several     When one transaction needs numbers for more than one branch, take
branches    the locks in ascending branch_id order and release them all after
            the commit (MySQL detects user-lock deadlocks, error 3058).
Not the     Nothing else takes this lock: approvals, settlement and the PO
scope       update endpoint only lock when they re-allocate a number.

The ERP takes the lock on a dedicated connection from the tenant engine
(``PoNumberLock``), reads the number there, inserts/commits through the request
session and releases in ``__exit__``. With a MagicMock session (unit tests)
every step degrades to no-ops.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional, TypeVar

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.juteProcurement.formatters import get_financial_year

T = TypeVar("T")

logger = logging.getLogger(__name__)

LOCK_PREFIX = "jute_po_no"
LOCK_TIMEOUT_SEC = 5
UNIQUE_KEY_NAME = "uq_jute_po_branch_fy_no"
MYSQL_DUPLICATE_ENTRY = 1062

LOCK_BUSY_MSG = (
    "Another jute PO is being numbered for this branch right now. "
    "Please save again in a moment."
)
LOCK_ERROR_MSG = "Could not take the PO numbering lock (database error)."
PO_NO_CLASH_MSG = (
    "The PO number was taken by another save at the same moment. "
    "Please save again."
)

GET_LOCK_SQL = text(
    f"SELECT GET_LOCK(CONCAT('{LOCK_PREFIX}:', DATABASE(), ':', :branch_id), :timeout)"
)
RELEASE_LOCK_SQL = text(
    f"SELECT RELEASE_LOCK(CONCAT('{LOCK_PREFIX}:', DATABASE(), ':', :branch_id))"
)
# Same rule and the same po_date window as the transfer app's _next_po_no, so
# both keep working on tenants that do not have the fy_start_year column yet.
NEXT_PO_NO_SQL = text(
    """
    SELECT COALESCE(MAX(po_no), 0) + 1 AS next_po
    FROM jute_po
    WHERE branch_id = :branch_id
      AND po_date >= :fy_start
      AND po_date <= :fy_end
    """
)


def fy_bounds(po_date: date) -> tuple[date, date]:
    """(April 1, March 31) of the financial year po_date falls in."""
    start_year, end_year = get_financial_year(po_date)
    return date(start_year, 4, 1), date(end_year, 3, 31)


def fy_start_year(po_date: Optional[date]) -> Optional[int]:
    """The FY start year of po_date -- what the fy_start_year column stores
    (None when there is no date, like the column)."""
    if not isinstance(po_date, date):
        return None
    return get_financial_year(po_date)[0]


def po_number_key_changed(
    old_branch_id: Optional[int],
    old_po_date: Optional[date],
    new_branch_id: Optional[int],
    new_po_date: Optional[date],
) -> bool:
    """True when an edit moves a PO into another (branch, financial year)
    sequence, i.e. its po_no has to be re-allocated there."""
    if old_branch_id != new_branch_id:
        return True
    return fy_start_year(old_po_date) != fy_start_year(new_po_date)


def is_duplicate_po_no_error(exc: BaseException) -> bool:
    """True for MySQL 1062 on the (branch_id, fy_start_year, po_no) key.

    pymysql raises ``IntegrityError(1062, "Duplicate entry '2-2026-47' for key
    'jute_po.uq_jute_po_branch_fy_no'")``, SQLAlchemy wraps it with the
    original in ``.orig``. Any other integrity error is not ours.
    """
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        args = getattr(cur, "args", ()) or ()
        code = args[0] if args else None
        if code == MYSQL_DUPLICATE_ENTRY and UNIQUE_KEY_NAME in str(cur):
            return True
        cur = getattr(cur, "orig", None) or cur.__cause__
    return False


class PoNumberLock:
    """``with PoNumberLock(db, branch_id) as lock: no = lock.next_po_no(po_date)``

    Holds the branch's named lock on a dedicated connection taken from the
    session's engine, from ``__enter__`` until ``__exit__`` -- which the caller
    reaches only after ``db.commit()`` (or the failing rollback), so the lock
    outlives the commit as the protocol requires. ``next_po_no`` reads on that
    connection and ends its transaction straight away, so a retry after a
    duplicate-key error sees the row that beat us.
    """

    def __init__(self, db, branch_id: int):
        self.db = db
        self.branch_id = int(branch_id)
        self.conn = None

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "PoNumberLock":
        bind = self.db.get_bind()
        engine = getattr(bind, "engine", bind)
        self.conn = engine.connect()
        try:
            got = self.conn.execute(
                GET_LOCK_SQL,
                {"branch_id": self.branch_id, "timeout": LOCK_TIMEOUT_SEC},
            ).scalar()
        except Exception:
            self._drop_connection()
            raise
        if got is None:
            self._drop_connection()
            raise HTTPException(status_code=500, detail=LOCK_ERROR_MSG)
        if int(got) != 1:
            self._drop_connection()
            logger.warning(
                "jute_po numbering lock busy for branch %s after %ss",
                self.branch_id, LOCK_TIMEOUT_SEC,
            )
            raise HTTPException(status_code=409, detail=LOCK_BUSY_MSG)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False  # never swallow the caller's exception

    # -- API ----------------------------------------------------------------

    def next_po_no(self, po_date: date) -> int:
        """MAX(po_no) + 1 for the locked branch in po_date's FY, read in a
        fresh snapshot on the lock connection."""
        fy_start, fy_end = fy_bounds(po_date)
        value = self.conn.execute(
            NEXT_PO_NO_SQL,
            {"branch_id": self.branch_id, "fy_start": fy_start, "fy_end": fy_end},
        ).scalar()
        # End the read's transaction: the next read (a retry) must not reuse
        # this snapshot. User locks survive ROLLBACK.
        self.conn.rollback()
        return int(value) if value is not None else 1

    def release(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(RELEASE_LOCK_SQL, {"branch_id": self.branch_id})
            self.conn.rollback()
            self.conn.close()
        except Exception:
            # Could not release cleanly: drop the DBAPI connection instead of
            # returning it to the pool, so the server frees the lock.
            logger.exception("jute_po numbering lock release failed; dropping connection")
            self._drop_connection()
        finally:
            self.conn = None

    # -- internals -----------------------------------------------------------

    def _drop_connection(self) -> None:
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            conn.invalidate()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def commit_with_po_no(
    db,
    lock: PoNumberLock,
    po_date: date,
    apply_fn: Callable[[int], T],
) -> tuple[T, int]:
    """Allocate a number under ``lock``, let ``apply_fn(po_no)`` stage the
    ORM writes (it may flush), then commit.

    If the commit/flush hits the unique key's duplicate error -- a writer that
    skipped the lock got there first -- roll back, read the next number and
    run ``apply_fn`` once more. A second clash surfaces as HTTP 409 so the user
    simply saves again. Any other integrity error is re-raised untouched.
    Returns ``(apply_fn's result, po_no)``.
    """
    for attempt in (1, 2):
        next_po_no = lock.next_po_no(po_date)
        try:
            result = apply_fn(next_po_no)
            db.commit()
            return result, next_po_no
        except IntegrityError as exc:
            db.rollback()
            if not is_duplicate_po_no_error(exc):
                raise
            if attempt == 1:
                logger.warning(
                    "jute_po number %s clashed for branch %s (unique key); retrying once",
                    next_po_no, lock.branch_id,
                )
                continue
            raise HTTPException(status_code=409, detail=PO_NO_CLASH_MSG) from exc
        except Exception:
            # Roll back while the lock is still held (protocol: release only
            # after the transaction has ended one way or the other).
            db.rollback()
            raise
    raise AssertionError("unreachable")  # pragma: no cover
