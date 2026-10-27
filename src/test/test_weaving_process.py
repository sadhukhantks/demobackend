"""Weaving Process: builder smoke, BLOCK, happy-path freeze, DB-guarded parity."""
import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProduction import weaving_query as q
from src.juteProduction.weaving_process import _resolve_operators

client = TestClient(app)


def test_process_builders_compile():
    for name in [
        "get_weaving_daily_active_row_by_machine_query",
        "get_weaving_unit_daily_ids_query", "get_weaving_unmapped_produced_looms_query",
        "get_weaving_process_quality_mismatch_query",
        "get_weaving_process_negative_jugar_query",
        "get_weaving_process_no_worker_query", "get_weaving_process_no_standard_query",
        "get_weaving_process_no_picks_query", "soft_delete_weaving_log_for_unit_query",
        "insert_weaving_log_from_slice_query",
        "get_weaving_operator_candidates_query", "clear_weaving_daily_eb_query",
        "stamp_weaving_daily_eb_query", "get_weaving_unit_open_jugar_query",
        "get_weaving_process_lock_row_query", "insert_weaving_process_lock_query",
        "update_weaving_process_lock_query", "update_weaving_process_lock_reprocess_query",
        "get_weaving_drift_query", "get_weaving_log_rows_query",
    ]:
        assert getattr(q, name)() is not None


