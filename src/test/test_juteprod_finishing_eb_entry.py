"""Tests for hemming / herackle machine-and/or-worker entry + the attendance eb sync.

Covers the MACHINE_OR_LABOUR_PROCESSES additions to
src/juteProduction/finishing_entry.py (prefix /api/finishingProd):

  * entry_save  — machine and/or eb (neither = 400), server-side eb branch check, the
    NULL-safe (machine_id, eb_id) pair key (machine-only and machine+eb coexist), and
    the entry_id edit path with its collision guard.
  * entry_sync_eb — attendance -> eb_id back-fill: single candidate stamps, an
    operator+helper pair resolves to the operator, two operators are reported ambiguous
    (never guessed), a duplicate identity is a reported collision, and the fill-only
    guard lives in SQL on BOTH the slice read and the stamp write.

Portal persona, DB + auth mocked (sibling style: app.dependency_overrides keyed by the
symbols imported into the router module). db.execute is routed by inspecting the bound
SQL, so each handler step gets what it reads.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction.finishing_entry import (
    _resolve_finishing_operators,
    get_current_user_with_refresh,
    get_tenant_db,
)

client = TestClient(app)


def _row(**kw):
    r = MagicMock()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _one(row):
    ex = MagicMock()
    ex.fetchone.return_value = row
    return ex


def _all(rows):
    ex = MagicMock()
    ex.fetchall.return_value = rows
    return ex


def _sql_calls(session, needle):
    """Every (sql, params) pair whose bound SQL contains needle (lower-cased)."""
    out = []
    for call in session.execute.call_args_list:
        sql = str(call.args[0]).lower()
        if needle in sql:
            out.append((sql, call.args[1] if len(call.args) > 1 else {}))
    return out


# =============================================================================
# entry_save — hemming / herackle (machine and/or eb)
# =============================================================================


class TestFlexSave:
    """POST /api/finishingProd/entry_save for process=hemming."""

    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _route(
        self,
        quality_type=2,
        worker_branch=2,
        pair_hit=None,
        by_id=None,
        clash=None,
        sibling=None,
        insert_id=101,
    ):
        """pair_hit is a callable(pair binds) -> row or None (a MagicMock row can't be
        used directly here: MagicMock is itself callable)."""

        def _r(query, params=None):
            params = params or {}
            sql = str(query).lower()
            if "from jute_prod_finishing_quality" in sql:
                return _one(None if quality_type is None
                            else _row(quality_type=quality_type))
            if "select o.branch_id" in sql:
                return _one(None if worker_branch is None
                            else _row(branch_id=worker_branch))
            if "from machine_mst m" in sql and "inner join dept_mst" in sql:
                return _one(_row(branch_id=2))
            if "fd.eb_id is not null" in sql:
                return _one(sibling)
            if "machine_id <=>" in sql:
                if params.get("exclude_id") is not None:
                    return _one(clash)
                return _one(pair_hit(params) if pair_hit else None)
            if "from jute_prod_finishing_daily" in sql and "where finishing_daily_id" in sql:
                return _one(by_id)
            ins = MagicMock()
            ins.lastrowid = insert_id
            return ins

        return _r

    def _payload(self, **over):
        p = {
            "co_id": 1,
            "tran_date": "2026-08-20",
            "spell_id": 6,
            "process": "hemming",
            "finishing_quality_id": 5,
            "prod_qty": 250.0,
            "prod_uom": "pcs",
        }
        p.update(over)
        return p

    def _stored(self, **over):
        """A stored row whose identity MATCHES _payload() — the edit path re-checks it."""
        row = {
            "finishing_daily_id": 55,
            "tran_date": "2026-08-20",
            "spell_id": 6,
            "process": "hemming",
            "finishing_quality_id": 5,
            "machine_id": 28,
            "eb_id": None,
        }
        row.update(over)
        return _row(**row)

    def test_neither_machine_nor_eb_returns_400(self):
        self.db.execute.side_effect = self._route()
        r = client.post("/api/finishingProd/entry_save", json=self._payload())
        assert r.status_code == 400
        assert "eb no" in r.json()["detail"].lower()
        self.db.commit.assert_not_called()

    def test_machine_only_save(self):
        self.db.execute.side_effect = self._route()
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, branch_id=2),
        )
        assert r.status_code == 200
        assert r.json()["data"]["finishing_daily_id"] == 101
        ins = _sql_calls(self.db, "insert into jute_prod_finishing_daily")[0][1]
        assert ins["machine_id"] == 28
        assert ins["eb_id"] is None
        assert ins["prod_qty"] == 250.0

    def test_eb_only_save_derives_branch_from_worker(self):
        self.db.execute.side_effect = self._route(worker_branch=7)
        r = client.post(
            "/api/finishingProd/entry_save", json=self._payload(eb_id=42)
        )
        assert r.status_code == 200
        ins = _sql_calls(self.db, "insert into jute_prod_finishing_daily")[0][1]
        assert ins["machine_id"] is None
        assert ins["eb_id"] == 42
        assert ins["branch_id"] == 7  # taken from HRMS, not the (absent) body branch

    def test_machine_and_eb_save(self):
        self.db.execute.side_effect = self._route()
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2),
        )
        assert r.status_code == 200
        ins = _sql_calls(self.db, "insert into jute_prod_finishing_daily")[0][1]
        assert ins["machine_id"] == 28
        assert ins["eb_id"] == 42

    def test_eb_from_another_branch_returns_400(self):
        self.db.execute.side_effect = self._route(worker_branch=99)
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2),
        )
        assert r.status_code == 400
        assert "branch" in r.json()["detail"].lower()
        self.db.commit.assert_not_called()

    def test_unknown_eb_returns_400(self):
        self.db.execute.side_effect = self._route(worker_branch=None)
        r = client.post(
            "/api/finishingProd/entry_save", json=self._payload(eb_id=4242)
        )
        assert r.status_code == 400
        assert "eb no" in r.json()["detail"].lower()

    def test_duplicate_pair_updates_instead_of_inserting(self):
        """Re-saving the SAME (machine, eb, quality) hits the pair key -> UPDATE."""
        self.db.execute.side_effect = self._route(
            pair_hit=lambda p: _row(finishing_daily_id=55, machine_id=28, eb_id=42)
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2, prod_qty=300.0),
        )
        assert r.status_code == 200
        assert r.json()["data"]["finishing_daily_id"] == 55
        assert _sql_calls(self.db, "insert into jute_prod_finishing_daily") == []
        upd = _sql_calls(self.db, "update jute_prod_finishing_daily")[0][1]
        assert upd["id"] == 55
        assert upd["prod_qty"] == 300.0

    def test_machine_only_and_machine_plus_eb_coexist(self):
        """A machine-only row on machine 28 does NOT satisfy the machine+eb key.

        The pair lookup is NULL-safe on BOTH columns, so eb_id=42 misses the eb_id NULL
        row and a second row is inserted rather than the worker being merged in."""
        self.db.execute.side_effect = self._route(
            pair_hit=lambda p: (_row(finishing_daily_id=55) if p["eb_id"] is None
                                else None)
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2),
        )
        assert r.status_code == 200
        assert r.json()["data"]["finishing_daily_id"] == 101  # inserted, not 55
        pair_params = _sql_calls(self.db, "machine_id <=>")[0][1]
        assert pair_params["machine_id"] == 28 and pair_params["eb_id"] == 42

    def test_edit_by_entry_id_updates_that_row(self):
        """Same identity + a NEW eb_id is exactly what the edit path is for."""
        self.db.execute.side_effect = self._route(by_id=self._stored())
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(
                machine_id=28, eb_id=42, branch_id=2, entry_id=55, prod_qty=400.0
            ),
        )
        assert r.status_code == 200
        assert r.json()["data"]["finishing_daily_id"] == 55
        upd = _sql_calls(self.db, "update jute_prod_finishing_daily")[0][1]
        assert upd["eb_id"] == 42 and upd["prod_qty"] == 400.0

    def test_edit_with_changed_tran_date_returns_400(self):
        """UPDATE never SETs tran_date, so a drifted date would silently rewrite the OLD
        day's row and report success."""
        self.db.execute.side_effect = self._route(by_id=self._stored())
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(
                machine_id=28, branch_id=2, entry_id=55, tran_date="2026-08-21"
            ),
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "date" in detail and "2026-08-20" in detail
        assert _sql_calls(self.db, "update jute_prod_finishing_daily") == []
        self.db.commit.assert_not_called()

    def test_edit_with_changed_quality_returns_400(self):
        self.db.execute.side_effect = self._route(by_id=self._stored())
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(
                machine_id=28, branch_id=2, entry_id=55, finishing_quality_id=9
            ),
        )
        assert r.status_code == 400
        assert "quality" in r.json()["detail"]
        assert _sql_calls(self.db, "update jute_prod_finishing_daily") == []
        self.db.commit.assert_not_called()

    def test_edit_with_changed_machine_returns_400(self):
        self.db.execute.side_effect = self._route(by_id=self._stored())
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=29, branch_id=2, entry_id=55),
        )
        assert r.status_code == 400
        assert "machine" in r.json()["detail"]
        self.db.commit.assert_not_called()

    def test_edit_of_eb_only_row_tolerates_null_machine(self):
        """machine stays NULL on both sides — a NULL/None mix must not read as drift."""
        self.db.execute.side_effect = self._route(
            by_id=self._stored(machine_id=None, eb_id=7), worker_branch=2
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(eb_id=42, branch_id=2, entry_id=55, prod_qty=90.0),
        )
        assert r.status_code == 200
        upd = _sql_calls(self.db, "update jute_prod_finishing_daily")[0][1]
        assert upd["eb_id"] == 42 and upd["prod_qty"] == 90.0

    def test_edit_onto_existing_identity_returns_400(self):
        self.db.execute.side_effect = self._route(
            by_id=self._stored(),
            clash=_row(finishing_daily_id=56),
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2, entry_id=55),
        )
        assert r.status_code == 400
        assert "56" in r.json()["detail"]
        assert _sql_calls(self.db, "update jute_prod_finishing_daily") == []
        self.db.commit.assert_not_called()

    def test_edit_unknown_entry_id_returns_404(self):
        self.db.execute.side_effect = self._route(by_id=None)
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, branch_id=2, entry_id=999),
        )
        assert r.status_code == 404

    # --- machine-only insert next to an EB-stamped sibling: warn, never block -------

    def test_machine_only_insert_warns_about_stamped_sibling(self):
        self.db.execute.side_effect = self._route(
            sibling=_row(finishing_daily_id=55, eb_id=7, mech_code="91001")
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, branch_id=2),
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["finishing_daily_id"] == 101  # saved anyway — coexistence is allowed
        assert "91001" in data["warning"] and "#55" in data["warning"]
        assert _sql_calls(self.db, "insert into jute_prod_finishing_daily")

    def test_no_warning_without_a_stamped_sibling(self):
        self.db.execute.side_effect = self._route()
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, branch_id=2),
        )
        assert r.status_code == 200
        assert "warning" not in r.json()["data"]

    def test_no_warning_when_the_save_carries_an_eb(self):
        """An eb-carrying save IS the attributed row — nothing to confuse it with."""
        self.db.execute.side_effect = self._route(
            sibling=_row(finishing_daily_id=55, eb_id=7, mech_code="91001")
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, eb_id=42, branch_id=2),
        )
        assert r.status_code == 200
        assert "warning" not in r.json()["data"]
        assert _sql_calls(self.db, "fd.eb_id is not null") == []

    def test_no_warning_on_update(self):
        self.db.execute.side_effect = self._route(
            pair_hit=lambda p: _row(finishing_daily_id=55),
            sibling=_row(finishing_daily_id=60, eb_id=7, mech_code="91001"),
        )
        r = client.post(
            "/api/finishingProd/entry_save",
            json=self._payload(machine_id=28, branch_id=2),
        )
        assert r.status_code == 200
        assert "warning" not in r.json()["data"]
        assert _sql_calls(self.db, "fd.eb_id is not null") == []


