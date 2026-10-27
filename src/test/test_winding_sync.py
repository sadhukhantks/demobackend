"""Winding "Sync from attendance" (TKT-000171) — POST /api/windingProd/quality_sync.

Winding freezes nothing (every figure is computed at read time), so its Sync is
NOT a reprocess: it reconciles the person -> quality map
(jute_prod_winding_daily_qlty) for one (branch, date, spell) unit against that
unit's attendance — the winding analogue of spinning's attendance fill-sync.

The rules live in the pure planner ``plan_winding_sync`` and are tested on stub
rows; the SQL is run against a throwaway in-memory SQLite schema (the
test_winding_spell_report.py convention) so a typo in a column name fails here,
not on sls; the endpoint wiring (400 / 403 gates, execute order, response
shape) goes through TestClient with the DB + auth dependencies overridden and
the shared spell / permission helpers patched.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProduction import winding_sync_query as sq
from src.juteProduction.winding_sync import (
    BRANCH_REQUIRED_MSG,
    REMOVE_NO_ATTENDANCE_MSG,
    SYNC_EDIT_MSG,
    plan_winding_sync,
)

client = TestClient(app)

MC_41 = {"machine_id": 41, "mech_code": "4101", "machine_name": "WINDING4101", "dtl_rec_id": 1}
MC_42 = {"machine_id": 42, "mech_code": "4102", "machine_name": "WINDING4102", "dtl_rec_id": 2}


def _map_row(qid, eb_id, machine_id=None, item_id=5, emp_code=None):
    return {
        "winding_daily_qlty_id": qid,
        "eb_id": eb_id,
        "machine_id": machine_id,
        "item_id": item_id,
        "no_of_spindle": 10,
        "emp_code": emp_code or f"E{eb_id}",
        "worker_name": f"Winder {eb_id}",
        "mech_code": None if machine_id is None else f"m{machine_id}",
        "machine_name": None if machine_id is None else f"MACHINE {machine_id}",
    }


def _att(*eb_ids):
    return {eb: {"daily_atten_id": 1000 + eb, "att_rows": 1} for eb in eb_ids}


# =============================================================================
# plan_winding_sync — the rules
# =============================================================================


class TestPlanner:
    def test_adds_winder_marked_on_winding_machine_and_doffing_winder(self):
        plan = plan_winding_sync(
            map_rows=[_map_row(1, 101, machine_id=41)],
            att_by_eb=_att(101, 102, 103),
            att_mc_by_eb={101: [MC_41], 102: [MC_42]},
            doff_by_eb={103: {"item_id": 9, "doffs": 3}},
        )
        adds = {a["eb_id"]: a for a in plan["add"]}
        assert set(adds) == {102, 103}
        assert adds[102]["source"] == "attendance"
        assert adds[102]["machine"] == MC_42
        assert adds[102]["item_id"] is None  # carried from the last map row by the endpoint
        assert adds[103]["source"] == "doff"
        assert adds[103]["machine"] is None
        assert adds[103]["item_id"] == 9

    def test_attendance_only_winder_without_winding_machine_is_not_added(self):
        """Present, no winding machine, no doffs -> could be a fitter, a sweeper,
        a spinner: never added (sls: a '%WIND%' designation gate would pull in
        13 non-winders a day)."""
        plan = plan_winding_sync([], _att(201), {}, {})
        assert plan["add"] == []

    def test_fill_sets_blank_machine_and_only_reports_a_differing_one(self):
        rows = [_map_row(1, 101, machine_id=None), _map_row(2, 102, machine_id=41)]
        plan = plan_winding_sync(rows, _att(101, 102), {101: [MC_41], 102: [MC_42]}, {})
        assert [c["row"]["winding_daily_qlty_id"] for c in plan["machine_fill"]] == [1]
        assert plan["machine_update"] == []
        assert [c["row"]["winding_daily_qlty_id"] for c in plan["machine_differs"]] == [2]
        assert plan["machine_differs"][0]["machine"] == MC_42

    def test_force_overwrites_the_differing_machine(self):
        rows = [_map_row(2, 102, machine_id=41)]
        plan = plan_winding_sync(rows, _att(102), {102: [MC_42]}, {}, mode="force")
        assert [c["row"]["winding_daily_qlty_id"] for c in plan["machine_update"]] == [2]
        assert plan["machine_differs"] == []

    def test_same_machine_is_a_no_op(self):
        rows = [_map_row(1, 101, machine_id=41)]
        plan = plan_winding_sync(rows, _att(101), {101: [MC_41]}, {}, mode="force")
        assert plan["machine_fill"] == plan["machine_update"] == plan["machine_differs"] == []

    def test_absent_map_winders_are_reported_with_doff_flag(self):
        rows = [_map_row(1, 101), _map_row(2, 102), _map_row(3, 103)]
        plan = plan_winding_sync(rows, _att(101), {}, {103: {"item_id": 5, "doffs": 2}})
        absent = {a["row"]["eb_id"]: a["has_doffs"] for a in plan["absent"]}
        assert absent == {102: False, 103: True}
        assert plan["remove"] == []  # not asked

    def test_remove_absent_drops_only_rows_without_doffs(self):
        rows = [_map_row(1, 101), _map_row(2, 102), _map_row(3, 103)]
        plan = plan_winding_sync(
            rows, _att(101), {}, {103: {"item_id": 5, "doffs": 2}}, remove_absent=True)
        assert [r["winding_daily_qlty_id"] for r in plan["remove"]] == [2]

    def test_no_attendance_at_all_suppresses_absent_and_remove(self):
        """Attendance is habitually late: an empty unit must not read as
        'everyone absent' nor let remove_absent wipe the carried-forward map."""
        rows = [_map_row(1, 101), _map_row(2, 102)]
        plan = plan_winding_sync(rows, {}, {}, {102: {"item_id": 5, "doffs": 1}},
                                 remove_absent=True)
        assert plan["attendance_missing"] is True
        assert plan["absent"] == []
        assert plan["remove"] == []
        # ... but a doffing winder missing from the map is still added.
        assert [a["eb_id"] for a in plan["add"]] == []  # 102 is on the map
        plan2 = plan_winding_sync([], {}, {}, {102: {"item_id": 5, "doffs": 1}})
        assert [a["eb_id"] for a in plan2["add"]] == [102]

    def test_two_machines_first_marked_wins_and_is_reported(self):
        rows = [_map_row(1, 101, machine_id=None)]
        plan = plan_winding_sync(rows, _att(101), {101: [MC_42, MC_41]}, {})
        assert plan["machine_fill"][0]["machine"] == MC_42
        assert plan["machine_ambiguous"] == [
            {"eb_id": 101, "machine_ids": [41, 42], "used_machine_id": 42}]

    def test_idempotent_second_run_plans_nothing(self):
        """Apply a fill plan by hand, re-plan on the result: nothing left to do."""
        rows = [_map_row(1, 101, machine_id=None)]
        att = _att(101, 102)
        mcs = {101: [MC_41], 102: [MC_42]}
        first = plan_winding_sync(rows, att, mcs, {})
        assert len(first["add"]) == 1 and len(first["machine_fill"]) == 1
        after = [
            _map_row(1, 101, machine_id=41),
            _map_row(2, 102, machine_id=42),
        ]
        second = plan_winding_sync(after, att, mcs, {})
        assert second["add"] == []
        assert second["machine_fill"] == second["machine_differs"] == []
        assert second["absent"] == []


# =============================================================================
# SQL shape — run the real statements on a throwaway SQLite schema
# =============================================================================


_SCHEMA = [
    """CREATE TABLE daily_attendance (daily_atten_id INTEGER, attendance_date DATE,
       spell_id INTEGER, branch_id INTEGER, is_active INTEGER, eb_id INTEGER,
       status_id TEXT)""",
    """CREATE TABLE daily_ebmc_attendance (dtl_rec_id INTEGER, daily_atten_id INTEGER,
       mc_id INTEGER, is_active INTEGER)""",
    """CREATE TABLE machine_mst (machine_id INTEGER, mech_code TEXT, machine_name TEXT,
       machine_type_id INTEGER, dept_id INTEGER, active INTEGER)""",
    """CREATE TABLE machine_type_mst (machine_type_id INTEGER, machine_type_name TEXT,
       active INTEGER)""",
    "CREATE TABLE dept_mst (dept_id INTEGER, branch_id INTEGER)",
    """CREATE TABLE jute_prod_winding_daily_qlty (winding_daily_qlty_id INTEGER PRIMARY KEY,
       co_id INTEGER, branch_id INTEGER, tran_date DATE, spell_id INTEGER, eb_id INTEGER,
       machine_id INTEGER, item_id INTEGER, no_of_spindle INTEGER, active INTEGER,
       updated_by INTEGER)""",
    """CREATE TABLE jute_prod_winding_doff (winding_doff_id INTEGER PRIMARY KEY,
       branch_id INTEGER, tran_date DATE, spell_id INTEGER, eb_id INTEGER,
       item_id INTEGER, active INTEGER)""",
    """CREATE TABLE hrms_ed_personal_details (eb_id INTEGER, first_name TEXT,
       middle_name TEXT, last_name TEXT)""",
    "CREATE TABLE hrms_ed_official_details (eb_id INTEGER, emp_code TEXT, active INTEGER)",
    "CREATE TABLE spell_mst (spell_id INTEGER, spell_code TEXT)",
]

_SEED = [
    # unit = branch 29, 2026-10-07, spell 109; winder 1 on map (machine NULL),
    # winder 2 present on winding machine 42 but not on map, winder 3 absent on
    # map, winder 4 present in spell 110 only, winder 5 marked on a spinning frame.
    "INSERT INTO daily_attendance VALUES (1,'2026-10-07',109,29,1,1,'3')",
    "INSERT INTO daily_attendance VALUES (2,'2026-10-07',109,29,1,2,NULL)",
    "INSERT INTO daily_attendance VALUES (3,'2026-10-07',109,29,1,9,'4')",  # rejected
    "INSERT INTO daily_attendance VALUES (4,'2026-10-07',110,29,1,4,'3')",
    "INSERT INTO daily_attendance VALUES (5,'2026-10-07',109,29,1,5,'3')",
    "INSERT INTO daily_ebmc_attendance VALUES (10,1,41,1)",
    "INSERT INTO daily_ebmc_attendance VALUES (11,2,42,1)",
    "INSERT INTO daily_ebmc_attendance VALUES (12,5,77,1)",  # spinning frame
    "INSERT INTO daily_ebmc_attendance VALUES (13,2,43,0)",  # inactive detail
    "INSERT INTO machine_mst VALUES (41,'4101','WINDING4101',5,53,1)",
    "INSERT INTO machine_mst VALUES (42,'4102','WINDING4102',5,53,1)",
    "INSERT INTO machine_mst VALUES (43,'4103','WINDING4103',5,53,1)",
    "INSERT INTO machine_mst VALUES (77,'S77','SPINNING77',3,40,1)",
    "INSERT INTO machine_type_mst VALUES (5,'Winding',1)",
    "INSERT INTO machine_type_mst VALUES (3,'Spinning',1)",
    "INSERT INTO dept_mst VALUES (53,29)",
    "INSERT INTO dept_mst VALUES (40,29)",
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (1,2,29,'2026-10-07',109,1,NULL,5,10,1,1)",
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (2,2,29,'2026-10-07',109,3,41,5,10,1,1)",
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (3,2,29,'2026-10-07',109,4,41,5,10,1,1)",
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (4,2,29,'2026-10-07',109,8,41,5,10,0,1)",  # inactive
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (5,2,29,'2026-10-06',110,2,42,7,12,1,1)",  # winder 2 yesterday
    "INSERT INTO jute_prod_winding_daily_qlty VALUES (6,2,29,'2026-10-05',109,2,42,6,11,1,1)",
    "INSERT INTO jute_prod_winding_doff VALUES (100,29,'2026-10-07',109,3,5,1)",
    "INSERT INTO jute_prod_winding_doff VALUES (101,29,'2026-10-07',109,3,5,1)",
    "INSERT INTO jute_prod_winding_doff VALUES (102,29,'2026-10-07',109,6,8,1)",
    "INSERT INTO hrms_ed_personal_details VALUES (1,'LAXMI',NULL,'DEBI')",
    "INSERT INTO hrms_ed_personal_details VALUES (2,'SITA','',NULL)",
    "INSERT INTO hrms_ed_official_details VALUES (1,'02413',1)",
    "INSERT INTO hrms_ed_official_details VALUES (2,' 02414 ',1)",
    "INSERT INTO spell_mst VALUES (109,'A')",
    "INSERT INTO spell_mst VALUES (110,'B')",
]

_BINDS = {"branch_id": 29, "tran_date": "2026-10-07", "spell_id": 109}


@pytest.fixture(scope="module")
def sqlite_conn():
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect()
    for ddl in _SCHEMA:
        conn.execute(text(ddl))
    for row in _SEED:
        conn.execute(text(row))
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _sqlite_has_concat_ws(conn) -> bool:
    try:
        conn.execute(text("SELECT CONCAT_WS(' ', 'a', 'b')")).scalar()
        return True
    except Exception:
        return False


class TestSqlShape:
    def test_builders_compile(self):
        for name in [
            "get_winding_sync_attendance_query",
            "get_winding_sync_attendance_machines_query",
            "get_winding_sync_map_query",
            "get_winding_sync_doff_winders_query",
            "get_winding_sync_worker_labels_query",
            "get_winding_sync_last_map_query",
            "get_winding_sync_other_spell_attendance_query",
            "update_winding_sync_map_machine_query",
        ]:
            assert getattr(sq, name)() is not None

    def test_attendance_unit_rows(self, sqlite_conn):
        rows = sqlite_conn.execute(sq.get_winding_sync_attendance_query(), _BINDS).fetchall()
        # rejected (9) and other-spell (4) rows are out; 1, 2, 5 are in.
        assert sorted(int(r.eb_id) for r in rows) == [1, 2, 5]
        assert all(int(r.att_rows) == 1 for r in rows)

    def test_attendance_machines_are_winding_type_in_branch_and_active(self, sqlite_conn):
        rows = sqlite_conn.execute(
            sq.get_winding_sync_attendance_machines_query(),
            {**_BINDS, "machine_type_name": "Winding"},
        ).fetchall()
        got = {(int(r.eb_id), int(r.machine_id)) for r in rows}
        # winder 5's spinning frame and winder 2's inactive detail row are excluded
        assert got == {(1, 41), (2, 42)}
        assert {r.mech_code for r in rows} == {"4101", "4102"}

    def test_doff_winders(self, sqlite_conn):
        rows = sqlite_conn.execute(sq.get_winding_sync_doff_winders_query(), _BINDS).fetchall()
        got = {int(r.eb_id): (int(r.item_id), int(r.doffs)) for r in rows}
        assert got == {3: (5, 2), 6: (8, 1)}

    def test_map_rows_are_active_unit_rows_with_labels(self, sqlite_conn):
        if not _sqlite_has_concat_ws(sqlite_conn):
            pytest.skip("SQLite without CONCAT_WS")
        rows = sqlite_conn.execute(sq.get_winding_sync_map_query(), _BINDS).fetchall()
        assert [int(r.winding_daily_qlty_id) for r in rows] == [1, 2, 3]
        by_eb = {int(r.eb_id): r for r in rows}
        assert by_eb[1].emp_code == "02413" and by_eb[1].worker_name == "LAXMI DEBI"
        assert by_eb[1].machine_id is None and by_eb[1].mech_code is None
        assert by_eb[3].mech_code == "4101"
        assert by_eb[4].emp_code is None  # no HRMS row -> still returned

    def test_last_map_row_carries_the_most_recent_earlier_yarn(self, sqlite_conn):
        rows = sqlite_conn.execute(
            sq.get_winding_sync_last_map_query(), {**_BINDS, "eb_ids": [2, 99]}
        ).fetchall()
        assert len(rows) == 1
        assert int(rows[0].eb_id) == 2
        assert int(rows[0].item_id) == 7 and int(rows[0].no_of_spindle) == 12  # 06 Oct, not 05 Oct

    def test_other_spell_attendance(self, sqlite_conn):
        rows = sqlite_conn.execute(
            sq.get_winding_sync_other_spell_attendance_query(), {**_BINDS, "eb_ids": [3, 4]}
        ).fetchall()
        assert [(int(r.eb_id), r.spell_code) for r in rows] == [(4, "B")]

    def test_worker_labels(self, sqlite_conn):
        if not _sqlite_has_concat_ws(sqlite_conn):
            pytest.skip("SQLite without CONCAT_WS")
        rows = sqlite_conn.execute(
            sq.get_winding_sync_worker_labels_query(), {"eb_ids": [2]}
        ).fetchall()
        assert rows[0].emp_code == "02414" and rows[0].worker_name == "SITA"

    def test_machine_update_only_touches_active_row(self, sqlite_conn):
        res = sqlite_conn.execute(
            sq.update_winding_sync_map_machine_query(),
            {"id": 4, "machine_id": 42, "updated_by": 1},
        )
        assert res.rowcount == 0  # row 4 is inactive
        res = sqlite_conn.execute(
            sq.update_winding_sync_map_machine_query(),
            {"id": 1, "machine_id": 41, "updated_by": 1},
        )
        assert res.rowcount == 1
        sqlite_conn.rollback()


# =============================================================================
# Endpoint wiring
# =============================================================================


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    for k, v in mapping.items():
        setattr(row, k, v)
    return row


def _result(rows=None, lastrowid=None, rowcount=1):
    res = MagicMock()
    res.fetchall.return_value = rows or []
    res.lastrowid = lastrowid
    res.rowcount = rowcount
    return res


def _session(handlers):
    """A session whose execute() dispatches on a substring of the SQL text
    (whitespace-normalised), so tests describe the data, not the call order.
    A handler value is a result stub, or a zero-arg function producing one
    (MagicMock is itself callable, so the type check comes first)."""
    session = MagicMock()
    calls = []

    def execute(stmt, params=None):
        sql = " ".join(str(stmt).split())
        calls.append((sql, params))
        for marker, res in handlers:
            if marker in sql:
                return res if isinstance(res, MagicMock) else res()
        return _result([])

    session.execute.side_effect = execute
    session.calls = calls
    return session


def _sql_calls(session, marker):
    return [(s, p) for s, p in session.calls if marker in s]


class TestQualitySyncEndpoint:
    URL = "/api/windingProd/quality_sync"
    BODY = {"co_id": 2, "branch_id": 29, "tran_date": "2026-10-07", "spell_id": 109}

    @pytest.fixture(autouse=True)
    def overrides(self):
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        yield
        app.dependency_overrides.clear()

    def _post(self, session, body=None, level=4):
        app.dependency_overrides[get_tenant_db] = lambda: session
        with patch("src.juteProduction.winding_sync._resolve_spell", return_value=109), \
             patch("src.juteProduction.winding_sync.user_menu_access_level", return_value=level):
            return client.post(self.URL, json=body or self.BODY)

    def test_branch_required(self):
        r = self._post(_session([]), {**self.BODY, "branch_id": None})
        assert r.status_code == 400
        assert r.json()["detail"] == BRANCH_REQUIRED_MSG

    def test_bad_mode(self):
        r = self._post(_session([]), {**self.BODY, "mode": "prune"})
        assert r.status_code == 400

    def test_force_requires_edit(self):
        session = _session([])
        r = self._post(session, {**self.BODY, "mode": "force"}, level=3)
        assert r.status_code == 403
        assert r.json()["detail"] == SYNC_EDIT_MSG
        session.commit.assert_not_called()

    def test_remove_absent_requires_edit(self):
        r = self._post(_session([]), {**self.BODY, "remove_absent": True}, level=3)
        assert r.status_code == 403

    def test_fill_does_not_probe_permission(self):
        session = _session([])
        app.dependency_overrides[get_tenant_db] = lambda: session
        with patch("src.juteProduction.winding_sync._resolve_spell", return_value=109), \
             patch("src.juteProduction.winding_sync.user_menu_access_level") as probe:
            r = client.post(self.URL, json=self.BODY)
        assert r.status_code == 200
        probe.assert_not_called()

    def test_fill_adds_fills_reports_and_commits(self):
        map_rows = [
            _mock_row(_map_row(1, 101, machine_id=None)),
            _mock_row(_map_row(2, 102, machine_id=41)),
            _mock_row(_map_row(3, 103, machine_id=41)),
        ]
        att_rows = [_mock_row({"eb_id": 101, "daily_atten_id": 11, "att_rows": 1}),
                    _mock_row({"eb_id": 102, "daily_atten_id": 12, "att_rows": 1}),
                    _mock_row({"eb_id": 104, "daily_atten_id": 14, "att_rows": 1})]
        mc_rows = [_mock_row({"eb_id": 101, **MC_41}),
                   _mock_row({"eb_id": 102, **MC_42}),
                   _mock_row({"eb_id": 104, **MC_42})]
        doff_rows = [_mock_row({"eb_id": 103, "item_id": 5, "doffs": 2}),
                     _mock_row({"eb_id": 105, "item_id": 9, "doffs": 1})]
        labels = [_mock_row({"eb_id": 104, "emp_code": "E104", "worker_name": "NEW WINDER"}),
                  _mock_row({"eb_id": 105, "emp_code": "E105", "worker_name": "DOFFER"})]
        last_map = [_mock_row({"eb_id": 104, "item_id": 7, "no_of_spindle": 12})]
        other = [_mock_row({"eb_id": 103, "spell_code": "B"})]
        inserted = iter([501, 502])
        session = _session([
            ("FROM jute_prod_winding_daily_qlty q LEFT JOIN hrms_ed_personal_details p", _result(map_rows)),
            ("FROM daily_attendance da WHERE", _result(att_rows)),
            ("FROM daily_ebmc_attendance dea", _result(mc_rows)),
            ("FROM jute_prod_winding_doff", _result(doff_rows)),
            ("FROM hrms_ed_personal_details p WHERE p.eb_id IN", _result(labels)),
            ("ROW_NUMBER() OVER", _result(last_map)),
            ("LEFT JOIN spell_mst sp", _result(other)),
            ("INSERT INTO jute_prod_winding_daily_qlty", lambda: _result([], lastrowid=next(inserted))),
        ])
        r = self._post(session)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["mode"] == "fill"
        assert data["attendance_missing"] is False
        assert data["counts"] == {"attendance_winders": 3, "map_rows": 3, "doff_winders": 2}

        # ADD: 104 from attendance (machine 42, yarn carried from last map row,
        # 12 spindles), 105 from doffs (yarn from the doff, no machine).
        added = {a["eb_id"]: a for a in data["added"]}
        assert set(added) == {104, 105}
        assert added[104] == {
            "winding_daily_qlty_id": 501, "eb_id": 104, "emp_code": "E104",
            "worker_name": "NEW WINDER", "machine_id": 42, "mech_code": "4102",
            "machine_name": "WINDING4102", "item_id": 7, "source": "attendance"}
        assert added[105]["item_id"] == 9 and added[105]["machine_id"] is None
        assert added[105]["source"] == "doff"
        inserts = _sql_calls(session, "INSERT INTO jute_prod_winding_daily_qlty")
        assert [p["eb_id"] for _, p in inserts] == [104, 105]
        assert inserts[0][1]["no_of_spindle"] == 12 and inserts[0][1]["item_id"] == 7
        assert inserts[0][1]["co_id"] == 2 and inserts[0][1]["branch_id"] == 29
        assert inserts[1][1]["no_of_spindle"] == 0
        assert inserts[1][1]["updated_by"] == 7

        # FILL: row 1 blank -> 41. Row 2 differs (41 vs 42): reported, not updated.
        assert [m["winding_daily_qlty_id"] for m in data["machine_filled"]] == [1]
        assert data["machine_filled"][0]["machine_id"] == 41
        assert data["machine_updated"] == []
        updates = _sql_calls(session, "UPDATE jute_prod_winding_daily_qlty SET machine_id")
        assert [(p["id"], p["machine_id"]) for _, p in updates] == [(1, 41)]
        differs = data["exceptions"]["machine_differs"]
        assert [(d["winding_daily_qlty_id"], d["old_machine_id"], d["machine_id"])
                for d in differs] == [(2, 41, 42)]

        # ABSENT: 103 has doffs and attendance in spell B; nothing removed.
        assert data["exceptions"]["absent_on_map"] == [{
            "winding_daily_qlty_id": 3, "eb_id": 103, "emp_code": "E103",
            "worker_name": "Winder 103", "has_doffs": True, "other_spells": ["B"]}]
        assert data["removed"] == []
        assert _sql_calls(session, "SET active = 0") == []
        assert data["exceptions"]["machine_ambiguous"] == []
        session.commit.assert_called_once()

    def test_force_updates_differing_machine(self):
        map_rows = [_mock_row(_map_row(2, 102, machine_id=41))]
        session = _session([
            ("FROM jute_prod_winding_daily_qlty q LEFT JOIN hrms_ed_personal_details p", _result(map_rows)),
            ("FROM daily_attendance da WHERE",
             _result([_mock_row({"eb_id": 102, "daily_atten_id": 12, "att_rows": 1})])),
            ("FROM daily_ebmc_attendance dea", _result([_mock_row({"eb_id": 102, **MC_42})])),
        ])
        r = self._post(session, {**self.BODY, "mode": "force"}, level=4)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["mode"] == "force"
        assert [(m["winding_daily_qlty_id"], m["old_mech_code"], m["mech_code"])
                for m in data["machine_updated"]] == [(2, "m41", "4102")]
        assert data["exceptions"]["machine_differs"] == []
        updates = _sql_calls(session, "UPDATE jute_prod_winding_daily_qlty SET machine_id")
        assert [(p["id"], p["machine_id"], p["updated_by"]) for _, p in updates] == [(2, 42, 7)]
        session.commit.assert_called_once()

    def test_remove_absent_soft_deletes_only_undoffed_absentees(self):
        map_rows = [_mock_row(_map_row(1, 101)), _mock_row(_map_row(2, 102)),
                    _mock_row(_map_row(3, 103))]
        session = _session([
            ("FROM jute_prod_winding_daily_qlty q LEFT JOIN hrms_ed_personal_details p", _result(map_rows)),
            ("FROM daily_attendance da WHERE",
             _result([_mock_row({"eb_id": 101, "daily_atten_id": 11, "att_rows": 1})])),
            ("FROM jute_prod_winding_doff",
             _result([_mock_row({"eb_id": 103, "item_id": 5, "doffs": 1})])),
        ])
        r = self._post(session, {**self.BODY, "remove_absent": True}, level=4)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert [x["winding_daily_qlty_id"] for x in data["removed"]] == [2]
        deletes = _sql_calls(session, "SET active = 0")
        assert [(p["id"], p["updated_by"]) for _, p in deletes] == [(2, 7)]
        # the removed row is no longer listed as absent; the doffed one still is
        assert [(a["winding_daily_qlty_id"], a["has_doffs"])
                for a in data["exceptions"]["absent_on_map"]] == [(3, True)]
        session.commit.assert_called_once()

    def test_remove_absent_refused_when_unit_has_no_attendance(self):
        map_rows = [_mock_row(_map_row(1, 101)), _mock_row(_map_row(2, 102))]
        session = _session([
            ("FROM jute_prod_winding_daily_qlty q LEFT JOIN hrms_ed_personal_details p", _result(map_rows)),
        ])
        r = self._post(session, {**self.BODY, "remove_absent": True}, level=4)
        assert r.status_code == 400
        assert r.json()["detail"] == REMOVE_NO_ATTENDANCE_MSG
        assert _sql_calls(session, "SET active = 0") == []
        session.commit.assert_not_called()
        session.rollback.assert_called()

    def test_fill_with_no_attendance_reports_it_and_still_adds_doffers(self):
        session = _session([
            ("FROM jute_prod_winding_doff",
             _result([_mock_row({"eb_id": 105, "item_id": 9, "doffs": 1})])),
            ("FROM hrms_ed_personal_details p WHERE p.eb_id IN",
             _result([_mock_row({"eb_id": 105, "emp_code": "E105", "worker_name": "DOFFER"})])),
            ("INSERT INTO jute_prod_winding_daily_qlty", _result([], lastrowid=900)),
        ])
        r = self._post(session)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["attendance_missing"] is True
        assert [a["eb_id"] for a in data["added"]] == [105]
        assert data["exceptions"]["absent_on_map"] == []
        session.commit.assert_called_once()

    def test_db_error_rolls_back_as_500(self):
        session = MagicMock()
        session.execute.side_effect = RuntimeError("boom")
        r = self._post(session)
        assert r.status_code == 500
        session.rollback.assert_called()