class TestProcessEndpoint:
    def _run(self, session, **extra):
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            with patch("src.juteProduction.weaving_process._resolve_spell", return_value=91), \
                 patch("src.juteProduction.weaving_process._derive_co_id", return_value=1), \
                 patch("src.juteProduction.weaving_process.require_edit_if_locked"):
                return client.post("/api/weavingProd/process", json={
                    "co_id": 1, "branch_id": 2, "tran_date": "2026-07-07", "spell": "A1",
                    **extra})
        finally:
            app.dependency_overrides.clear()

    def test_block_when_unmapped(self):
        session = MagicMock()
        row = MagicMock()
        row._mapping = {"machine_id": 42, "mech_code": "L42", "machine_name": "Loom 42"}
        session.execute.return_value.fetchall.return_value = [row]  # unmapped -> BLOCK
        resp = self._run(session)
        assert resp.status_code == 400
        assert resp.json()["detail"]["unmapped"][0]["machine_id"] == 42

    def test_process_400_when_co_id_underivable(self):
        """Regression: body omitting co_id AND branch_id must 400, not int(None) -> 500."""
        session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            with patch("src.juteProduction.weaving_process._resolve_spell", return_value=91):
                resp = client.post("/api/weavingProd/process", json={
                    "tran_date": "2026-07-07", "spell": "A1"})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 400
        assert "co_id" in resp.json()["detail"]

    def test_process_freezes_and_warns(self):
        session = MagicMock()
        empty = MagicMock(fetchall=MagicMock(return_value=[]))
        insert = MagicMock(rowcount=5)
        lockrow = MagicMock(fetchone=MagicMock(return_value=None))
        unit_ids = MagicMock(fetchall=MagicMock(return_value=[]))
        # execute() order: unmapped, quality_mismatch, no_worker, no_standard, no_picks,
        # null_branch (branch-scoped run), unit rows, operator candidates, eb clear
        # (+ ONE bulk eb stamp when any loom resolved), unit open_jugar SELECT
        # (+ ONE bulk UPDATE when any row changed), negative_jugar, soft_delete,
        # INSERT, lock_row, lock_insert. The eb stamp is BEFORE the freeze (the log
        # inherits eb_id from the slice) — no post-freeze log stamp any more.
        session.execute.side_effect = [empty, empty, empty, empty, empty, empty,
                                       unit_ids, empty, MagicMock(), empty,
                                       empty, MagicMock(), insert,
                                       lockrow, MagicMock()]
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["processed"] == 5
        warns = resp.json()["data"]["warnings"]
        assert warns["no_worker"] == []
        assert warns["negative_jugar"] == []
        assert "null_branch_rows" not in warns  # only added when rows come back

    def test_warns_negative_jugar(self):
        """Regression: a row whose straight-count total_jugar goes negative is
        surfaced in warnings["negative_jugar"] (never clamped or blocked)."""
        session = MagicMock()
        empty = MagicMock(fetchall=MagicMock(return_value=[]))
        neg_row = MagicMock()
        neg_row._mapping = {"machine_id": 7, "mech_code": "L07", "machine_name": "Loom 7"}
        neg = MagicMock(fetchall=MagicMock(return_value=[neg_row]))
        insert = MagicMock(rowcount=1)
        lockrow = MagicMock(fetchone=MagicMock(return_value=None))
        session.execute.side_effect = [empty, empty, empty, empty, empty, empty,
                                       empty, MagicMock(),
                                       empty, empty, neg, MagicMock(), insert,
                                       lockrow, MagicMock()]
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        warns = resp.json()["data"]["warnings"]
        assert warns["negative_jugar"] == [
            {"machine_id": 7, "mech_code": "L07", "machine_name": "Loom 7"}]

    def test_branch_id_bound_on_every_unit_query(self):
        """Regression: the body's branch_id must ride in the binds of every unit-scoped
        statement (BLOCK probes, WARN collectors, unit ids, soft-delete, freeze INSERT)
        so branch A's Process never re-freezes branch B's rows."""
        session = MagicMock()
        empty = MagicMock(fetchall=MagicMock(return_value=[]))
        insert = MagicMock(rowcount=0)
        lockrow = MagicMock(fetchone=MagicMock(return_value=None))
        session.execute.side_effect = [empty, empty, empty, empty, empty, empty,
                                       empty, MagicMock(),
                                       empty, empty, empty, MagicMock(), insert,
                                       lockrow, MagicMock()]
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        # calls 0-10: unmapped, mismatch, no_worker, no_standard, no_picks,
        # null_branch, unit rows, operator candidates, eb clear, unit open_jugar,
        # negative_jugar; 11: soft-delete; 12: freeze INSERT.
        for i in range(13):
            binds = session.execute.call_args_list[i].args[1]
            assert binds["branch_id"] == 2, f"call {i} missing branch_id bind"
        # And the unit-query SQL actually carries the NULL-tolerant predicate.
        for builder in [q.get_weaving_unit_daily_ids_query,
                        q.get_weaving_unmapped_produced_looms_query,
                        q.get_weaving_process_quality_mismatch_query,
                        q.get_weaving_process_negative_jugar_query,
                        q.get_weaving_unit_open_jugar_query,
                        q.soft_delete_weaving_log_for_unit_query]:
            assert ":branch_id IS NULL OR" in str(builder())

    def _eb_side_effects(self, unit_rows, cands, open_jugar_rows=()):
        """side_effect list for a run that actually resolves workers: calls
        6 unit rows, 7 candidates, 8 eb clear, then ONE bulk stamp if any loom
        resolved, the unit open_jugar SELECT, ONE bulk UPDATE if any row changed."""
        empty = MagicMock(fetchall=MagicMock(return_value=[]))
        unit_mcs = {int(r.machine_id) for r in unit_rows}
        stamped = {int(c.mc_id) for c in cands if int(c.mc_id) in unit_mcs}
        changed = [r for r in open_jugar_rows
                   if r.stored_value is None or float(r.stored_value) != float(r.open_jugar or 0)]
        carried = [r for r in open_jugar_rows
                   if float(r.cuts or 0) == 0
                   and float(r.close_jugar or 0) in (0.0, float(r.stored_value or 0))
                   and float(r.close_jugar or 0) != float(r.open_jugar or 0)]
        return ([empty] * 6
                + [MagicMock(fetchall=MagicMock(return_value=unit_rows)),
                   MagicMock(fetchall=MagicMock(return_value=cands)),
                   MagicMock()]                                   # eb clear
                + [MagicMock()] * (1 if stamped else 0)           # bulk eb stamp
                + [MagicMock(fetchall=MagicMock(return_value=list(open_jugar_rows)))]
                + [MagicMock()] * (1 if changed else 0)           # bulk open_jugar
                + [MagicMock()] * (1 if carried else 0)           # idle close carry
                + [empty, MagicMock(), MagicMock(rowcount=1),
                   MagicMock(fetchone=MagicMock(return_value=None)), MagicMock()])

    def test_eb_stamp_scoped_to_unit_looms(self):
        """Attendance covers the whole branch: a machine with no daily row in the
        unit (another department's) must be neither stamped nor reported."""
        session = MagicMock()
        unit_rows = [MagicMock(weaving_daily_id=1, machine_id=7)]
        cands = [_cand(7, 200, 9), _cand(7, 100, 4),      # ambiguous loom
                 _cand(99, 300, 2), _cand(99, 400, 3)]    # NOT in the unit
        session.execute.side_effect = self._eb_side_effects(unit_rows, cands)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        # Only the unit's loom is reported ambiguous — machine 99 is dropped.
        assert resp.json()["data"]["warnings"]["operator_ambiguous"] == [
            {"machine_id": 7, "eb_ids": [100, 200], "stamped_eb_id": 100}]
        # ...and exactly one bulk stamp fired, carrying only loom 7 with the
        # first-marked eb (machine 99 never reaches the CASE).
        stamps = [c for c in session.execute.call_args_list
                  if str(c.args[0]) == str(q.stamp_weaving_daily_eb_bulk_query(1))]
        assert len(stamps) == 1
        assert stamps[0].args[1]["m0"] == 7
        assert stamps[0].args[1]["e0"] == 100
        assert stamps[0].args[1]["branch_id"] == 2
        assert "m1" not in stamps[0].args[1]

    def test_open_jugar_written_back_only_where_changed(self):
        """TKT-000134: the unit's open_jugar is resolved in ONE SELECT and written
        back in ONE CASE UPDATE that carries only the rows whose stored value
        differs (NULL counts as different) — never a per-row round trip."""
        session = MagicMock()
        unit_rows = [MagicMock(weaving_daily_id=i, machine_id=i) for i in (1, 2, 3, 4)]
        oj = [MagicMock(weaving_daily_id=1, stored_value=5.0, open_jugar=5.0),    # unchanged
              MagicMock(weaving_daily_id=2, stored_value=None, open_jugar=0),     # NULL -> 0
              MagicMock(weaving_daily_id=3, stored_value=2.0, open_jugar=7.5),    # changed
              MagicMock(weaving_daily_id=4, stored_value=0.0, open_jugar=None)]   # None -> 0
        session.execute.side_effect = self._eb_side_effects(unit_rows, [], oj)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        writes = [c for c in session.execute.call_args_list
                  if "SET open_jugar = CASE weaving_daily_id" in str(c.args[0])]
        assert len(writes) == 1
        assert str(writes[0].args[0]) == str(q.update_weaving_daily_open_jugar_bulk_query(2))
        assert writes[0].args[1] == {"i0": 2, "v0": 0.0, "i1": 3, "v1": 7.5}
        # No per-row resolver / updater in the Process path any more.
        for c in session.execute.call_args_list:
            assert "WHERE wd.weaving_daily_id = :row_id" not in str(c.args[0])
            assert "WHERE weaving_daily_id = :row_id" not in str(c.args[0])

    def test_idle_loom_close_jugar_carries_open_jugar(self):
        """cuts + close_jugar = 0 -> close_jugar := open_jugar (production 0, chain
        carries forward); a previous carry is re-carried when open shifts; a real
        zero-cut reading (close != 0 and != old open) is left alone."""
        session = MagicMock()
        unit_rows = [MagicMock(weaving_daily_id=i, machine_id=i) for i in (1, 2, 3, 4)]
        oj = [MagicMock(weaving_daily_id=1, stored_value=12.0, open_jugar=12.0, cuts=0, close_jugar=0),  # idle
              MagicMock(weaving_daily_id=2, stored_value=4.0, open_jugar=9.0, cuts=0, close_jugar=4.0),  # re-carry
              MagicMock(weaving_daily_id=3, stored_value=4.0, open_jugar=4.0, cuts=0, close_jugar=7.0),  # real reading
              MagicMock(weaving_daily_id=4, stored_value=4.0, open_jugar=4.0, cuts=3, close_jugar=0)]    # produced
        session.execute.side_effect = self._eb_side_effects(unit_rows, [], oj)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        writes = [c for c in session.execute.call_args_list
                  if "SET close_jugar = CASE weaving_daily_id" in str(c.args[0])]
        assert len(writes) == 1
        assert writes[0].args[1] == {"i0": 1, "v0": 12.0, "i1": 2, "v1": 9.0}

    def test_no_bulk_writes_when_nothing_resolved_or_changed(self):
        session = MagicMock()
        unit_rows = [MagicMock(weaving_daily_id=1, machine_id=7)]
        oj = [MagicMock(weaving_daily_id=1, stored_value=3.0, open_jugar=3.0)]
        session.execute.side_effect = self._eb_side_effects(unit_rows, [], oj)
        resp = self._run(session)
        assert resp.status_code == 200, resp.text
        sqls = [str(c.args[0]) for c in session.execute.call_args_list]
        assert not any("CASE machine_id" in s for s in sqls)
        assert not any("CASE weaving_daily_id" in s for s in sqls)

    @pytest.mark.parametrize("body, expected", [({}, 0),
                                                ({"ignore_designation": True}, 1)])
    def test_ignore_designation_threads_to_the_gate(self, body, expected):
        """The escape hatch must actually reach BOTH the candidates query and the
        no_worker WARN (they share one eligibility rule)."""
        session = MagicMock()
        empty = MagicMock(fetchall=MagicMock(return_value=[]))
        session.execute.side_effect = [empty] * 8 + [
            MagicMock(), empty, empty, MagicMock(), MagicMock(rowcount=0),
            MagicMock(fetchone=MagicMock(return_value=None)), MagicMock()]
        resp = self._run(session, **body)
        assert resp.status_code == 200, resp.text
        no_worker_binds = session.execute.call_args_list[2].args[1]
        cand_binds = session.execute.call_args_list[7].args[1]
        assert cand_binds["ignore_designation"] == expected
        assert no_worker_binds["ignore_designation"] == expected
        assert cand_binds["desig_prefix"] == q.WEAVER_DESIG_PREFIX

    def test_block_when_stored_quality_mismatches_map(self):
        """A daily row whose STORED quality is NULL/stale vs the active map blocks
        Process — quality_mismatch carries the mech_code list."""
        session = MagicMock()
        no_unmapped = MagicMock(fetchall=MagicMock(return_value=[]))
        row = MagicMock()
        row._mapping = {"machine_id": 42, "mech_code": "L42", "machine_name": "Loom 42"}
        mismatch = MagicMock(fetchall=MagicMock(return_value=[row]))
        session.execute.side_effect = [no_unmapped, mismatch]
        resp = self._run(session)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["unmapped"] == []
        assert detail["quality_mismatch"] == ["L42"]


