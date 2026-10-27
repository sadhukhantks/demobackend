"""Endpoint tests for the manual additions / exclusions of worked machines on
the Hands Report (spec 2026-09-30 hands norms v2, §12.3):

* ``POST /api/juteProductionReports/hands_report_machine_override``
* ``POST /api/juteProductionReports/hands_report_machine_override_undo``
* ``GET  /api/juteProductionReports/hands_report_machines``
* ``meta.can_edit`` of ``GET /api/juteProductionReports/hands_report``

Portal persona, no real DB: the tenant session and the auth dependency are
overridden through ``app.dependency_overrides`` keyed by the symbols imported
into the router module (same pattern as test_juteprod_hands_norms.py), and the
session is a recording fake that answers each statement by its SQL — so the
tests read the exact statements, binds and transaction boundaries of a save.
The menu access level is patched in the module: Edit (4) unless a test lowers
it. The builder rules and the rebuild's override statements are tested in
test_juteprod_hands_report.py.
"""

from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.juteProduction.hands_report import (
    HANDS_REPORT_MENU_PATH,
    OVERRIDE_MAX_MACHINES,
    OVERRIDE_VIEW_ONLY_MSG,
    _date_runs,
    get_current_user_with_refresh,
    get_tenant_db,
)
from src.main import app

client = TestClient(app)

MOD = "src.juteProduction.hands_report"
BASE = "/api/juteProductionReports"
OVERRIDE_URL = f"{BASE}/hands_report_machine_override"
UNDO_URL = f"{BASE}/hands_report_machine_override_undo"
MACHINES_URL = f"{BASE}/hands_report_machines"

# "Today" at the mill for every test: 2026-10-01 23:30 IST.
NOW = datetime(2026, 10, 1, 23, 30, 0)
TODAY = "2026-10-01"

# The rebuild of one window: the read transaction is ended, then delete,
# source insert, ADD upsert, EXCLUDE update, commit.
REBUILD = ["rollback", "SET", "DELETE", "INSERT", "INSERT", "UPDATE", "commit"]


class Row:
    def __init__(self, **cols):
        self._mapping = cols


class Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return None


class FakeDb:
    """Recording session. ``branch_machines`` are the active machines of the
    branch, ``active_overrides`` the active override rows of the company /
    branch (``{id: date}``), ``machines`` / ``members`` what the picker's two
    reads return; ``pair`` = the branch belongs to the company."""

    def __init__(self, branch_machines=(), active_overrides=None, machines=(), members=(),
                 pair=True):
        self.log = []
        self.branch_machines = set(branch_machines)
        self.active_overrides = dict(active_overrides or {})
        self.machines = list(machines)
        self.members = list(members)
        self.pair = pair

    def execute(self, stmt, binds=None):
        sql = " ".join(str(stmt).split())
        self.log.append(("execute", sql, binds))
        if "FROM branch_mst WHERE branch_id" in sql:
            return Result([Row(ok=1)] if self.pair else [])
        if sql.startswith("SELECT m.machine_id FROM machine_mst m WHERE m.active = 1"):
            # hands_norms.get_branch_machines_query — the group members' check
            return Result([Row(machine_id=m) for m in binds["machine_ids"]
                           if m in self.branch_machines])
        if sql.startswith("SELECT machine_shift_override_id, tran_date FROM jute_prod_machine_shift_override"):
            return Result([Row(machine_shift_override_id=i, tran_date=self.active_overrides[i])
                           for i in binds["override_ids"] if i in self.active_overrides])
        if sql.startswith("SELECT m.machine_id, m.machine_name"):
            return Result([Row(**m) for m in self.machines])
        if sql.startswith("SELECT g.machine_group_id"):
            return Result([Row(**m) for m in self.members])
        return Result()

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def statements(self) -> list:
        return [e[1].split()[0] if e[0] == "execute" else e[0] for e in self.log]

    def executed(self, starts_with: str) -> list:
        return [e for e in self.log if e[0] == "execute" and e[1].startswith(starts_with)]

    def wrote_anything(self) -> bool:
        return any(v in ("INSERT", "UPDATE", "DELETE", "commit") for v in self.statements())


