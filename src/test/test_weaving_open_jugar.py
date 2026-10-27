"""Tests for the Phase 1b stored-open_jugar chain sync in Weaving Production Entry.

Phase 1b (2026-07-07): jute_prod_weaving_daily.open_jugar is now a STORED column. Every
mutation of a daily row re-resolves the row's OWN open_jugar from its chain predecessor and
repairs the SINGLE chain successor in the same transaction
(weaving_entry._sync_jugar_chain_after_write) â€” bounded at 2-3 indexed statements per write:

  (a) resync-self SELECT   -> resolve_weaving_open_jugar_for_row_query (.fetchone().open_jugar)
  (b) resync-self UPDATE   -> update_weaving_daily_open_jugar_query
  (c) chain-successor probe -> get_weaving_chain_successor_query (.scalar() = id or None)
      when the probe finds an id, the successor gets its own SELECT + UPDATE (a')/(b').

These tests assert the extra statements fire (and carry the right binds) on the write paths
that sync â€” entry_create insert, entry_edit with a changed identity, entry_delete â€” and that
the adjustment_save UPDATE path does NOT sync (less_production never moves the jugar chain).
Plus SQL-shape sanity on the three new query builders (no DB).

Portal persona: DB + auth mocked (no real DB) via app.dependency_overrides keyed by the
get_tenant_db / get_current_user_with_refresh symbols imported into the router module,
exactly like test_weaving_entry.py. Rows expose ._mapping / attribute access; auth returns
{"user_id": 1}.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction.weaving_entry import (
    get_tenant_db,
    get_current_user_with_refresh,
)
from src.juteProduction.weaving_query import (
    get_weaving_chain_successor_query,
    resolve_weaving_open_jugar_for_row_query,
    update_weaving_daily_open_jugar_query,
)

client = TestClient(app)


import pytest as _pytest
from unittest.mock import patch as _patch


@_pytest.fixture(autouse=True)
def _bypass_weaving_lock():
    """Locking is tested in test_weaving_lock.py; bypass the guard here so the legacy
    execute-order mocks (which predate the Process lock lookup) stay aligned."""
    with _patch("src.juteProduction.weaving_entry.require_edit_if_locked"), \
         _patch("src.juteProduction.weaving_entry.is_unit_locked", return_value=False), \
         _patch("src.juteProduction.weaving_entry.flag_reprocess_if_locked"), \
         _patch("src.juteProduction.weaving_entry._flag_reprocess_units"):
        yield


# ---------------------------------------------------------------------------
# execute() result builders (mirror test_weaving_entry.py conventions)
# ---------------------------------------------------------------------------


def _spell_exec(spell_id=1, branch_id=2):
    """_resolve_spell result row: .spell_id (code path) / .branch_id (id path)."""
    r = MagicMock(); r.spell_id = spell_id; r.branch_id = branch_id
    ex = MagicMock(); ex.fetchone.return_value = r
    return ex


def _branch_exec(branch_id=2):
    r = MagicMock(); r.branch_id = branch_id
    ex = MagicMock(); ex.fetchone.return_value = r
    return ex


def _mapped_qid_exec(qid):
    ex = MagicMock()
    if qid is None:
        ex.fetchone.return_value = None
    else:
        r = MagicMock(); r.weaving_quality_id = qid
        ex.fetchone.return_value = r
    return ex


def _quality_exec(no_of_jugar_per_cut=16.0):
    r = MagicMock()
    r.weaving_quality_id = 5
    r.item_id = 10
    r.ends = 272
    r.finished_length = 120.0
    r.ozs_yds = 10.0
    r.std_ozs_yds = 10.0
    r.no_of_jugar_per_cut = no_of_jugar_per_cut
    r.is_composite = 0
    ex = MagicMock(); ex.fetchone.return_value = r
    return ex


def _scalar_exec(value):
    ex = MagicMock(); ex.scalar.return_value = value
    return ex


def _none_fetchone_exec():
    ex = MagicMock(); ex.fetchone.return_value = None
    return ex


def _open_jugar_select_exec(open_jugar):
    """resolve_weaving_open_jugar_for_row_query result: .fetchone().open_jugar."""
    ex = MagicMock(); ex.fetchone.return_value = MagicMock(open_jugar=open_jugar)
    return ex


class TestEntryCreateSyncsSuccessor:
    """entry_create insert path with a FOUND chain successor repairs it in the same txn."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_insert_with_found_successor_updates_it(self):
        """co_id + branch_id supplied, so no derive execs. Execute order:
          0. _resolve_spell_id     -> spell_id 1
          1. _mapped_quality_id    -> qid 5
          2. _fetch_quality        -> quality (jc=16)
          3. latest beam_no        -> scalar
          4. active-row lookup     -> None (insert path)
          5. insert_weaving_daily  -> lastrowid 555
          6. resync-self open_jugar SELECT   -> 4.0 (this row)
          7. resync-self open_jugar UPDATE
          8. chain-successor probe -> .scalar() = 77 (FOUND)
          9. successor open_jugar SELECT     -> 9.0
         10. successor open_jugar UPDATE     -> {"row_id": 77, "open_jugar": 9.0}
        """
        insert_exec = MagicMock(); insert_exec.lastrowid = 555
        self._mock_session.execute.side_effect = [
            _spell_exec(1),
            _mapped_qid_exec(5),
            _quality_exec(16.0),
            _scalar_exec("BEAM-007"),
            _none_fetchone_exec(),
            insert_exec,
            _open_jugar_select_exec(4.0),   # resync-self SELECT
            MagicMock(),                    # resync-self UPDATE
            _scalar_exec(77),               # chain-successor probe -> FOUND id 77
            _open_jugar_select_exec(9.0),   # successor SELECT
            MagicMock(),                    # successor UPDATE
        ]

        body = {
            "co_id": 1,
            "branch_id": 2,
            "tran_date": "2026-06-21",
            "spell": "A1",
            "machine_id": 7,
            "cuts": 10,
            "close_jugar": 12,
        }
        response = client.post("/api/weavingProd/entry_create", json=body)

        assert response.status_code == 200
        assert response.json()["data"]["weaving_daily_id"] == 555
        self._mock_session.commit.assert_called_once()

        # 11 executes total: the successor was FOUND, so its SELECT + UPDATE ran.
        assert self._mock_session.execute.call_count == 11
        # The final UPDATE repairs the successor's open_jugar from the value the
        # successor SELECT resolved (9.0), keyed by the successor id (77).
        final_update = self._mock_session.execute.call_args_list[10].args[1]
        assert final_update == {"row_id": 77, "open_jugar": 9.0}