class TestProcessSpellIdContract:
    """process accepts spell_id (preferred) via the real shared _resolve_spell:
    wrong-branch spell_id -> 400; both spell_id and spell -> 400 exactly-one."""

    def _post(self, session, body):
        app.dependency_overrides[get_tenant_db] = lambda: session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        try:
            return client.post("/api/weavingProd/process", json=body)
        finally:
            app.dependency_overrides.clear()

    def test_wrong_branch_spell_id_400(self):
        session = MagicMock()
        row = MagicMock(); row.spell_id = 91; row.branch_id = 12  # shift branch != 2
        session.execute.return_value.fetchone.return_value = row
        resp = self._post(session, {
            "co_id": 1, "branch_id": 2, "tran_date": "2026-07-07", "spell_id": 91})
        assert resp.status_code == 400
        assert "does not belong to this branch" in resp.json()["detail"]

    def test_both_spell_id_and_spell_400(self):
        resp = self._post(MagicMock(), {
            "co_id": 1, "branch_id": 2, "tran_date": "2026-07-07",
            "spell_id": 91, "spell": "A1"})
        assert resp.status_code == 400
        assert "exactly one of spell_id or spell" in resp.json()["detail"]

    def test_neither_spell_id_nor_spell_400(self):
        resp = self._post(MagicMock(), {
            "co_id": 1, "branch_id": 2, "tran_date": "2026-07-07"})
        assert resp.status_code == 400
        assert "exactly one of spell_id or spell" in resp.json()["detail"]