# =============================================================================
# _resolve_finishing_operators (pure)
# =============================================================================


class TestResolveOperators:
    def test_single_candidate(self):
        resolved, amb = _resolve_finishing_operators(
            [_row(mc_id=28, eb_id=1, desig="HEMMING OPERATOR")]
        )
        assert resolved == {28: 1} and amb == []

    def test_operator_preferred_over_helper(self):
        resolved, amb = _resolve_finishing_operators([
            _row(mc_id=28, eb_id=1, desig="HEMMING OPERATOR"),
            _row(mc_id=28, eb_id=2, desig="HEMMING HELPER"),
        ])
        assert resolved == {28: 1} and amb == []

    def test_two_operators_are_ambiguous(self):
        resolved, amb = _resolve_finishing_operators([
            _row(mc_id=28, eb_id=1, desig="HEMMING OPERATOR"),
            _row(mc_id=28, eb_id=2, desig="HEMMING OPERATOR"),
        ])
        assert resolved == {}
        assert amb == [{"machine_id": 28, "eb_ids": [1, 2]}]

    def test_null_designation_is_tolerated(self):
        resolved, amb = _resolve_finishing_operators(
            [_row(mc_id=28, eb_id=1, desig=None)]
        )
        assert resolved == {28: 1} and amb == []


