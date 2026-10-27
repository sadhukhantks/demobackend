"""Weaving Sync (2026-08-21): POST /sync, hybrid stored/live reads, stale flags.

Covers the sync-without-lock flow: /sync WARNS (never blocks) on unmapped /
stale-quality looms, upserts a sync-only header (is_locked untouched), and the
readers (entries_by_date, planning_grid, fetch_weaving_day_rows) serve the
stored jute_prod_weaving_log snapshot per spell when a header exists. SQL-text
guards pin the new flag builders (EXISTS-on-log) and the log driver's alias
parity with the live driver. Repo pattern: TestClient + dependency overrides +
MagicMock sessions (see test_weaving_process.py / test_weaving_dayslice.py).
"""
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProduction import weaving_query as q
from src.juteProduction.weaving_lock import fetch_weaving_day_rows, get_process_lock

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _fetchall_exec(rows):
    ex = MagicMock()
    ex.fetchall.return_value = rows
    return ex


def _fetchone_exec(row):
    ex = MagicMock()
    ex.fetchone.return_value = row
    return ex


def test_sync_builders_compile():
    for name in [
        "insert_weaving_sync_header_query", "update_weaving_header_synced_query",
        "get_weaving_day_headers_query", "get_weaving_map_spells_query",
        "get_weaving_daily_spells_query", "get_weaving_plan_driver_log_query",
        "flag_weaving_stale_for_pick_query", "flag_weaving_stale_for_target_query",
        "flag_weaving_stale_for_stoppage_query",
    ]:
        assert getattr(q, name)() is not None


# =============================================================================
# POST /api/weavingProd/sync
# =============================================================================