# =============================================================================
# Worker (eb_id) resolution — spinning's attendance cascade, rebuilt for weaving
# =============================================================================


def _cand(mc_id, eb_id, daily_atten_id):
    return MagicMock(mc_id=mc_id, eb_id=eb_id, daily_atten_id=daily_atten_id)


class TestResolveOperators:
    def test_empty(self):
        assert _resolve_operators([]) == ({}, [])

    def test_single_candidate_wins(self):
        resolved, ambiguous = _resolve_operators([_cand(7, 100, 5)])
        assert resolved == {7: 100}
        assert ambiguous == []

    def test_lowest_daily_atten_id_wins_and_is_reported(self):
        """Two weavers on one loom: first-marked (lowest daily_atten_id) is
        stamped, and the tie is ALWAYS surfaced — marking order is arbitrary."""
        resolved, ambiguous = _resolve_operators(
            [_cand(7, 200, 9), _cand(7, 100, 4)])
        assert resolved == {7: 100}
        assert ambiguous == [
            {"machine_id": 7, "eb_ids": [100, 200], "stamped_eb_id": 100}]

    def test_duplicate_marks_of_same_eb_not_ambiguous(self):
        resolved, ambiguous = _resolve_operators(
            [_cand(7, 100, 4), _cand(7, 100, 9)])
        assert resolved == {7: 100}
        assert ambiguous == []