@pytest.fixture
def api():
    """``(db_holder, access)``: set ``db_holder['db']`` to the FakeDb of the
    test; ``access`` is the patched ``user_menu_access_level`` (Edit by
    default)."""
    holder = {"db": FakeDb()}
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 9}
    app.dependency_overrides[get_tenant_db] = lambda: holder["db"]
    with patch(f"{MOD}.user_menu_access_level", return_value=4) as access, \
            patch(f"{MOD}.now_ist", return_value=NOW):
        yield holder, access
    app.dependency_overrides.clear()


def body(**over) -> dict:
    base = {
        "co_id": 2,
        "branch_id": 29,
        "tran_date": "2026-09-30",
        "shift": "A",
        "machine_ids": [11, 12],
        "action": "ADD",
        "reason": "Weaver not marked",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


# --- POST hands_report_machine_override ----------------------------------------


def test_override_post_needs_edit_access_and_validates(api):
    holder, access = api
    # A role with View / Print / Write on the report cannot change the machines.
    for level in (0, 1, 2, 3):
        access.return_value = level
        holder["db"] = FakeDb(branch_machines={11, 12})
        resp = client.post(OVERRIDE_URL, json=body())
        assert resp.status_code == 403, (level, resp.text)
        assert resp.json() == {"detail": OVERRIDE_VIEW_ONLY_MSG}
        assert resp.json()["detail"] == (
            "This role can view the hands report but not change the machines worked")
        assert not holder["db"].wrote_anything()
    # the level is read for this user, company, branch on the report's own menu row
    assert access.call_args.args[1:] == (9, 2, 29, HANDS_REPORT_MENU_PATH)
    assert HANDS_REPORT_MENU_PATH == "juteProduction/report/handsReport"

    # With Edit: every validation failure is a 400 with a plain-language detail
    # and nothing is written.
    access.return_value = 4
    cases = [
        (body(co_id=...), "co_id is required"),
        (body(branch_id=None), "branch_id is required"),
        (body(co_id="two"), "co_id is not valid"),
        (body(tran_date=...), "Date is required"),
        (body(tran_date="30-09-2026"), "Date is not valid"),
        (body(tran_date="2026-10-02"), "in the future"),
        (body(shift="D"), "Shift must be one of A, B, C, G"),
        (body(shift=...), "Shift must be one of A, B, C, G"),
        (body(action="REMOVE"), "Action must be ADD or EXCLUDE"),
        (body(machine_ids=[]), "Select at least one machine"),
        (body(machine_ids=...), "Select at least one machine"),
        (body(machine_ids="11"), "Select at least one machine"),
        (body(machine_ids=[11, "loom"]), "'loom' is not a valid machine"),
        (body(machine_ids=[11, 1.5]), "'1.5' is not a valid machine"),
        (body(machine_ids=list(range(1, OVERRIDE_MAX_MACHINES + 2))), "At most 500 machines"),
        (body(reason=...), "Reason is required — at least 3 characters"),
        (body(reason="  ok  "), "Reason is required — at least 3 characters"),
        (body(reason="x" * 256), "Reason is too long — at most 255 characters"),
        # 13 is not an active machine of this branch — the group members' check
        (body(machine_ids=[11, 13]), "1 machine(s) are not active machines of this branch: 13"),
    ]
    for payload, expected in cases:
        holder["db"] = FakeDb(branch_machines={11, 12})
        resp = client.post(OVERRIDE_URL, json=payload)
        assert resp.status_code == 400, (payload, resp.text)
        assert expected in resp.json()["detail"], (payload, resp.json())
        assert not holder["db"].wrote_anything(), payload
    # a branch of another company
    holder["db"] = FakeDb(branch_machines={11, 12}, pair=False)
    resp = client.post(OVERRIDE_URL, json=body())
    assert resp.status_code == 400 and resp.json()["detail"] == "branch_id does not belong to co_id"
    assert not holder["db"].wrote_anything()
    # a body that is not a JSON object
    holder["db"] = FakeDb()
    resp = client.post(OVERRIDE_URL, json=[1, 2])
    assert resp.status_code == 400 and "JSON object" in resp.json()["detail"]
    resp = client.post(OVERRIDE_URL, content="{not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400 and "not valid JSON" in resp.json()["detail"]

    # Today itself is allowed (the mill's date, IST), and so are a 3-character
    # reason, a 255-character one and exactly 500 machines.
    for payload in (body(tran_date=TODAY), body(reason="abc"), body(reason="y" * 255),
                    body(machine_ids=list(range(1, OVERRIDE_MAX_MACHINES + 1)))):
        holder["db"] = FakeDb(branch_machines=set(range(1, OVERRIDE_MAX_MACHINES + 1)))
        resp = client.post(OVERRIDE_URL, json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": {"saved": len(payload["machine_ids"])}}


def test_override_post_retires_the_previous_override_and_rebuilds_the_date(api):
    holder, _access = api
    db = holder["db"] = FakeDb(branch_machines={11, 12, 14})
    # lower-case shift / action, a repeated machine and a padded reason are normalised
    resp = client.post(OVERRIDE_URL, json=body(
        shift="b", action="exclude", machine_ids=[11, "12", 11, 14],
        reason="  Marked on the   wrong loom "))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"saved": 3}}

    # company / branch check, the machines' branch check, then ONE transaction
    # (at READ COMMITTED, so two simultaneous saves cannot deadlock on gap
    # locks): retire the previous overrides of those machine-shifts, insert
    # the new ones, commit — and then that date is rebuilt.
    assert db.statements() == (
        ["SELECT", "SELECT", "rollback", "SET", "UPDATE", "INSERT", "commit"] + REBUILD
    )
    save_set, rebuild_set = db.executed("SET TRANSACTION")
    assert save_set[1] == rebuild_set[1] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    day = date(2026, 9, 30)
    scope = {"co_id": 2, "branch_id": 29, "tran_date": day, "shift": "B"}

    (retire,) = db.executed("UPDATE jute_prod_machine_shift_override")
    assert "SET active = 0" in retire[1] and "active = 1" in retire[1]
    for cond in ("co_id = :co_id", "branch_id = :branch_id", "tran_date = :tran_date",
                 "shift = :shift", "machine_id IN"):
        assert cond in retire[1]
    # the retired row is frozen: it keeps saying who made THAT change and when
    assert "updated_date_time = updated_date_time" in retire[1] and "updated_by" not in retire[1]
    assert retire[2] == {**scope, "machine_ids": [11, 12, 14]}

    (insert,) = db.executed("INSERT INTO jute_prod_machine_shift_override")
    assert "(co_id, branch_id, tran_date, shift, machine_id, action, reason, active, updated_by)" in insert[1]
    assert insert[2] == [
        {**scope, "machine_id": m, "action": "EXCLUDE", "reason": "Marked on the wrong loom",
         "updated_by": 9}
        for m in (11, 12, 14)
    ]
    assert db.log.index(retire) < db.log.index(insert) < db.log.index(("commit",))

    # the rebuild covers that one date only, overrides applied in its transaction
    window = {"co_id": 2, "branch_id": 29, "date_from": day, "date_to": day}
    (delete,) = db.executed("DELETE FROM jute_prod_machine_shift_worked")
    source_insert, add_upsert = db.executed("INSERT INTO jute_prod_machine_shift_worked")
    (exclude_update,) = db.executed("UPDATE jute_prod_machine_shift_worked")
    assert [e[2] for e in (delete, source_insert, add_upsert, exclude_update)] == [window] * 4
    assert db.log.index(delete) > db.log.index(insert)
    assert db.statements().count("commit") == 2 and db.statements()[-1] == "commit"

    # A failure while saving rolls the overrides back and rebuilds nothing.
    class Failing(FakeDb):
        def execute(self, stmt, binds=None):
            result = super().execute(stmt, binds)
            if self.log[-1][1].startswith("INSERT INTO jute_prod_machine_shift_override"):
                raise RuntimeError("boom")
            return result

    db = holder["db"] = Failing(branch_machines={11, 12})
    resp = client.post(OVERRIDE_URL, json=body())
    assert resp.status_code == 500 and "boom" in resp.json()["detail"]
    assert db.statements() == ["SELECT", "SELECT", "rollback", "SET", "UPDATE", "INSERT", "rollback"]


# --- POST hands_report_machine_override_undo --------------------------------------


def test_undo_sets_inactive_and_rebuilds(api):
    holder, access = api
    d28, d29, d30, d20 = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 9, 20)
    # 501..504 are active overrides of this company / branch; 999 is not
    # (already undone, or another branch's) and is ignored.
    active = {501: d29, 502: d30, 503: d20, 504: d28}
    db = holder["db"] = FakeDb(active_overrides=active)
    resp = client.post(UNDO_URL, json={"co_id": 2, "branch_id": 29,
                                       "override_ids": [502, 501, 999, "503", 502, 504]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": {"undone": 4}}

    (lookup,) = db.executed("SELECT machine_shift_override_id, tran_date")
    assert "co_id = :co_id" in lookup[1] and "branch_id = :branch_id" in lookup[1]
    assert "active = 1" in lookup[1]
    assert lookup[2] == {"co_id": 2, "branch_id": 29, "override_ids": [502, 501, 999, 503, 504]}
    (undo,) = db.executed("UPDATE jute_prod_machine_shift_override")
    assert "SET active = 0, updated_by = :updated_by" in undo[1]
    # only rows of that company / branch, only active ones
    for cond in ("co_id = :co_id", "branch_id = :branch_id", "active = 1",
                 "machine_shift_override_id IN"):
        assert cond in undo[1][undo[1].index("WHERE"):]
    assert undo[2] == {"co_id": 2, "branch_id": 29, "updated_by": 9,
                       "override_ids": [502, 501, 503, 504]}

    # saved first, then the affected dates are rebuilt: 20 Sep on its own and
    # 28..30 Sep as one stretch — nothing in between
    assert db.statements() == ["SELECT", "UPDATE", "commit"] + REBUILD + REBUILD
    windows = [e[2] for e in db.executed("DELETE FROM jute_prod_machine_shift_worked")]
    assert windows == [
        {"co_id": 2, "branch_id": 29, "date_from": d20, "date_to": d20},
        {"co_id": 2, "branch_id": 29, "date_from": d28, "date_to": d30},
    ]

    # Nothing to undo (ids unknown / already undone): 0, no write, no rebuild.
    db = holder["db"] = FakeDb(active_overrides=active)
    resp = client.post(UNDO_URL, json={"co_id": 2, "branch_id": 29, "override_ids": [998, 999]})
    assert resp.status_code == 200 and resp.json() == {"data": {"undone": 0}}
    assert db.statements() == ["SELECT"]

    # Validation and access.
    for payload, expected in [
        ({"branch_id": 29, "override_ids": [501]}, "co_id is required"),
        ({"co_id": 2, "override_ids": [501]}, "branch_id is required"),
        ({"co_id": 2, "branch_id": 29}, "Select at least one manual change"),
        ({"co_id": 2, "branch_id": 29, "override_ids": []}, "Select at least one manual change"),
        ({"co_id": 2, "branch_id": 29, "override_ids": ["x"]}, "'x' is not a valid manual change"),
    ]:
        db = holder["db"] = FakeDb(active_overrides=active)
        resp = client.post(UNDO_URL, json=payload)
        assert resp.status_code == 400 and expected in resp.json()["detail"], (payload, resp.text)
        assert not db.wrote_anything()
    access.return_value = 3
    db = holder["db"] = FakeDb(active_overrides=active)
    resp = client.post(UNDO_URL, json={"co_id": 2, "branch_id": 29, "override_ids": [501]})
    assert resp.status_code == 403 and resp.json() == {"detail": OVERRIDE_VIEW_ONLY_MSG}
    assert db.statements() == []


def test_date_runs_merge_consecutive_days_only():
    d = lambda day: date(2026, 9, day)  # noqa: E731
    assert _date_runs([]) == []
    assert _date_runs([d(5)]) == [(d(5), d(5))]
    assert _date_runs([d(7), d(5), d(6), d(5)]) == [(d(5), d(7))]
    assert _date_runs([d(1), d(3), d(4), d(9)]) == [(d(1), d(1)), (d(3), d(4)), (d(9), d(9))]
    # across a month end
    assert _date_runs([date(2026, 9, 30), date(2026, 10, 1)]) == [(date(2026, 9, 30), date(2026, 10, 1))]


# --- GET hands_report_machines ------------------------------------------------------


def test_machines_endpoint_lists_branch_machines_with_groups(api):
    holder, access = api
    machines = [
        {"machine_id": 11, "machine_name": "LOOM-11", "mech_code": "L11", "machine_type_id": 7,
         "machine_type_name": "Loom", "dept_id": 4, "dept": "WEAVING"},
        {"machine_id": 12, "machine_name": None, "mech_code": "L12", "machine_type_id": 7,
         "machine_type_name": "Loom", "dept_id": 4, "dept": "WEAVING"},
        {"machine_id": 30, "machine_name": None, "mech_code": None, "machine_type_id": None,
         "machine_type_name": None, "dept_id": 6, "dept": "FINISHING"},
    ]
    members = [
        {"machine_group_id": 7, "group_name": "WVGHESS", "unit_label": "loom", "machine_id": 11, "units": 1},
        {"machine_group_id": 8, "group_name": "All looms", "unit_label": "loom", "machine_id": 11, "units": 1},
        {"machine_group_id": 8, "group_name": "All looms", "unit_label": "loom", "machine_id": 12, "units": 1},
    ]
    db = holder["db"] = FakeDb(machines=machines, members=members)
    # a view-only role may read the picker
    access.return_value = 1
    resp = client.get(f"{MACHINES_URL}?co_id=2&branch_id=29")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": [
        {"machine_id": 11, "machine": "LOOM-11", "mech_code": "L11", "machine_type_id": 7,
         "machine_type": "Loom", "dept_id": 4, "dept": "WEAVING",
         "group_ids": [8, 7], "groups": "All looms, WVGHESS"},
        {"machine_id": 12, "machine": "L12", "mech_code": "L12", "machine_type_id": 7,
         "machine_type": "Loom", "dept_id": 4, "dept": "WEAVING",
         "group_ids": [8], "groups": "All looms"},
        {"machine_id": 30, "machine": "MC30", "mech_code": None, "machine_type_id": None,
         "machine_type": None, "dept_id": 6, "dept": "FINISHING",
         "group_ids": [], "groups": ""},
    ]}
    assert db.statements() == ["SELECT", "SELECT"]
    picker, groups = [e for e in db.log if e[0] == "execute"]
    assert picker[2] == {"co_id": 2, "branch_id": 29} and groups[2] == {"co_id": 2, "branch_id": 29}
    # active machines of the branch's departments, of that company — the set
    # the override save accepts
    for cond in ("m.active = 1", "dp.branch_id = :branch_id", "b.co_id = :co_id",
                 "INNER JOIN dept_mst dp ON dp.dept_id = m.dept_id"):
        assert cond in picker[1]
    # an empty branch is an empty list, and both ids are required
    holder["db"] = FakeDb()
    assert client.get(f"{MACHINES_URL}?co_id=2&branch_id=29").json() == {"data": []}
    assert client.get(f"{MACHINES_URL}?branch_id=29").status_code == 400
    assert client.get(f"{MACHINES_URL}?co_id=2").status_code == 400
    assert client.get(f"{MACHINES_URL}?co_id=2&branch_id=x").status_code == 400