class TestEntryEditChangedIdentitySyncsOldChain:
    """entry_edit with a CHANGED machine_id repairs BOTH the new and OLD chain positions."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_changed_machine_id_probes_old_position_successor(self):
        """Body sends a NEW machine_id (9 != existing 7), so the row moves between jugar
        chains and the OLD position's successor is also probed. Execute order:
          0. existing daily row lookup -> existing (machine_id 7, qid 5, spell 1)
          1. _mapped_quality_id        -> qid 5 (new machine 9)
          2. _fetch_quality            -> quality
          3. _derive_branch_id         -> branch row
          4. latest beam_no            -> scalar
          5. UPDATE edited row
          6. resync-self open_jugar SELECT
          7. resync-self open_jugar UPDATE
          8. chain-successor probe (NEW machine 9) -> None
          9. OLD-position chain-successor probe (machine 7) -> None
        """
        existing = MagicMock()
        existing.weaving_daily_id = 900
        existing.co_id = 1
        existing.branch_id = 2
        existing.tran_date = "2026-06-21"
        existing.spell_id = 1
        existing.machine_id = 7
        existing.weaving_quality_id = 5
        existing.eb_id = None
        existing.beam_no = "BEAM-OLD"
        existing.cuts = 4.0
        existing.close_jugar = 2.0
        existing.less_production = 0.0
        existing_exec = MagicMock(); existing_exec.fetchone.return_value = existing

        self._mock_session.execute.side_effect = [
            existing_exec,
            _mapped_qid_exec(5),
            _quality_exec(16.0),
            _branch_exec(2),
            _scalar_exec("BEAM-007"),
            MagicMock(),                 # UPDATE
            _open_jugar_select_exec(4.0),  # resync-self SELECT
            MagicMock(),                 # resync-self UPDATE
            _scalar_exec(None),          # NEW-position successor probe -> None
            _scalar_exec(None),          # OLD-position successor probe -> None
        ]

        response = client.put(
            "/api/weavingProd/entry_edit/900?co_id=1",
            json={"machine_id": 9, "cuts": 12, "close_jugar": 6},
        )

        assert response.status_code == 200
        self._mock_session.commit.assert_called_once()

        # The extra OLD-chain probe brings the count to 10 (9 without it).
        assert self._mock_session.execute.call_count == 10
        # The self-sync successor probe carries the NEW machine_id (9)...
        new_probe = self._mock_session.execute.call_args_list[8].args[1]
        assert new_probe["machine_id"] == 9
        # ...and the extra probe carries the OLD machine_id (7) â€” the old position repair.
        old_probe = self._mock_session.execute.call_args_list[9].args[1]
        assert old_probe["machine_id"] == 7


class TestEntryDeleteRepairsSuccessor:
    """entry_delete soft-deletes then repairs the deleted row's single successor."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_delete_repairs_found_successor(self):
        """Existence row carries machine_id/weaving_quality_id/tran_date/spell_id. Order:
          0. existence lookup      -> existing (machine 7, qid 5, spell 1)
          1. soft-delete UPDATE
          2. chain-successor probe -> .scalar() = 88 (FOUND)
          3. successor open_jugar SELECT -> 3.0
          4. successor open_jugar UPDATE -> {"row_id": 88, "open_jugar": 3.0}
        """
        existing = MagicMock()
        existing.weaving_daily_id = 900
        existing.machine_id = 7
        existing.weaving_quality_id = 5
        existing.tran_date = "2026-06-21"
        existing.spell_id = 1
        existing_exec = MagicMock(); existing_exec.fetchone.return_value = existing

        self._mock_session.execute.side_effect = [
            existing_exec,
            MagicMock(),                 # soft-delete UPDATE
            _scalar_exec(88),            # successor probe -> FOUND id 88
            _open_jugar_select_exec(3.0),  # successor SELECT
            MagicMock(),                 # successor UPDATE
        ]

        response = client.delete("/api/weavingProd/entry_delete/900?co_id=1")

        assert response.status_code == 200
        self._mock_session.commit.assert_called_once()

        assert self._mock_session.execute.call_count == 5
        # The successor probe runs on the deleted row's OWN identity (machine 7).
        probe = self._mock_session.execute.call_args_list[2].args[1]
        assert probe["machine_id"] == 7
        # The found successor's open_jugar is repaired from its resolved value.
        final_update = self._mock_session.execute.call_args_list[4].args[1]
        assert final_update == {"row_id": 88, "open_jugar": 3.0}

    def test_delete_null_quality_row_still_repairs_successor(self):
        """The chain is per loom (quality is not a criterion), so deleting a
        NULL-quality row still repairs its successor. Order: existence lookup,
        soft-delete UPDATE, successor probe (-> None)."""
        existing = MagicMock()
        existing.weaving_daily_id = 901
        existing.machine_id = 7
        existing.weaving_quality_id = None
        existing.tran_date = "2026-06-21"
        existing.spell_id = 1
        existing_exec = MagicMock(); existing_exec.fetchone.return_value = existing

        self._mock_session.execute.side_effect = [
            existing_exec,
            MagicMock(),        # soft-delete UPDATE
            _scalar_exec(None), # successor probe -> none
        ]

        response = client.delete("/api/weavingProd/entry_delete/901?co_id=1")

        assert response.status_code == 200, response.text
        assert self._mock_session.execute.call_count == 3
        probe = self._mock_session.execute.call_args_list[2].args[1]
        assert probe["machine_id"] == 7 and "weaving_quality_id" not in probe
        self._mock_session.commit.assert_called_once()