class TestOperatorQueryGuards:
    def test_candidates_query_filters_spell_and_designation(self):
        """Regression: the old stamp matched ANY spell of the day and gated on
        designation_mst.on_machine = 'Yes' (a value sls does not have)."""
        sql = str(q.get_weaving_operator_candidates_query())
        assert "da.spell_id = :spell_id" in sql
        assert "LIKE :desig_prefix" in sql
        assert ":ignore_designation = 1" in sql
        assert "da.branch_id = :branch_id" in sql  # header branch, never dea's
        assert "on_machine" not in sql
        assert q.WEAVER_DESIG_PREFIX == "weaver%"

    def test_candidates_queries_exclude_null_eb(self):
        """da.eb_id is nullable — a NULL-eb mark is not a worker, and letting one
        through would crash int(r.eb_id). Same guard in spinning (shared pattern)."""
        from src.juteProduction.spinning_query import get_operator_candidates_query
        for builder in [q.get_weaving_operator_candidates_query,
                        get_operator_candidates_query]:
            assert "da.eb_id IS NOT NULL" in str(builder()), builder.__name__

    def test_no_worker_warn_is_branch_scoped_like_the_stamp(self):
        sql = str(q.get_weaving_process_no_worker_query())
        assert "da.branch_id = :branch_id" in sql

    def test_no_worker_warn_shares_the_stamp_eligibility(self):
        """The WARN must not disagree with what gets stamped: same spell join,
        same designation gate."""
        sql = str(q.get_weaving_process_no_worker_query())
        # 2026-09-16: attendance resolved date-first; the unit spell bind is the
        # same one the candidates query filters on.
        assert "da.spell_id = :spell_id" in sql and "da.attendance_date = :tran_date" in sql
        assert "LIKE :desig_prefix" in sql
        assert ":ignore_designation = 1" in sql

    def test_daily_eb_writes_are_unit_and_branch_scoped(self):
        for builder in [q.clear_weaving_daily_eb_query, q.stamp_weaving_daily_eb_query]:
            sql = str(builder())
            assert "jute_prod_weaving_daily" in sql, builder.__name__
            assert ":branch_id IS NULL OR" in sql, builder.__name__
            assert "spell_id = :spell_id" in sql, builder.__name__
        assert "machine_id = :mc_id" in str(q.stamp_weaving_daily_eb_query())

    def test_bulk_builders_shape(self):
        """TKT-000134 set-based twins: unit + branch scoped like the per-row ones,
        one CASE arm per bind pair, and binds line up with bulk_case_binds."""
        sql = str(q.stamp_weaving_daily_eb_bulk_query(2))
        assert ":branch_id IS NULL OR" in sql and "spell_id = :spell_id" in sql
        assert "CASE machine_id WHEN :m0 THEN :e0 WHEN :m1 THEN :e1 END" in sql
        assert "machine_id IN (:m0, :m1)" in sql
        assert q.bulk_case_binds("m", "e", [(7, 100), (9, 200)]) == {
            "m0": 7, "e0": 100, "m1": 9, "e1": 200}
        sql = str(q.update_weaving_daily_open_jugar_bulk_query(1))
        assert "CASE weaving_daily_id WHEN :i0 THEN :v0 END" in sql
        assert "weaving_daily_id IN (:i0)" in sql
        assert "updated_by" not in sql
        # 2026-09-16: the unit SELECT is a bounded WINDOW twin of the per-row probe
        # (same chain order + gap cap, one scan instead of two probes per row).
        unit_sql = str(q.get_weaving_unit_open_jugar_query())
        assert "WINDOW chain AS" in unit_sql and "LAG(w.close_jugar) OVER chain" in unit_sql
        assert q._open_jugar_probe_sql() in str(q.resolve_weaving_open_jugar_for_row_query())
        assert "w.open_jugar AS stored_value" in unit_sql
        assert " AS stored," not in unit_sql  # STORED is a MySQL 8 reserved word