# --- meta.can_edit of the report itself -----------------------------------------


def test_report_meta_can_edit_follows_the_callers_access_and_never_raises(api):
    holder, access = api
    url = f"{BASE}/hands_report?co_id=2&branch_id=29&date_from=2026-09-29&date_to=2026-09-30&view=machines_run"
    for level, expected in ((4, True), (3, False), (1, False), (0, False)):
        access.return_value = level
        holder["db"] = FakeDb()
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        meta = resp.json()["data"]["meta"]
        assert meta["can_edit"] is expected
        assert (meta["added_manually"], meta["excluded_manually"]) == (0, 0)
    assert access.call_args.args[1:] == (9, 2, 29, HANDS_REPORT_MENU_PATH)
    # a failing access lookup must not take the report down
    access.side_effect = RuntimeError("role tables unreachable")
    holder["db"] = FakeDb()
    resp = client.get(url)
    assert resp.status_code == 200 and resp.json()["data"]["meta"]["can_edit"] is False
    access.side_effect = None
    # no user in the token: not an editor, and the level is not even looked up
    access.reset_mock()
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {}
    holder["db"] = FakeDb()
    resp = client.get(url)
    assert resp.status_code == 200 and resp.json()["data"]["meta"]["can_edit"] is False
    assert not access.called
    resp = client.post(OVERRIDE_URL, json=body())
    assert resp.status_code == 403 and not access.called