# =============================================================================
# entry_sync_eb
# =============================================================================


class TestEntrySyncEb:
    """POST /api/finishingProd/entry_sync_eb"""

    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _route(self, slice_rows, cand_rows, clash=None):
        def _r(query, params=None):
            sql = str(query).lower()
            if "from spell_mst sp" in sql:
                return _one(_row(spell_id=6, branch_id=2))
            if "from jute_prod_finishing_daily fd" in sql:
                return _all(slice_rows)
            if "daily_ebmc_attendance" in sql:
                return _all(cand_rows)
            if "machine_id <=>" in sql:
                return _one(clash)
            return MagicMock()

        return _r

    def _body(self, **over):
        b = {
            "co_id": 1,
            "branch_id": 2,
            "tran_date": "2026-08-20",
            "spell_id": 6,
            "process": "hemming",
        }
        b.update(over)
        return b

    def test_wrong_process_returns_400(self):
        self.db.execute.side_effect = self._route([], [])
        r = client.post(
            "/api/finishingProd/entry_sync_eb", json=self._body(process="calendering")
        )
        assert r.status_code == 400
        assert "hemming" in r.json()["detail"].lower()
        self.db.commit.assert_not_called()

    def test_single_candidate_stamps(self):
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=10, machine_id=28, finishing_quality_id=5,
                  mech_code="91001")],
            [_row(mc_id=28, eb_id=7, desig="HEMMING OPERATOR")],
        )
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["stamped"] == 1
        assert data["no_worker"] == [] and data["ambiguous"] == []
        stamp = _sql_calls(self.db, "set eb_id = :eb_id")[0][1]
        assert stamp == {"id": 10, "eb_id": 7, "updated_by": 1}
        self.db.commit.assert_called_once()

    def test_operator_preferred_over_helper(self):
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=11, machine_id=75, finishing_quality_id=5,
                  mech_code="91002")],
            [_row(mc_id=75, eb_id=8, desig="HEMMING HELPER"),
             _row(mc_id=75, eb_id=9, desig="HEMMING OPERATOR")],
        )
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        assert r.json()["data"]["stamped"] == 1
        assert _sql_calls(self.db, "set eb_id = :eb_id")[0][1]["eb_id"] == 9

    def test_two_operators_skipped_as_ambiguous(self):
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=12, machine_id=76, finishing_quality_id=5,
                  mech_code="91003")],
            [_row(mc_id=76, eb_id=1, desig="HEMMING OPERATOR"),
             _row(mc_id=76, eb_id=2, desig="HEMMING OPERATOR")],
        )
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["stamped"] == 0
        assert data["no_worker"] == []
        assert data["ambiguous"] == [
            {"machine_id": 76, "eb_ids": [1, 2], "mech_code": "91003"}
        ]
        assert _sql_calls(self.db, "set eb_id = :eb_id") == []

    def test_no_candidate_reported_once_per_machine(self):
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=13, machine_id=77, finishing_quality_id=5,
                  mech_code="91004"),
             _row(finishing_daily_id=14, machine_id=77, finishing_quality_id=6,
                  mech_code="91004")],
            [],
        )
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["stamped"] == 0
        assert data["no_worker"] == [{"machine_id": 77, "mech_code": "91004"}]

    def test_collision_is_skipped_and_reported(self):
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=15, machine_id=28, finishing_quality_id=5,
                  mech_code="91001")],
            [_row(mc_id=28, eb_id=7, desig="HEMMING OPERATOR")],
            clash=_row(finishing_daily_id=99),
        )
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["stamped"] == 0
        assert data["collisions"] == [{
            "machine_id": 28, "mech_code": "91001", "eb_id": 7,
            "finishing_daily_id": 15, "conflicting_id": 99,
        }]
        assert _sql_calls(self.db, "set eb_id = :eb_id") == []

    def test_empty_slice_short_circuits(self):
        self.db.execute.side_effect = self._route([], [])
        r = client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        assert r.status_code == 200
        assert r.json()["data"] == {
            "stamped": 0, "no_worker": [], "ambiguous": [], "collisions": [],
        }
        assert _sql_calls(self.db, "daily_ebmc_attendance") == []

    def test_fill_only_guard_is_in_sql(self):
        """The slice read AND the stamp write both refuse rows that already carry an eb,
        so a re-sync can never overwrite a manually keyed worker."""
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=10, machine_id=28, finishing_quality_id=5,
                  mech_code="91001")],
            [_row(mc_id=28, eb_id=7, desig="HEMMING OPERATOR")],
        )
        client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        slice_sql = _sql_calls(self.db, "from jute_prod_finishing_daily fd")[0][0]
        assert "fd.eb_id is null" in slice_sql
        stamp_sql = _sql_calls(self.db, "set eb_id = :eb_id")[0][0]
        assert "eb_id is null" in stamp_sql

    def test_candidates_query_never_filters_line_level_columns(self):
        """dea.branch_id / dea.attendace_date / dea.designation_id are NULL or sparse on
        mobile-marked attendance — the gate must read the HEADER instead."""
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=10, machine_id=28, finishing_quality_id=5,
                  mech_code="91001")],
            [],
        )
        client.post("/api/finishingProd/entry_sync_eb", json=self._body())
        sql, params = _sql_calls(self.db, "daily_ebmc_attendance")[0]
        for banned in ("dea.branch_id", "dea.attendace_date", "dea.designation_id"):
            assert banned not in sql
        assert "da.branch_id = :branch_id" in sql
        assert "da.worked_designation_id" in sql
        assert params["desig_prefix"] == "hemming%"

    def test_herackle_uses_the_heracle_designation_prefix(self):
        """The process token has a k, the shop-floor designation does not."""
        self.db.execute.side_effect = self._route(
            [_row(finishing_daily_id=10, machine_id=97, finishing_quality_id=5,
                  mech_code="11122")],
            [],
        )
        client.post(
            "/api/finishingProd/entry_sync_eb", json=self._body(process="herackle")
        )
        params = _sql_calls(self.db, "daily_ebmc_attendance")[0][1]
        assert params["desig_prefix"] == "heracle%"