class TestProductionGatedLoomLists:
    """TKT-000111: a unit holds one daily row per loom in the shed (sls co 106:
    485 rows, ~200 with production), so every per-loom BLOCK/WARN list must be gated
    on actual production or it names hundreds of idle looms and buries the real ones."""

    GATED_BUILDERS = [
        "get_weaving_unmapped_produced_looms_query",
        "get_weaving_process_quality_mismatch_query",
        "get_weaving_process_no_worker_query",
        "get_weaving_process_no_standard_query",
        "get_weaving_process_no_picks_query",
    ]

    def test_every_per_loom_list_is_production_gated(self):
        for name in self.GATED_BUILDERS:
            sql = str(getattr(q, name)())
            assert q.WEAVING_PRODUCED_PREDICATE in sql, name

    def test_predicate_covers_the_day_slice_production_inputs(self):
        """cuts / close_jugar / less_production are the slice's production inputs;
        open_jugar is a resolved carry-forward, so it must NOT make a loom 'produced'
        (an idle loom after a produced spell still carries an opening)."""
        pred = q.WEAVING_PRODUCED_PREDICATE
        for col in ("wd.cuts", "wd.close_jugar", "wd.less_production"):
            assert col in pred, col
        assert "open_jugar" not in pred

    def test_gate_does_not_leak_into_the_unit_row_scan(self):
        """The open_jugar / eb-stamp scan must still see EVERY row of the unit —
        gating it would strand idle looms with a stale carry-forward or eb stamp."""
        assert q.WEAVING_PRODUCED_PREDICATE not in str(q.get_weaving_unit_daily_ids_query())


RUN_DB = os.environ.get("WEAVING_PARITY_DB")  # e.g. "dev3"; unset -> skip


@pytest.mark.skipif(not RUN_DB, reason="set WEAVING_PARITY_DB to run DB parity")
def test_log_matches_slice_after_process():
    """After Process, each frozen log row equals the live day-slice row for the
    same (machine, quality) on production_yds/efficiency/working_hours (<=0.001)."""
    import pymysql
    conn = pymysql.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                           password=os.environ["DB_PASS"], database=RUN_DB, port=3306)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    co = int(os.environ["PARITY_CO"]); d = os.environ["PARITY_DATE"]
    spell_id = int(os.environ["PARITY_SPELL_ID"])
    cur.execute("""SELECT machine_id, weaving_quality_id, ROUND(production_yds,3) p,
                          ROUND(efficiency,2) e, ROUND(working_hours,3) w
                   FROM jute_prod_weaving_log
                   WHERE co_id=%s AND tran_date=%s AND spell_id=%s AND active=1""",
                (co, d, spell_id))
    log_rows = {(r["machine_id"], r["weaving_quality_id"]): r for r in cur.fetchall()}
    assert log_rows, "no frozen rows — process the unit first"
    conn.close()