class TestSyncEndpoint:
    """Same recompute-and-store as Process but unmapped/mismatch only WARN and
    the header upsert never locks. Execute order (15 calls, like /process):
    unmapped, mismatch, no_worker, no_standard, no_picks, null_branch
    (branch-scoped run), unit rows, operator candidates, eb clear (+ one stamp per
    resolved loom), open_jugar set-based resolve (+ one bulk UPDATE when any row
    changed), negative_jugar, soft_delete, freeze INSERT, header row,
    header insert/update. The eb stamp moved BEFORE the freeze."""

    def _run(self, session):
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            with patch("src.juteProduction.weaving_process._resolve_spell", return_value=91), \
                 patch("src.juteProduction.weaving_process._derive_co_id", return_value=1), \
                 patch("src.juteProduction.weaving_process.require_edit_if_locked"):
                return client.post("/api/weavingProd/sync", json={
                    "co_id": 1, "branch_id": 2, "tran_date": "2026-08-21", "spell": "A1"})
        finally:
            app.dependency_overrides.clear()

    def _side_effects(self, unmapped=(), mismatch=(), rowcount=3, header=None,
                      null_branch=()):
        empty = _fetchall_exec([])
        return [
            _fetchall_exec(list(unmapped)),     # 0 unmapped (WARN, not block)
            _fetchall_exec(list(mismatch)),     # 1 quality mismatch (WARN)
            empty, empty, empty,                # 2-4 no_worker/no_standard/no_picks
            _fetchall_exec(list(null_branch)),  # 5 null-branch rows (branch-scoped)
            _fetchall_exec([]),                 # 6 unit rows (eb scope + open_jugar)
            _fetchall_exec([]),                 # 7 operator candidates (none)
            MagicMock(),                        # 8 eb clear
            _fetchall_exec([]),                 # 9 open_jugar set-based resolve (TKT-000134,
                                                #   no rows -> no bulk UPDATE)
            empty,                              # 10 negative_jugar
            MagicMock(),                        # 11 soft delete
            MagicMock(rowcount=rowcount),       # 12 freeze INSERT
            _fetchone_exec(header),             # 13 header probe
            MagicMock(),                        # 14 header insert/update
        ]

    def test_sync_warns_but_does_not_block_on_unmapped(self):
        """CONTRAST with /process: an unmapped produced loom 400s Process but only
        lands in warnings here — the snapshot still stores (zero production)."""
        session = MagicMock()
        row = _mock_row({"machine_id": 42, "mech_code": "L42", "machine_name": "Loom 42"})
        mm = _mock_row({"machine_id": 7, "mech_code": "L07", "machine_name": "Loom 7"})
        session.execute.side_effect = self._side_effects(
            unmapped=[row], mismatch=[mm], rowcount=5)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["synced"] == 5
        assert data["warnings"]["unmapped"][0]["machine_id"] == 42
        assert data["warnings"]["quality_mismatch"] == ["L07"]
        assert data["warnings"]["no_worker"] == []
        assert "null_branch_rows" not in data["warnings"]  # only when rows come back
        session.commit.assert_called_once()

    def test_sync_branch_scoped_warns_null_branch_rows(self):
        """A branch-scoped sync skips NULL-branch daily rows in the freeze — they
        surface in warnings["null_branch_rows"] instead of silently vanishing
        once the unit serves the stored log."""
        session = MagicMock()
        nb = _mock_row({"machine_id": 9, "mech_code": "L09", "machine_name": "Loom 9"})
        session.execute.side_effect = self._side_effects(null_branch=[nb])
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["warnings"]["null_branch_rows"] == [
            {"machine_id": 9, "mech_code": "L09", "machine_name": "Loom 9"}]

    def test_sync_creates_sync_only_header_when_none(self):
        session = MagicMock()
        session.execute.side_effect = self._side_effects(header=None)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        last_sql, last_binds = session.execute.call_args_list[14].args
        assert str(last_sql) == str(q.insert_weaving_sync_header_query())
        assert last_binds["synced_by"] == 1
        assert last_binds["co_id"] == 1

    def test_sync_updates_existing_header(self):
        session = MagicMock()
        header = MagicMock(weaving_process_lock_id=9)
        session.execute.side_effect = self._side_effects(header=header)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        last_sql, last_binds = session.execute.call_args_list[14].args
        assert str(last_sql) == str(q.update_weaving_header_synced_query())
        assert last_binds == {"id": 9, "synced_by": 1}

    def test_sync_locked_unit_write_user_gets_403(self):
        """Re-syncing a LOCKED unit overwrites frozen data — same Edit gate as
        reprocess (mirrors the /process lock-gate test)."""
        session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        try:
            with patch("src.juteProduction.weaving_process._resolve_spell", return_value=91), \
                 patch("src.juteProduction.weaving_lock.is_unit_locked", return_value=True), \
                 patch("src.juteProduction.weaving_lock.user_menu_access_level", return_value=3):
                resp = client.post("/api/weavingProd/sync", json={
                    "co_id": 1, "branch_id": 2, "tran_date": "2026-08-21", "spell": "A1"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 403, resp.text

    def test_sync_400_when_co_id_underivable(self):
        session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            with patch("src.juteProduction.weaving_process._resolve_spell", return_value=91):
                resp = client.post("/api/weavingProd/sync", json={
                    "tran_date": "2026-08-21", "spell": "A1"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 400
        assert "co_id" in resp.json()["detail"]


# =============================================================================
# entries_by_date serves the stored log for STORED (sync-only or locked) units
# =============================================================================


class TestEntriesByDateStored:
    def test_stored_unit_serves_log_rows(self):
        """A unit with an active header (is_unit_stored) reads jute_prod_weaving_log,
        NOT the live day-slice — adapts the old locked-serves-log test."""
        session = MagicMock()
        frozen = _mock_row({"weaving_daily_id": 1, "machine_id": 42,
                            "production_yds": 100.0, "efficiency": 88.0,
                            "tran_date": "2026-08-21"})
        session.execute.return_value.fetchall.return_value = [frozen]
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            with patch("src.juteProduction.weaving_entry._resolve_spell", return_value=91), \
                 patch("src.juteProduction.weaving_entry.is_unit_stored", return_value=True):
                resp = client.get("/api/weavingProd/entries_by_date"
                                  "?co_id=1&branch_id=2&tran_date=2026-08-21&spell=A1")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"][0]["efficiency"] == 88.0
        sql = str(session.execute.call_args.args[0])
        assert "FROM jute_prod_weaving_log" in sql
        assert "jute_prod_weaving_daily" not in sql  # not the live slice


# =============================================================================
# planning_grid — hybrid per-spell stored/live + sync_status
# =============================================================================


def _driver_row(spell_id, machine_id=7, daily_id=10):
    return _mock_row({
        "weaving_quality_map_id": 50 + spell_id, "machine_id": machine_id,
        "spell_id": spell_id, "spell_code": "A1", "mech_code": "L01",
        "machine_name": "Loom-1", "line_no": "1", "branch_id": 2,
        "item_id": 10, "item_code": "JC", "item_name": "Jute Cloth",
        "weaving_quality_id": 5, "weaving_quality_code": "Q5",
        "weaving_quality_name": "Red", "finished_length": 100.0,
        "ozs_yds": 10.0, "std_ozs_yds": 10.0, "no_of_jugar_per_cut": 16.0,
        "is_composite": 0, "weaving_daily_id": daily_id, "cuts": 10.0,
        "close_jugar": 12.0, "less_production": 0.0, "open_jugar": 0.0,
        "jugar": 172.0, "std_speed": 160.0, "act_speed": 155.0,
        "std_picks": 9.0, "act_picks": 9.5, "std_eff": 85.0,
        "target_eff": 90.0, "eff_speed": 150.0, "eff_picks": 9.2,
        "working_hours": 8.0, "production_yds": 1075.0,
        "production_kg": 304.76, "production_mt": 0.305,
        "std_prod_yds": 1200.0, "target_prod_yds": 1080.0,
        "efficiency": 89.58, "std_prod_kg": 340.0, "target_kg": 306.0,
    })


class TestPlanningGridHybrid:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_no_headers_single_live_driver_call(self):
        """Nothing stored -> fast path: ONE live driver call (pre-sync behavior)."""
        self._session.execute.side_effect = [
            _fetchall_exec([]),                 # day headers: none
            _fetchall_exec([_driver_row(1)]),   # live driver
        ]
        resp = client.get("/api/weavingProd/planning_grid?co_id=1&tran_date=2026-08-21")
        assert resp.status_code == 200, resp.text
        assert self._session.execute.call_count == 2
        assert str(self._session.execute.call_args_list[1].args[0]) == \
            str(q.get_weaving_plan_driver_query())
        payload = resp.json()["data"]
        assert payload["rows"][0]["weaving_quality_map_id"] == 51
        assert payload["sync_status"] == [
            {"spell_id": 1, "stored": False, "locked": False, "stale": False,
             "synced_at": None}]

    def test_mixed_day_stored_spell_uses_log_driver(self):
        """Header for spell 1 only: spell 1 -> log driver, spell 2 -> live driver;
        sync_status reflects per-spell stored/locked/stale."""
        header = MagicMock(spell_id=1, branch_id=None, is_locked=0,
                           reprocess_needed=0,
                           synced_date_time="2026-08-21 10:00:00")
        self._session.execute.side_effect = [
            _fetchall_exec([header]),                              # day headers
            _fetchall_exec([MagicMock(spell_id=1), MagicMock(spell_id=2)]),  # map spells
            _fetchall_exec([_driver_row(1)]),                      # spell 1 (log)
            _fetchall_exec([_driver_row(2, machine_id=8)]),        # spell 2 (live)
        ]
        resp = client.get("/api/weavingProd/planning_grid?co_id=1&tran_date=2026-08-21")
        assert resp.status_code == 200, resp.text
        calls = self._session.execute.call_args_list
        assert str(calls[2].args[0]) == str(q.get_weaving_plan_driver_log_query())
        assert calls[2].args[1]["spell_id"] == 1
        assert str(calls[3].args[0]) == str(q.get_weaving_plan_driver_query())
        assert calls[3].args[1]["spell_id"] == 2
        status = {s["spell_id"]: s for s in resp.json()["data"]["sync_status"]}
        assert status[1] == {"spell_id": 1, "stored": True, "locked": False,
                             "stale": False, "synced_at": "2026-08-21 10:00:00"}
        assert status[2] == {"spell_id": 2, "stored": False, "locked": False,
                             "stale": False, "synced_at": None}
        assert len(resp.json()["data"]["rows"]) == 2

    def test_cowide_read_branch_scoped_header_not_stored(self):
        """A branch-scoped header does NOT cover a co-wide read (the log would
        miss other branches' rows): live path, sync_status stored=False."""
        header = MagicMock(spell_id=1, branch_id=2, is_locked=0, reprocess_needed=0,
                           synced_date_time="2026-08-21 10:00:00")
        self._session.execute.side_effect = [
            _fetchall_exec([header]),           # day headers: branch-scoped only
            _fetchall_exec([_driver_row(1)]),   # single LIVE driver call
        ]
        resp = client.get("/api/weavingProd/planning_grid?co_id=1&tran_date=2026-08-21")
        assert resp.status_code == 200, resp.text
        assert self._session.execute.call_count == 2
        assert str(self._session.execute.call_args_list[1].args[0]) == \
            str(q.get_weaving_plan_driver_query())
        status = resp.json()["data"]["sync_status"]
        assert status == [{"spell_id": 1, "stored": False, "locked": False,
                           "stale": False, "synced_at": "2026-08-21 10:00:00"}]


# =============================================================================
# fetch_weaving_day_rows — reports: fresh stored per spell, live otherwise
# =============================================================================


class TestFetchWeavingDayRows:
    def test_no_fresh_headers_one_live_call(self):
        db = MagicMock()
        live = _mock_row({"weaving_daily_id": 1, "production_kg": 10.0})
        db.execute.side_effect = [
            _fetchall_exec([]),        # day headers: none
            _fetchall_exec([live]),    # whole-day live slice
        ]
        rows = fetch_weaving_day_rows(db, 1, "2026-08-21", 2)
        assert rows == [{"weaving_daily_id": 1, "production_kg": 10.0}]
        assert db.execute.call_count == 2
        assert str(db.execute.call_args_list[1].args[0]) == \
            str(q.get_weaving_entries_by_date_query())
        assert db.execute.call_args_list[1].args[1]["spell_id"] is None

    def test_fresh_header_serves_log_for_its_spell_live_for_others(self):
        db = MagicMock()
        header = MagicMock(spell_id=1, branch_id=None, reprocess_needed=0)
        log_row = _mock_row({"weaving_daily_id": 1, "spell_id": 1})
        live_row = _mock_row({"weaving_daily_id": 2, "spell_id": 2})
        db.execute.side_effect = [
            _fetchall_exec([header]),                              # day headers
            _fetchall_exec([MagicMock(spell_id=1), MagicMock(spell_id=2)]),  # daily spells
            _fetchall_exec([log_row]),                             # spell 1 -> log
            _fetchall_exec([live_row]),                            # spell 2 -> live
        ]
        rows = fetch_weaving_day_rows(db, 1, "2026-08-21", None)
        assert [r["weaving_daily_id"] for r in rows] == [1, 2]
        calls = db.execute.call_args_list
        assert str(calls[2].args[0]) == str(q.get_weaving_log_rows_query())
        assert calls[2].args[1]["spell_id"] == 1
        assert str(calls[3].args[0]) == str(q.get_weaving_entries_by_date_query())
        assert calls[3].args[1]["spell_id"] == 2

    def test_stale_header_treated_as_not_fresh(self):
        """reprocess_needed=1 -> the header does NOT count as fresh: a report must
        never silently serve stale cached numbers -> whole day recomputes live."""
        db = MagicMock()
        header = MagicMock(spell_id=1, reprocess_needed=1)
        live = _mock_row({"weaving_daily_id": 3})
        db.execute.side_effect = [
            _fetchall_exec([header]),  # day headers: stale only
            _fetchall_exec([live]),    # whole-day live slice
        ]
        rows = fetch_weaving_day_rows(db, 1, "2026-08-21", 2)
        assert rows == [{"weaving_daily_id": 3}]
        assert db.execute.call_count == 2
        assert str(db.execute.call_args_list[1].args[0]) == \
            str(q.get_weaving_entries_by_date_query())

    def test_spell_with_any_stale_header_serves_live(self):
        """Two headers for one spell, one stale -> the whole spell recomputes
        live (never mix fresh and stale stored rows)."""
        db = MagicMock()
        fresh_h = MagicMock(spell_id=1, branch_id=None, reprocess_needed=0)
        stale_h = MagicMock(spell_id=1, branch_id=2, reprocess_needed=1)
        live = _mock_row({"weaving_daily_id": 4})
        db.execute.side_effect = [
            _fetchall_exec([fresh_h, stale_h]),  # day headers: same spell
            _fetchall_exec([live]),              # whole-day live slice
        ]
        rows = fetch_weaving_day_rows(db, 1, "2026-08-21", None)
        assert rows == [{"weaving_daily_id": 4}]
        assert str(db.execute.call_args_list[1].args[0]) == \
            str(q.get_weaving_entries_by_date_query())


# =============================================================================
# SQL-text guards (style of test_weaving_dayslice.py's sanity class)
# =============================================================================


def _select_aliases(sql: str):
    """Output alias list of a query's top-level SELECT (before the first FROM)."""
    select_list = sql.split("FROM", 1)[0].split("SELECT", 1)[1]
    aliases = []
    for part in select_list.split(","):
        part = part.strip()
        if " AS " in part:
            aliases.append(part.split(" AS ")[-1].strip())
        else:
            aliases.append(part.split(".")[-1].strip())
    return aliases


class TestSyncSqlGuards:
    def test_unit_reprocess_flag_no_longer_requires_lock(self):
        """Sync-only headers must be stale-flaggable too: the is_locked = 1
        predicate is gone from flag_weaving_unit_reprocess_query."""
        sql = str(q.flag_weaving_unit_reprocess_query())
        assert "is_locked = 1" not in sql
        assert "is_locked" not in sql
        assert "reprocess_needed = 1" in sql

    def test_new_flag_queries_guard_on_log_exists(self):
        """The pick/target/stoppage flags only touch headers whose stored log rows
        actually involve the mutated ref (EXISTS on jute_prod_weaving_log)."""
        for builder in [q.flag_weaving_stale_for_pick_query,
                        q.flag_weaving_stale_for_target_query,
                        q.flag_weaving_stale_for_stoppage_query]:
            sql = str(builder())
            assert "EXISTS" in sql, builder.__name__
            assert "jute_prod_weaving_log" in sql, builder.__name__
            assert "reprocess_needed = 1" in sql, builder.__name__

    def test_pick_flag_bounds_at_next_reading_date(self):
        sql = str(q.flag_weaving_stale_for_pick_query())
        assert "MIN(p.entry_date)" in sql
        assert ":entry_date" in sql and ":weaving_quality_id" in sql

    def test_lock_upserts_stamp_sync_columns(self):
        """Process implies a sync stamp: both the lock insert and re-lock update
        carry synced_by/synced_date_time now."""
        for builder in [q.insert_weaving_process_lock_query,
                        q.update_weaving_process_lock_query]:
            sql = str(builder())
            assert "synced_by" in sql, builder.__name__
            assert "synced_date_time" in sql, builder.__name__

    def test_sync_header_insert_is_unlocked_and_unprocessed(self):
        sql = str(q.insert_weaving_sync_header_query())
        assert "is_locked" in sql
        assert "processed_by" not in sql  # sync never stamps a process
        assert "synced_by" in sql

    def test_header_synced_update_keeps_lock_state(self):
        sql = str(q.update_weaving_header_synced_query())
        assert "is_locked" not in sql  # a locked unit re-synced stays locked
        assert "reprocess_needed = 0" in sql

    def test_log_driver_joins_log_and_matches_live_driver_aliases(self):
        """The stored-snapshot driver must be a drop-in for the live one: same
        output aliases, same order, but sourced from jute_prod_weaving_log."""
        log_sql = str(q.get_weaving_plan_driver_log_query())
        live_sql = str(q.get_weaving_plan_driver_query())
        assert "jute_prod_weaving_log" in log_sql
        assert "jute_prod_weaving_daily" not in log_sql  # no live recompute
        assert _select_aliases(log_sql) == _select_aliases(live_sql)

    def test_day_headers_query_exposes_switch_columns(self):
        sql = str(q.get_weaving_day_headers_query())
        for col in ["spell_id", "branch_id", "is_locked", "reprocess_needed",
                    "processed_date_time", "synced_date_time"]:
            assert col in sql, col

    def test_get_process_lock_prefers_locked_header(self):
        """A LOCKED header must win over a newer sync-only one — and the row
        carries branch_id for scope checks."""
        db = MagicMock()
        get_process_lock(db, 1, 2, "2026-08-21", 91)
        sql = str(db.execute.call_args.args[0])
        assert "is_locked DESC, weaving_process_lock_id DESC" in sql
        assert "branch_id" in sql.split("FROM")[0]  # selected, not just filtered

    def test_header_upsert_probe_is_scope_strict(self):
        """The sync/process header UPSERT probe must match the EXACT run scope —
        a branch-scoped run must never find (and fresh-stamp) a co-wide header
        it only partially refreshed."""
        from src.juteProduction.weaving_query import get_weaving_process_lock_row_query
        sql = str(get_weaving_process_lock_row_query())
        assert "(:branch_id IS NULL AND branch_id IS NULL) OR branch_id = :branch_id" in sql
        # The old NULL-tolerant pass-through must be gone from the probe.
        assert "OR branch_id IS NULL)" not in sql.replace(
            "(:branch_id IS NULL AND branch_id IS NULL) OR branch_id = :branch_id", ""
        )


class TestSyncRange:
    """POST /sync_range: one transaction per unit, failures reported not raised."""

    def _run(self, session, body):
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            return client.post("/api/weavingProd/sync_range", json=body)
        finally:
            app.dependency_overrides.clear()

    def test_range_syncs_each_unit_and_isolates_failures(self):
        from src.juteProduction import weaving_process as wp
        session = MagicMock()
        u1 = MagicMock(tran_date=__import__("datetime").date(2026, 9, 10), spell_id=109)
        u2 = MagicMock(tran_date=__import__("datetime").date(2026, 9, 10), spell_id=110)
        session.execute.return_value = _fetchall_exec([u1, u2])
        calls = []
        def fake_sync_unit(db, body, token_data):
            calls.append((str(body.tran_date), body.spell_id))
            if body.spell_id == 110:
                raise RuntimeError("boom")
            return {"synced": 5, "warnings": {"no_worker": [1, 2], "unmapped": []}}
        with patch.object(wp, "_sync_unit", side_effect=fake_sync_unit):
            resp = self._run(session, {"co_id": 2, "branch_id": 29,
                                       "from_date": "2026-09-10", "to_date": "2026-09-10"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["units"] == 2 and data["synced_units"] == 1
        assert data["results"][0] == {"tran_date": "2026-09-10", "spell_id": 109, "synced": 5,
                                      "warnings": {"no_worker": 2, "unmapped": 0}}
        assert data["results"][1]["error"] == "boom"
        assert calls == [("2026-09-10", 109), ("2026-09-10", 110)]
        assert session.commit.call_count == 1 and session.rollback.call_count == 1

    def test_range_rejects_bad_ranges(self):
        session = MagicMock()
        assert self._run(session, {"co_id": 2, "from_date": "2026-09-10", "to_date": "2026-09-09"}).status_code == 400
        assert self._run(session, {"co_id": 2, "from_date": "2026-01-01", "to_date": "2026-09-09"}).status_code == 400
        # cap counts INCLUSIVE days: 8 calendar days is one too many for a 7-day cap
        from src.juteProduction.constants import WEAVING_SYNC_RANGE_MAX_DAYS
        assert WEAVING_SYNC_RANGE_MAX_DAYS == 7
        assert self._run(session, {"co_id": 2, "from_date": "2026-09-01", "to_date": "2026-09-08"}).status_code == 400

    def test_range_400_when_branch_cannot_derive_company(self):
        from src.juteProduction import weaving_process as wp
        session = MagicMock()
        with patch.object(wp, "_derive_co_id", return_value=None):
            resp = self._run(session, {"branch_id": 999, "from_date": "2026-09-10", "to_date": "2026-09-10"})
        assert resp.status_code == 400
        assert "co_id" in resp.json()["detail"]
        session.rollback.assert_called_once()