class TestAdjustmentSaveUpdatePathNoSync:
    """adjustment_save UPDATE path touches ONLY less_production â€” no open_jugar sync."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_existing_row_update_runs_no_open_jugar_statements(self):
        """branch_id supplied (so _resolve_spell_id is a single execute). Order:
          0. _resolve_spell_id   -> spell_id 1
          1. _mapped_quality_id  -> qid 5
          2. active-row lookup   -> existing daily row (555)
          3. update less_production
        No chain sync: less_production never moves the jugar chain (update path).
        """
        active_exec = MagicMock()
        existing = MagicMock(); existing.weaving_daily_id = 555
        active_exec.fetchone.return_value = existing
        self._mock_session.execute.side_effect = [
            _spell_exec(1),
            _mapped_qid_exec(5),
            active_exec,
            MagicMock(),  # update less_production
        ]

        body = {
            "co_id": 1,
            "branch_id": 2,
            "tran_date": "2026-06-24",
            "spell": "A1",
            "entries": [{"machine_id": 7, "less_production": 4.0}],
        }
        response = client.post("/api/weavingProd/adjustment_save", json=body)

        assert response.status_code == 200
        assert response.json()["data"]["saved"] == 1
        self._mock_session.commit.assert_called_once()

        # Exactly 4 executes â€” no resync-self SELECT/UPDATE, no successor probe.
        assert self._mock_session.execute.call_count == 4
        # None of the executed statements is an open_jugar write (those bind "row_id"/"open_jugar").
        for call in self._mock_session.execute.call_args_list:
            params = call.args[1] if len(call.args) > 1 else {}
            assert "open_jugar" not in params
            assert "row_id" not in params


class TestPlanningGridSaveSyncsPerRow:
    """planning_grid_save runs the per-row chain sync after each upsert (5th call site)."""

    def setup_method(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_insert_row_syncs_self_and_found_successor(self):
        """co_id + branch_id supplied. One row, insert path. Execute order:
          0. _resolve_spell (row spell_id, id path, branch-validated)
          1. _mapped_quality_id    -> qid 5
          2. _fetch_quality        -> quality (jc=16)
          3. latest beam_no        -> scalar
          4. active-row lookup     -> None (insert path)
          5. insert_weaving_daily  -> lastrowid 601
          6. resync-self open_jugar SELECT -> 2.0
          7. resync-self open_jugar UPDATE
          8. chain-successor probe -> .scalar() = 66 (FOUND)
          9. successor open_jugar SELECT -> 5.0
         10. successor open_jugar UPDATE -> {"row_id": 66, "open_jugar": 5.0}
        """
        insert_exec = MagicMock(); insert_exec.lastrowid = 601
        self._mock_session.execute.side_effect = [
            _spell_exec(1, branch_id=2),
            _mapped_qid_exec(5),
            _quality_exec(16.0),
            _scalar_exec("BEAM-001"),
            _none_fetchone_exec(),
            insert_exec,
            _open_jugar_select_exec(2.0),
            MagicMock(),
            _scalar_exec(66),
            _open_jugar_select_exec(5.0),
            MagicMock(),
        ]

        body = {
            "co_id": 1,
            "branch_id": 2,
            "tran_date": "2026-06-21",
            "rows": [
                {"spell_id": 1, "machine_id": 7, "cuts": 8, "close_jugar": 4.0},
            ],
        }
        response = client.post("/api/weavingProd/planning_grid_save", json=body)

        assert response.status_code == 200
        assert response.json()["data"]["saved"] == 1
        self._mock_session.commit.assert_called_once()

        assert self._mock_session.execute.call_count == 11
        final_update = self._mock_session.execute.call_args_list[10].args[1]
        assert final_update == {"row_id": 66, "open_jugar": 5.0}


class TestResyncNoOpWhenRowVanished:
    """_resync_open_jugar_row skips the UPDATE when the row id no longer resolves."""

    def test_resync_skips_update_when_select_returns_none(self):
        from src.juteProduction.weaving_entry import _resync_open_jugar_row

        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        _resync_open_jugar_row(db, 42)
        # Only the SELECT ran â€” no open_jugar UPDATE for a vanished row.
        assert db.execute.call_count == 1


class TestChainQuerySqlShape:
    """SQL-shape sanity on the three Phase 1b query builders (no DB)."""

    def test_successor_query_binds_and_shape(self):
        sql = str(get_weaving_chain_successor_query())
        assert ":row_id" in sql
        assert ":spell_id" in sql
        assert "LIMIT 1" in sql
        # 2026-09-16: chain order = tran_date + spell starting_time (never spell code)
        assert "starting_time" in sql
        assert "tran_date > :tran_date" in sql

    def test_open_jugar_update_does_not_stamp_updated_by(self):
        # open_jugar is derived data â€” repairing a successor must NOT masquerade as a user edit.
        sql = str(update_weaving_daily_open_jugar_query())
        assert "updated_by" not in sql
        assert "open_jugar" in sql

    def test_resolve_open_jugar_for_row_binds_row_id(self):
        sql = str(resolve_weaving_open_jugar_for_row_query())
        assert ":row_id" in sql


class TestChainOrderEffectiveStart:
    """2026-09-16 ruling (TKT-000150/156): the jugar chain follows clock time on the
    transaction date. start = tran_date + spell starting_time; an overnight spell is
    dated by the day it starts and sorts LAST on that date; the legacy spell-code
    rank survives only as a tiebreak for spells sharing a start time."""

    def test_probe_orders_by_start_time_not_spell_code(self):
        from src.juteProduction.weaving_query import _open_jugar_probe_sql
        sql = _open_jugar_probe_sql()
        assert "COALESCE(ps.starting_time, '23:59:59')" in sql
        # the overnight flag must NOT move a row to another date
        assert "is_overnight" not in sql
        # same-day probe + prior-day probe, both indexed on tran_date
        assert sql.count("LIMIT 1") == 2
        assert "p.tran_date = wd.tran_date" in sql and "p.tran_date < wd.tran_date" in sql
        # rank CASE is present only as the SECOND sort key (tiebreak)
        assert "DESC, CASE ps.spell_code" in sql

    def test_chain_start_overnight_stays_on_its_own_date(self):
        import datetime as dt
        from src.juteProduction.services.weaving_rules import chain_effective_start
        d = dt.date(2026, 9, 14)
        a = chain_effective_start(d, dt.time(6, 0), 0)
        b = chain_effective_start(d, dt.time(14, 0), 0)
        c = chain_effective_start(d, dt.time(22, 0), 1)          # C dated 14-Sep = night 14 -> 15
        a_next = chain_effective_start(dt.date(2026, 9, 15), dt.time(6, 0), 0)
        assert c == dt.datetime(2026, 9, 14, 22, 0)
        assert a < b < c < a_next                                 # A(14) B(14) C(14) A(15)

    def test_chain_start_tolerates_missing_time_and_timedelta(self):
        import datetime as dt
        from src.juteProduction.services.weaving_rules import chain_effective_start
        d = dt.date(2026, 9, 10)
        assert chain_effective_start(d, None, None) == dt.datetime(2026, 9, 10, 23, 59, 59)
        assert chain_effective_start(d, dt.timedelta(hours=22), 1) == dt.datetime(2026, 9, 10, 22, 0)


class TestChainGapCapAndSyncWindow:
    """2026-09-16: gap cap + Sync resolves the unit with one window query."""

    def test_probe_and_successor_are_bounded_by_the_gap_cap(self):
        from src.juteProduction.constants import WEAVING_CHAIN_MAX_GAP_DAYS
        from src.juteProduction.weaving_query import _open_jugar_probe_sql, get_weaving_chain_successor_query
        assert f"INTERVAL {WEAVING_CHAIN_MAX_GAP_DAYS} DAY" in _open_jugar_probe_sql()
        assert f"INTERVAL {WEAVING_CHAIN_MAX_GAP_DAYS} DAY" in str(get_weaving_chain_successor_query())

    def test_unit_resolve_is_a_single_window_with_the_same_order_and_cap(self):
        from src.juteProduction.constants import WEAVING_CHAIN_MAX_GAP_DAYS
        from src.juteProduction.weaving_query import get_weaving_unit_open_jugar_query
        sql = str(get_weaving_unit_open_jugar_query())
        assert sql.count("LAG(") == 3 and "WINDOW chain AS" in sql
        assert "PARTITION BY w.co_id, w.machine_id, w.weaving_quality_id" in sql
        assert "ORDER BY w.tran_date, TIMESTAMP(w.tran_date, COALESCE(sp.starting_time, '23:59:59')), CASE sp.spell_code" in sql
        assert f"> {WEAVING_CHAIN_MAX_GAP_DAYS}" in sql and "THEN 0" in sql
        for b in (":co_id", ":tran_date", ":spell_id", ":branch_id"):
            assert b in sql
        assert "stored_value" in sql and "open_jugar" in sql


class TestAttendanceJoinsStartFromTheDay:
    """2026-09-16 perf: every attendance read in the Sync path starts from
    daily_attendance on the date (index seek), never from the 1.2M-row machine
    marks table."""

    def test_no_worker_candidates_and_slice_start_from_daily_attendance(self):
        from src.juteProduction.weaving_query import (
            get_weaving_process_no_worker_query, get_weaving_operator_candidates_query,
            weaving_day_slice_sql, get_weaving_drift_query)
        for sql in (str(get_weaving_process_no_worker_query()), str(get_weaving_operator_candidates_query()),
                    weaving_day_slice_sql(), str(get_weaving_drift_query())):
            assert "FROM daily_attendance da" in sql
            assert "JOIN daily_ebmc_attendance" in sql
            assert sql.index("FROM daily_attendance da") < sql.index("daily_ebmc_attendance")
            assert "FROM daily_ebmc_attendance" not in sql


class TestNullQualityNeverChains:
    """Capture-before-map rows (weaving_quality_id NULL) open at 0 in EVERY
    implementation: the per-row probe (= on NULL finds nothing), the Sync window,
    the view oracle, the backfill and the parity check. PARTITION BY groups NULLs,
    so each set-based twin needs the explicit rule or Sync and entry-save flap."""

    def test_window_and_sql_twins_carry_the_rule(self):
        import io
        from src.juteProduction.weaving_query import get_weaving_unit_open_jugar_query
        assert "w.weaving_quality_id IS NULL THEN 0" in str(get_weaving_unit_open_jugar_query())
        for path, needle in (
            ("dbqueries/migrations/2026-09-15_weaving_chain_effective_start.sql", "wd.weaving_quality_id IS NULL THEN 0"),
            ("dbqueries/migrations/create_weaving_tables.sql", "wd.weaving_quality_id IS NULL THEN 0"),
            ("dbqueries/migrations/backfill_weaving_open_jugar.sql", "x.weaving_quality_id IS NULL THEN 0"),
            ("dbqueries/check_weaving_open_jugar_parity.sql", "x.weaving_quality_id IS NULL THEN 0"),
        ):
            assert needle in io.open(path, encoding="utf-8").read(), path

    def test_gap_cap_literal_in_sql_files_matches_the_constant(self):
        import io, re
        from src.juteProduction.constants import WEAVING_CHAIN_MAX_GAP_DAYS
        for path in ("dbqueries/migrations/2026-09-15_weaving_chain_effective_start.sql",
                     "dbqueries/migrations/create_weaving_tables.sql",
                     "dbqueries/migrations/backfill_weaving_open_jugar.sql",
                     "dbqueries/check_weaving_open_jugar_parity.sql"):
            txt = io.open(path, encoding="utf-8").read()
            caps = set(re.findall(r"OVER w_chain\) > (\d+) THEN 0", txt))
            assert caps == {str(WEAVING_CHAIN_MAX_GAP_DAYS)}, (path, caps)
