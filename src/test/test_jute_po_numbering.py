"""Tests for the jute PO numbering guard (src/juteProcurement/po_numbering.py).

Two saves in the same second used to print one PO number on two POs: the
number is MAX(po_no) + 1 per branch and financial year with no lock, and the
sls transfer app writes jute_po rows with the same rule. The guard is

* a MySQL named lock ``jute_po_no:<db>:<branch_id>`` held on a dedicated
  connection from GET_LOCK through the commit to RELEASE_LOCK (PoNumberLock),
  with the MAX read on that connection in a fresh snapshot, and
* UNIQUE (branch_id, fy_start_year, po_no) as the backstop, whose duplicate
  error makes the save take the next number once more (commit_with_po_no).

No database: the lock connection, the engine and the session are scripted
fakes that record every call, so the assertions are on the protocol (order of
GET_LOCK / MAX / INSERT / COMMIT / RELEASE_LOCK, parameters, retry) and on
what the two endpoints (create, update-with-renumber) return.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pymysql
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import mysql

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement import po_numbering as pn
from src.juteProcurement.po_numbering import (
    LOCK_TIMEOUT_SEC,
    PoNumberLock,
    commit_with_po_no,
    fy_bounds,
    fy_start_year,
    is_duplicate_po_no_error,
    po_number_key_changed,
)
from src.main import app

client = TestClient(app)
PREFIX = "/api/jutePO"

DUP_MSG = "Duplicate entry '2-2026-47' for key 'jute_po.uq_jute_po_branch_fy_no'"


def duplicate_error(msg: str = DUP_MSG, code: int = 1062) -> IntegrityError:
    """The error SQLAlchemy raises when MySQL rejects the insert on the key."""
    orig = pymysql.err.IntegrityError(code, msg)
    return IntegrityError("INSERT INTO jute_po ...", {}, orig)


# =============================================================================
# Fakes
# =============================================================================


class Row:
    def __init__(self, **kw):
        self._mapping = kw

    def __getattr__(self, name):
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(name)


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self._scalar = scalar

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def scalar(self):
        return self._scalar


class FakeLockConn:
    """The dedicated connection PoNumberLock takes from the engine."""

    def __init__(self, events, max_values=(1,), got=1, release_raises=False):
        self.events = events
        self.max_values = list(max_values)
        self.got = got
        self.release_raises = release_raises
        self.calls = []
        self.closed = False
        self.invalidated = False

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = dict(params or {})
        self.calls.append((sql, params))
        if "GET_LOCK" in sql:
            self.events.append(("get_lock", params))
            return Result(scalar=self.got)
        if "RELEASE_LOCK" in sql:
            self.events.append(("release_lock", params))
            if self.release_raises:
                raise RuntimeError("connection gone")
            return Result(scalar=1)
        if "MAX(po_no)" in sql:
            value = self.max_values.pop(0)
            self.events.append(("max", params, value))
            return Result(scalar=value)
        raise AssertionError(f"unexpected SQL on lock connection: {sql[:80]}")

    def rollback(self):
        self.events.append(("lock_rollback",))

    def close(self):
        self.closed = True
        self.events.append(("lock_close",))

    def invalidate(self):
        self.invalidated = True
        self.events.append(("lock_invalidate",))


class FakeEngine:
    def __init__(self, lock_conn):
        self.lock_conn = lock_conn
        self.connects = 0

    def connect(self):
        self.connects += 1
        return self.lock_conn


class _Query:
    def __init__(self, db):
        self.db = db

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.db.orm_po

    def delete(self):
        self.db.deleted_line_sets += 1
        return 1


class FakeDb:
    """Scripted stand-in for the request session used by jute_po_create /
    jute_po_update: routes each text() statement by a fragment of its SQL,
    records adds / flushes / commits / rollbacks, raises the duplicate-key
    error on the first ``fail_flushes`` flushes, and hands PoNumberLock the
    fake engine through get_bind()."""

    def __init__(self, events, lock_conn, fail_flushes=0, fail_commits=0, branches=(2,), orm_po=None):
        self.events = events
        self.engine = FakeEngine(lock_conn)
        self.fail_flushes = fail_flushes
        self.fail_commits = fail_commits
        self.branches = list(branches)
        self.orm_po = orm_po
        self.added = []
        self.flushes = 0
        self.committed = 0
        self.rolled_back = 0
        self.deleted_line_sets = 0
        self.queries = []

    # -- Session API --------------------------------------------------------

    def get_bind(self):
        return self.engine

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = dict(params or {})
        self.queries.append((sql, params))
        if "FROM branch_mst WHERE branch_id" in sql:
            return Result([Row(co_id=1)])
        if "jute_supp_party_map" in sql:
            return Result([Row(ok=1)])
        if "jute_lorry_mst" in sql:
            return Result([Row(weight=100.0)])
        if "co_prefix" in sql:
            return Result([Row(co_prefix="CO", branch_prefix="BR")])
        if "SELECT branch_id FROM branch_mst WHERE co_id" in sql:
            return Result([Row(branch_id=b) for b in self.branches])
        if "MAX(po_no)" in sql:
            raise AssertionError("MAX(po_no) must be read on the lock connection, not the session")
        raise AssertionError(f"unexpected SQL on session: {sql[:80]}")

    def query(self, *a, **k):
        return _Query(self)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushes += 1
        if self.flushes <= self.fail_flushes:
            self.events.append(("flush_dup",))
            raise duplicate_error()
        for n, obj in enumerate(self.added, start=1):
            if type(obj).__name__ == "JutePo" and getattr(obj, "jute_po_id", None) is None:
                obj.jute_po_id = 500 + n
        self.events.append(("flush",))

    def commit(self):
        # An UPDATE that violates the key fails at commit (flush-on-commit).
        if self.fail_commits > 0:
            self.fail_commits -= 1
            self.events.append(("commit_dup",))
            raise duplicate_error("Duplicate entry '2-2026-12' for key 'jute_po.uq_jute_po_branch_fy_no'")
        self.committed += 1
        self.events.append(("commit",))

    def rollback(self):
        self.rolled_back += 1
        self.events.append(("rollback",))

    # -- helpers -------------------------------------------------------------

    def headers(self):
        return [o for o in self.added if type(o).__name__ == "JutePo"]

    def lines(self):
        return [o for o in self.added if type(o).__name__ == "JutePoLi"]


def _event_names(events):
    return [e[0] for e in events]


def _index(events, name, nth=0):
    hits = [i for i, e in enumerate(events) if e[0] == name]
    return hits[nth]


# =============================================================================
# Pure helpers
# =============================================================================


class TestFinancialYearHelpers:
    @pytest.mark.parametrize(
        "po_date, start, end",
        [
            (date(2026, 4, 1), date(2026, 4, 1), date(2027, 3, 31)),
            (date(2027, 3, 31), date(2026, 4, 1), date(2027, 3, 31)),
            (date(2026, 3, 31), date(2025, 4, 1), date(2026, 3, 31)),
            (date(2025, 1, 20), date(2024, 4, 1), date(2025, 3, 31)),
        ],
    )
    def test_fy_bounds(self, po_date, start, end):
        assert fy_bounds(po_date) == (start, end)

    def test_fy_start_year_matches_the_generated_column(self):
        # IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)
        assert fy_start_year(date(2026, 4, 1)) == 2026
        assert fy_start_year(date(2026, 3, 31)) == 2025
        assert fy_start_year(datetime(2026, 12, 1, 10, 0)) == 2026
        assert fy_start_year(None) is None
        assert fy_start_year("2026-04-01") is None  # not a date: no FY, like the column

    def test_key_changed_on_branch_or_fy_only(self):
        same = (2, date(2026, 5, 1))
        assert po_number_key_changed(*same, 2, date(2026, 11, 30)) is False  # same FY
        assert po_number_key_changed(*same, 2, date(2027, 3, 31)) is False   # FY end
        assert po_number_key_changed(*same, 2, date(2027, 4, 1)) is True     # next FY
        assert po_number_key_changed(2, date(2026, 3, 31), 2, date(2026, 4, 1)) is True
        assert po_number_key_changed(*same, 3, date(2026, 5, 1)) is True     # other branch
        assert po_number_key_changed(2, None, 2, date(2026, 5, 1)) is True   # gains a date
        assert po_number_key_changed(2, None, 2, None) is False


class TestIsDuplicatePoNoError:
    def test_our_key_wrapped_by_sqlalchemy(self):
        assert is_duplicate_po_no_error(duplicate_error()) is True

    def test_bare_pymysql_error(self):
        assert is_duplicate_po_no_error(pymysql.err.IntegrityError(1062, DUP_MSG)) is True

    def test_other_unique_key_is_not_ours(self):
        other = "Duplicate entry '5' for key 'jute_po.PRIMARY'"
        assert is_duplicate_po_no_error(duplicate_error(other)) is False

    def test_other_errno_is_not_ours(self):
        assert is_duplicate_po_no_error(duplicate_error(DUP_MSG, code=1452)) is False
        assert is_duplicate_po_no_error(RuntimeError(DUP_MSG)) is False

    def test_follows_cause_chain(self):
        try:
            try:
                raise pymysql.err.IntegrityError(1062, DUP_MSG)
            except pymysql.err.IntegrityError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            assert is_duplicate_po_no_error(outer) is True


# =============================================================================
# PoNumberLock protocol
# =============================================================================


class TestPoNumberLock:
    def test_lock_name_timeout_and_order(self):
        events = []
        conn = FakeLockConn(events, max_values=[47])
        db = FakeDb(events, conn)
        with PoNumberLock(db, 2) as lock:
            assert lock.next_po_no(date(2026, 4, 13)) == 47
            db.commit()

        get_sql, get_params = conn.calls[0]
        assert "GET_LOCK(CONCAT('jute_po_no:', DATABASE(), ':', :branch_id), :timeout)" in get_sql
        assert get_params == {"branch_id": 2, "timeout": LOCK_TIMEOUT_SEC}
        assert LOCK_TIMEOUT_SEC == 5

        max_sql, max_params = conn.calls[1]
        assert "COALESCE(MAX(po_no), 0) + 1" in max_sql
        assert "branch_id = :branch_id" in max_sql
        assert "po_date >= :fy_start" in max_sql and "po_date <= :fy_end" in max_sql
        assert max_params == {"branch_id": 2, "fy_start": date(2026, 4, 1), "fy_end": date(2027, 3, 31)}

        rel_sql, rel_params = conn.calls[2]
        assert "RELEASE_LOCK(CONCAT('jute_po_no:', DATABASE(), ':', :branch_id))" in rel_sql
        assert rel_params == {"branch_id": 2}

        names = _event_names(events)
        # GET_LOCK -> MAX (then the read's snapshot is dropped) -> COMMIT -> RELEASE -> close
        assert names.index("get_lock") < names.index("max") < names.index("commit") < names.index("release_lock")
        assert names[names.index("max") + 1] == "lock_rollback"
        assert conn.closed and not conn.invalidated
        assert db.engine.connects == 1

    def test_busy_lock_is_409_and_drops_the_connection(self):
        events = []
        conn = FakeLockConn(events, got=0)
        db = FakeDb(events, conn)
        with pytest.raises(HTTPException) as exc:
            with PoNumberLock(db, 29):
                pass
        assert exc.value.status_code == 409
        assert "numbered" in exc.value.detail
        assert conn.invalidated is True
        assert "release_lock" not in _event_names(events)  # we never owned it

    def test_lock_error_is_500(self):
        events = []
        conn = FakeLockConn(events, got=None)
        db = FakeDb(events, conn)
        with pytest.raises(HTTPException) as exc:
            with PoNumberLock(db, 29):
                pass
        assert exc.value.status_code == 500
        assert conn.invalidated is True

    def test_released_when_the_body_raises(self):
        events = []
        conn = FakeLockConn(events, max_values=[3])
        db = FakeDb(events, conn)
        with pytest.raises(ValueError):
            with PoNumberLock(db, 2) as lock:
                lock.next_po_no(date(2026, 6, 1))
                raise ValueError("save failed")
        assert "release_lock" in _event_names(events)
        assert conn.closed

    def test_release_failure_drops_the_pooled_connection(self):
        # A pooled connection that still holds the user lock would block every
        # later save of the branch; invalidate() makes the server free it.
        events = []
        conn = FakeLockConn(events, release_raises=True)
        db = FakeDb(events, conn)
        with PoNumberLock(db, 2):
            pass
        assert conn.invalidated is True

    def test_empty_table_starts_at_one(self):
        events = []
        conn = FakeLockConn(events, max_values=[None])
        with PoNumberLock(FakeDb(events, conn), 2) as lock:
            assert lock.next_po_no(date(2026, 6, 1)) == 1


# =============================================================================
# commit_with_po_no (retry on the unique key)
# =============================================================================


class TestCommitWithPoNo:
    def _run(self, max_values, fail_flushes, apply_raises=None):
        events = []
        conn = FakeLockConn(events, max_values=list(max_values))
        db = FakeDb(events, conn, fail_flushes=fail_flushes)
        seen = []

        def apply_fn(po_no):
            seen.append(po_no)
            if apply_raises is not None:
                raise apply_raises
            db.flush()
            return f"po-{po_no}"

        with PoNumberLock(db, 2) as lock:
            out = commit_with_po_no(db, lock, date(2026, 4, 13), apply_fn)
        return out, seen, db, events

    def test_happy_path_allocates_once(self):
        out, seen, db, events = self._run([47], fail_flushes=0)
        assert out == ("po-47", 47)
        assert seen == [47]
        assert db.committed == 1 and db.rolled_back == 0

    def test_duplicate_once_retries_with_a_fresh_number(self):
        out, seen, db, events = self._run([47, 48], fail_flushes=1)
        assert out == ("po-48", 48)
        assert seen == [47, 48]
        assert db.rolled_back == 1 and db.committed == 1
        names = _event_names(events)
        # rollback, then a second MAX read, then the successful flush + commit; lock held throughout
        assert names.index("flush_dup") < names.index("rollback") < _index(events, "max", 1) < names.index("flush")
        assert names.index("release_lock") > names.index("commit")

    def test_duplicate_twice_is_409(self):
        with pytest.raises(HTTPException) as exc:
            self._run([47, 48], fail_flushes=2)
        assert exc.value.status_code == 409
        assert "save again" in exc.value.detail.lower()

    def test_other_integrity_error_is_not_retried(self):
        fk = IntegrityError("INSERT", {}, pymysql.err.IntegrityError(1452, "foreign key fails"))
        with pytest.raises(IntegrityError):
            self._run([47, 48], fail_flushes=0, apply_raises=fk)


# =============================================================================
# Endpoints
# =============================================================================


def _create_payload(po_date="2026-04-13", branch_id=2):
    return {
        "co_id": 1,
        "branch_id": branch_id,
        "po_date": po_date,
        "mukam_id": 307,
        "jute_unit": "BALE",
        "supplier_id": 857,
        "party_id": None,
        "vehicle_type_id": 1,
        "vehicle_quantity": 2,
        "channel_code": "DOMESTIC",
        "line_items": [
            {"item_id": 269847, "percentage": 60.0, "rate": 10000, "jute_unit": "BALE"},
            {"item_id": 269848, "percentage": 40.0, "rate": 10000, "jute_unit": "BALE"},
        ],
    }


class TestCreateEndpoint:
    @pytest.fixture(autouse=True)
    def _auth(self):
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        yield
        app.dependency_overrides.clear()

    def _db(self, max_values, fail_flushes=0, got=1):
        events = []
        conn = FakeLockConn(events, max_values=list(max_values), got=got)
        db = FakeDb(events, conn, fail_flushes=fail_flushes)
        app.dependency_overrides[get_tenant_db] = lambda: db
        return db, conn, events

    def test_number_is_read_under_the_lock_and_released_after_commit(self):
        db, conn, events = self._db([47])
        r = client.post(f"{PREFIX}/jute_po_create", json=_create_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["po_num"] == "CO/BR/JPO/26-27/00047"
        assert body["jute_po_id"] == 501

        header = db.headers()[0]
        assert header.po_no == 47 and header.branch_id == 2 and header.status_id == 21
        assert len(db.lines()) == 2 and all(li.jute_po_id == 501 for li in db.lines())

        names = _event_names(events)
        assert names.index("get_lock") < names.index("max") < names.index("flush") \
            < names.index("commit") < names.index("release_lock")
        assert events[names.index("get_lock")][1] == {"branch_id": 2, "timeout": 5}
        assert events[names.index("max")][1]["fy_start"] == date(2026, 4, 1)
        assert db.committed == 1 and db.rolled_back == 0

    def test_duplicate_number_takes_the_next_one(self):
        db, conn, events = self._db([47, 48], fail_flushes=1)
        r = client.post(f"{PREFIX}/jute_po_create", json=_create_payload())
        assert r.status_code == 200, r.text
        assert r.json()["po_num"] == "CO/BR/JPO/26-27/00048"
        assert [h.po_no for h in db.headers()] == [47, 48]
        assert db.rolled_back == 1 and db.committed == 1
        # the lock was held across the retry: one GET_LOCK, one RELEASE, two MAX reads
        names = _event_names(events)
        assert names.count("get_lock") == 1 and names.count("release_lock") == 1
        assert names.count("max") == 2

    def test_second_clash_is_409_not_a_silent_duplicate(self):
        db, conn, events = self._db([47, 48], fail_flushes=2)
        r = client.post(f"{PREFIX}/jute_po_create", json=_create_payload())
        assert r.status_code == 409, r.text
        assert db.committed == 0
        assert "release_lock" in _event_names(events)

    def test_busy_lock_saves_nothing(self):
        db, conn, events = self._db([47], got=0)
        r = client.post(f"{PREFIX}/jute_po_create", json=_create_payload())
        assert r.status_code == 409, r.text
        assert db.added == [] and db.committed == 0
        assert conn.invalidated is True

    def test_validation_errors_happen_before_the_lock(self):
        db, conn, events = self._db([47])
        bad = _create_payload()
        bad["line_items"][0]["percentage"] = 50.0  # sums to 90
        r = client.post(f"{PREFIX}/jute_po_create", json=bad)
        assert r.status_code == 400
        assert "get_lock" not in _event_names(events)


def _orm_po(**over):
    po = SimpleNamespace(
        jute_po_id=12345, status_id=21, branch_id=2, supplier_id=857, party_id=None,
        vehicle_type_id=1, vehicle_quantity=2, jute_uom="BALE",
        po_no=47, po_date=date(2026, 3, 31),
    )
    for k, v in over.items():
        setattr(po, k, v)
    return po


class TestUpdateEndpoint:
    @pytest.fixture(autouse=True)
    def _auth(self):
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        yield
        app.dependency_overrides.clear()

    def _db(self, max_values=(1,), orm_po=None, branches=(2,), fail_commits=0):
        events = []
        conn = FakeLockConn(events, max_values=list(max_values))
        db = FakeDb(events, conn, branches=branches, orm_po=orm_po or _orm_po(),
                    fail_commits=fail_commits)
        app.dependency_overrides[get_tenant_db] = lambda: db
        return db, conn, events

    LINES = [{"item_id": 1, "percentage": 100.0, "rate": 9000, "jute_unit": "BALE"}]

    def test_same_fy_edit_keeps_the_number_and_takes_no_lock(self):
        db, conn, events = self._db()
        r = client.put(f"{PREFIX}/jute_po_update/12345?co_id=1",
                       json={"po_date": "2026-03-01", "remarks": "x", "line_items": self.LINES})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"success": True, "jute_po_id": 12345, "message": "Jute PO updated successfully"}
        assert db.orm_po.po_no == 47 and db.orm_po.po_date == date(2026, 3, 1)
        assert db.engine.connects == 0 and events == [("commit",)]

    def test_date_moved_into_next_fy_renumbers_under_the_lock(self):
        # 31-Mar-2026 (FY 25-26, no 47) -> 01-Apr-2026 (FY 26-27): 47 may already exist there
        db, conn, events = self._db(max_values=[12])
        r = client.put(f"{PREFIX}/jute_po_update/12345?co_id=1",
                       json={"po_date": "2026-04-01", "line_items": self.LINES})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["renumbered"] is True
        assert body["po_no"] == 12
        assert body["po_num"] == "CO/BR/JPO/26-27/00012"
        assert "26-27/00012" in body["message"]
        assert db.orm_po.po_no == 12 and db.orm_po.po_date == date(2026, 4, 1)
        assert db.orm_po.updated_by == 7
        names = _event_names(events)
        assert names.index("get_lock") < names.index("max") < names.index("commit") < names.index("release_lock")
        assert events[names.index("get_lock")][1]["branch_id"] == 2
        assert events[names.index("max")][1] == {"branch_id": 2, "fy_start": date(2026, 4, 1), "fy_end": date(2027, 3, 31)}
        assert db.deleted_line_sets == 1 and len(db.lines()) == 1

    def test_branch_change_renumbers_in_the_new_branch(self):
        db, conn, events = self._db(max_values=[5], branches=(2, 3))
        r = client.put(f"{PREFIX}/jute_po_update/12345?co_id=1", json={"branch_id": 3})
        assert r.status_code == 200, r.text
        assert r.json()["renumbered"] is True
        assert r.json()["po_num"] == "CO/BR/JPO/25-26/00005"
        assert db.orm_po.branch_id == 3 and db.orm_po.po_no == 5
        assert events[_index(events, "get_lock")][1]["branch_id"] == 3

    def test_renumber_clash_at_commit_retries_once(self):
        # A writer without the lock took 12 between our read and our commit:
        # the UPDATE fails on the key at commit, we roll back and re-apply with 13.
        db, conn, events = self._db(max_values=[12, 13], fail_commits=1)
        r = client.put(f"{PREFIX}/jute_po_update/12345?co_id=1",
                       json={"po_date": "2026-04-01", "remarks": "late", "line_items": self.LINES})
        assert r.status_code == 200, r.text
        assert r.json()["po_no"] == 13
        assert r.json()["po_num"] == "CO/BR/JPO/26-27/00013"
        assert db.orm_po.po_no == 13 and db.orm_po.remarks == "late"
        assert db.rolled_back == 1 and db.committed == 1
        names = _event_names(events)
        assert names.count("get_lock") == 1 and names.count("release_lock") == 1
        assert names.count("max") == 2
        assert names.index("commit_dup") < names.index("rollback") < _index(events, "max", 1) < names.index("commit")
        # both apply passes replaced the lines; only the committed pass counts in production
        assert db.deleted_line_sets == 2

    def test_non_editable_status_is_rejected_before_any_lock(self):
        db, conn, events = self._db(orm_po=_orm_po(status_id=3))
        r = client.put(f"{PREFIX}/jute_po_update/12345?co_id=1", json={"po_date": "2026-04-01"})
        assert r.status_code == 400
        assert events == []


# =============================================================================
# Schema side: ORM model and the two migration files
# =============================================================================


class TestSchema:
    def test_orm_never_writes_the_generated_fy_column(self):
        from sqlalchemy import insert, select
        from sqlalchemy.schema import CreateTable
        from src.models.jute import JutePo

        d = mysql.dialect()
        ddl = str(CreateTable(JutePo.__table__).compile(dialect=d))
        assert "fy_start_year SMALLINT GENERATED ALWAYS AS (IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)) STORED" in ddl
        assert "CONSTRAINT uq_jute_po_branch_fy_no UNIQUE (branch_id, fy_start_year, po_no)" in ddl
        ins = insert(JutePo).values(branch_id=2, po_no=1, po_date=date(2026, 5, 1))
        assert "fy_start_year" not in str(ins.compile(dialect=d))
        # deferred: a tenant without the migration can still SELECT the model
        assert "fy_start_year" not in str(select(JutePo).compile(dialect=d))

    @staticmethod
    def _runner_chunks(path):
        # exactly what the run-migration skill executes
        with open(path) as f:
            return [s.strip() for s in f.read().split(";") if s.strip() and not s.strip().startswith("--")]

    def test_dedupe_migration_chunks_like_the_runner(self):
        chunks = self._runner_chunks("dbqueries/migrations/2026-10-03a_jute_po_no_dedupe.sql")
        starts = [c.split()[0] for c in chunks]
        assert starts == ["CREATE", "INSERT", "UPDATE", "SELECT", "SELECT"]
        assert "ROW_NUMBER() OVER (PARTITION BY d.branch_id, d.fy_start_year" in chunks[1]
        assert "WHERE p.po_no = b.old_po_no" in chunks[2]  # idempotent apply

    def test_unique_key_migration_chunks_like_the_runner(self):
        chunks = self._runner_chunks("dbqueries/migrations/2026-10-03b_jute_po_no_unique.sql")
        starts = [c.split()[0] for c in chunks]
        assert starts == ["ALTER", "ALTER", "SELECT", "SELECT"]
        assert "GENERATED ALWAYS AS (IF(MONTH(po_date) >= 4, YEAR(po_date), YEAR(po_date) - 1)) STORED" in chunks[0]
        assert "ADD UNIQUE KEY uq_jute_po_branch_fy_no (branch_id, fy_start_year, po_no)" in chunks[1]
        assert pn.UNIQUE_KEY_NAME == "uq_jute_po_branch_fy_no"
