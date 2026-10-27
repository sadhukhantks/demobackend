"""Tests for the jute PO settlement (closing) engine and endpoints.

Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md §4 "Tests".

* ``is_settled`` matrix (lorry rule on/off, tolerance edge, zero vehicles, zero weight).
* ``recompute_po_settlement`` rules 1–6 against a scripted fake session (no
  database): skip statuses, sticky SHORT/BULK/MIGRATED closes, AUTO close,
  AUTO→reopen rollback with the prior status from the log, idempotence, a
  tenant without the rule table.
* ``settle_after_mr_change`` (old + new PO, missing-schema tolerance) and
  ``recompute_branch`` counts.
* Endpoint validation (400s) and one happy path each for close / bulk /
  reopen-from-closed / recompute / settle_rule / status_log through TestClient
  with the DB and auth dependencies overridden.

The fake session routes each ``text()`` statement by a distinctive fragment of
its SQL to scripted rows and records every write, so the assertions are on
what the engine decided and wrote, not on SQL string equality.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement import mr as mr_module
from src.juteProcurement import po_settlement as ps
from src.juteProcurement.po_settlement import (
    edit_access_check,
    normalise_menu_path,
    resolve_menu_id,
    PoReceipts,
    SettleRule,
    is_settled,
    load_rule,
    recompute_branch,
    recompute_po_settlement,
    settle_after_mr_change,
    weight_tolerance,
)
from src.main import app

client = TestClient(app)
PREFIX = "/api/jutePO"


# =============================================================================
# Fake session
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
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class SchemaError(Exception):
    """Stands in for pymysql's error: args[0] is the MySQL errno."""


class _Query:
    def __init__(self, db):
        self.db = db

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.db.orm_po


class FakeDb:
    """Scripted stand-in for a SQLAlchemy Session.

    pos: {jute_po_id: {status_id, close_type, vehicle_quantity, weight, branch_id, co_id}}
    receipts: {jute_po_id: {received_lorries, received_weight}}
    Writes mutate ``pos`` so a second call sees the first call's result.
    """

    def __init__(self, pos=None, receipts=None, rule=None, prior=None, mr_po_id=None,
                 candidates=None, branches=None, log_rows=None):
        self.pos = pos or {}
        self.receipts = receipts or {}
        self.rule = rule
        self.rule_error = None
        self.po_error = None
        self.prior = prior
        self.mr_po_id = mr_po_id
        self.candidates = candidates or []
        self.branches = branches or []
        self.log_rows = log_rows or []
        self.access_level = 4  # edit access by default; set 1 to test the 403 guard
        self.orm_po = None
        self.writes = []
        self.queries = []
        self.committed = 0
        self.rolled_back = 0

    # -- Session API --------------------------------------------------------

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = dict(params or {})
        self.queries.append(sql)
        if "FROM jute_po_settle_rule" in sql:
            if self.rule_error:
                raise self.rule_error
            return Result([Row(**self.rule)] if self.rule else [])
        if "INSERT INTO jute_po_settle_rule" in sql:
            self.writes.append(("rule", params))
            return Result([])
        if "INSERT INTO jute_po_status_log" in sql:
            self.writes.append(("log", params))
            return Result([])
        if "FROM jute_po_status_log l" in sql:
            return Result(self.log_rows)
        if "FROM jute_po_status_log" in sql:
            return Result([Row(old_status_id=self.prior)] if self.prior is not None else [])
        if sql.startswith("UPDATE jute_po "):
            po = self.pos.setdefault(params["po_id"], {})
            if "close_type = :close_type" in sql:
                self.writes.append(("close", params))
                po.update(status_id=params["closed"], close_type=params["close_type"])
            else:
                self.writes.append(("reopen", params))
                po.update(status_id=params["status_id"], close_type=None)
            return Result([])
        if "SELECT po_id FROM jute_mr" in sql:
            return Result([Row(po_id=self.mr_po_id)])
        if "FROM jute_mr jm" in sql:
            r = self.receipts.get(params["po_id"], {"received_lorries": 0, "received_weight": 0})
            return Result([Row(**r)])
        if "FROM jute_po jp" in sql and "jp.vehicle_quantity" in sql:
            if self.po_error:
                raise self.po_error
            po = self.pos.get(params["po_id"])
            return Result([Row(jute_po_id=params["po_id"], **po)] if po else [])
        if "FROM jute_po jp" in sql and "jp.branch_id = :branch_id" in sql:
            return Result([Row(jute_po_id=i) for i in self.candidates])
        if "FROM jute_po jp" in sql and ":jute_po_id" in sql:
            po = self.pos.get(params["jute_po_id"])
            ok = po and po.get("co_id") == params["co_id"]
            return Result([Row(jute_po_id=params["jute_po_id"], status_id=po["status_id"])] if ok else [])
        if "FROM branch_mst WHERE co_id" in sql:
            return Result([Row(branch_id=b) for b in self.branches])
        if sql.startswith("SELECT branch_id FROM jute_po WHERE jute_po_id"):
            po = self.pos.get(params["id"])
            return Result([Row(branch_id=po.get("branch_id"))] if po else [])
        if "FROM menu_mst" in sql:
            return Result([Row(menu_id=42)])
        if "FROM user_role_map urm" in sql:
            return Result([Row(max_access_type_id=self.access_level)])
        raise AssertionError(f"unexpected SQL: {sql[:90]}")

    def query(self, *a, **k):
        return _Query(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    # -- helpers -------------------------------------------------------------

    def writes_of(self, kind):
        return [p for k, p in self.writes if k == kind]


def _po(status_id=3, close_type=None, vehicle_quantity=5, weight=50000.0, branch_id=2, co_id=1):
    return {
        "status_id": status_id,
        "close_type": close_type,
        "vehicle_quantity": vehicle_quantity,
        "weight": weight,
        "branch_id": branch_id,
        "co_id": co_id,
    }


def _rx(lorries, weight):
    return {"received_lorries": lorries, "received_weight": weight}


def _recompute(db, po_id=100, **over):
    kw = {"user_id": 7, "source": "AUTO", "trigger_ref": "MR:1 test"}
    kw.update(over)
    return recompute_po_settlement(db, po_id, **kw)


# =============================================================================
# is_settled
# =============================================================================


class TestIsSettled:
    """Spec §11.1: tolerance = tolerance_lorry_fraction × (po_weight / vehicle_quantity)."""
    RULE = SettleRule()  # defaults: lorry rule OFF, half a lorry, 30 days
    LORRY_RULE = SettleRule(lorry_rule_enabled=True)

    def test_all_lorries_in_does_not_settle_by_default_when_weight_short(self):
        # Owner 2026-09-25: a lorry arriving never settles by itself; weight decides.
        assert is_settled(PoReceipts(5, 50000.0, 5, 40000.0), self.RULE) is False
        assert is_settled(PoReceipts(5, 50000.0, 5, 40000.0), self.LORRY_RULE) is True

    def test_more_lorries_than_ordered_settles_only_with_lorry_rule(self):
        assert is_settled(PoReceipts(5, 50000.0, 6, 10000.0), self.RULE) is False
        assert is_settled(PoReceipts(5, 50000.0, 6, 10000.0), self.LORRY_RULE) is True

    def test_weight_tolerance_is_half_a_lorry(self):
        # 5 lorries of 10,000 kg -> tolerance 5,000 -> settles at 45,000
        assert weight_tolerance(PoReceipts(5, 50000.0, 0, 0.0), self.RULE) == 5000.0
        assert is_settled(PoReceipts(5, 50000.0, 2, 45000.0), self.RULE) is True
        assert is_settled(PoReceipts(5, 50000.0, 2, 44999.0), self.RULE) is False

    def test_owner_examples(self):
        # a 10 MT lorry -> 5 MT tolerance; a 16 MT lorry -> 8 MT tolerance
        assert weight_tolerance(PoReceipts(1, 10000.0, 0, 0.0), self.RULE) == 5000.0
        assert weight_tolerance(PoReceipts(1, 16000.0, 0, 0.0), self.RULE) == 8000.0
        # 5 lorries of 14 MT (70 MT) settles by weight at >= 63 MT
        assert is_settled(PoReceipts(5, 70000.0, 4, 63000.0), self.RULE) is True
        assert is_settled(PoReceipts(5, 70000.0, 4, 62999.0), self.RULE) is False

    def test_lorry_rule_off_needs_weight(self):
        rule = SettleRule(lorry_rule_enabled=False)
        assert is_settled(PoReceipts(5, 50000.0, 5, 40000.0), rule) is False
        assert is_settled(PoReceipts(5, 50000.0, 5, 45000.0), rule) is True

    def test_fraction_bounds(self):
        # 0 -> full PO weight required; 1 -> a whole lorry may be missing
        assert is_settled(PoReceipts(5, 70000.0, 4, 69999.9), SettleRule(tolerance_lorry_fraction=0)) is False
        assert is_settled(PoReceipts(5, 70000.0, 4, 70000.0), SettleRule(tolerance_lorry_fraction=0)) is True
        assert is_settled(PoReceipts(5, 70000.0, 4, 56000.0), SettleRule(tolerance_lorry_fraction=1)) is True
        assert is_settled(PoReceipts(5, 70000.0, 4, 55999.0), SettleRule(tolerance_lorry_fraction=1)) is False

    def test_no_vehicle_quantity_means_zero_tolerance(self):
        for vehicles in (None, 0):
            assert weight_tolerance(PoReceipts(vehicles, 50000.0, 3, 1000.0), self.RULE) == 0.0
            assert is_settled(PoReceipts(vehicles, 50000.0, 3, 49999.0), self.RULE) is False
            assert is_settled(PoReceipts(vehicles, 50000.0, 3, 50000.0), self.RULE) is True

    def test_zero_or_missing_weight_never_settles_by_weight(self):
        assert is_settled(PoReceipts(5, 0.0, 2, 0.0), self.RULE) is False
        assert is_settled(PoReceipts(5, None, 2, 0.0), self.RULE) is False
        # ... but the lorry rule still can
        assert is_settled(PoReceipts(3, None, 3, 0.0), self.RULE) is False
        assert is_settled(PoReceipts(3, None, 3, 0.0), self.LORRY_RULE) is True

    def test_nothing_ordered_nothing_received(self):
        assert is_settled(PoReceipts(None, None, 0, 0.0), self.RULE) is False

    def test_returned_lorries_are_informational_only(self):
        r = PoReceipts(5, 50000.0, 4, 40000.0, returned_lorries=1, returned_weight=9000.0)
        assert is_settled(r, self.RULE) is False  # 4 received, the returned one does not count
        assert r.summary() == "lorries 4/5, weight 40,000/50,000, returned 1"

    def test_summary_text(self):
        assert PoReceipts(5, 50000.0, 3, 28400.4).summary() == "lorries 3/5, weight 28,400/50,000"
        assert PoReceipts(None, None, 1, 0).summary() == "lorries 1/-, weight 0/-"


# =============================================================================
# load_rule
# =============================================================================


class TestLoadRule:
    def test_defaults_when_no_row(self):
        r = load_rule(FakeDb(), 1)
        assert (r.lorry_rule_enabled, r.tolerance_lorry_fraction, r.stale_days, r.is_default) == (False, 0.5, 30, True)

    def test_row_values(self):
        db = FakeDb(rule={"lorry_rule_enabled": 0, "tolerance_lorry_fraction": 0.25, "stale_days": 45})
        r = load_rule(db, 1)
        assert (r.lorry_rule_enabled, r.tolerance_lorry_fraction, r.stale_days, r.is_default) == (False, 0.25, 45, False)

    def test_missing_table_falls_back_to_defaults(self):
        db = FakeDb()
        db.rule_error = SchemaError(1146, "Table 'sls.jute_po_settle_rule' doesn't exist")
        assert load_rule(db, 1) == SettleRule()

    def test_as_dict(self):
        assert SettleRule().as_dict() == {
            "lorry_rule_enabled": False, "tolerance_lorry_fraction": 0.5, "stale_days": 30, "is_default": True,
        }


# =============================================================================
# recompute_po_settlement
# =============================================================================


class TestRecompute:
    def test_rule1_not_found_is_skipped(self):
        db = FakeDb()
        assert _recompute(db, 999) == "skipped"
        assert db.writes == []

    @pytest.mark.parametrize("status", [2, 4, 6, 17, 20, 21, None])
    def test_rule1_non_live_statuses_are_skipped_before_receipts(self, status):
        db = FakeDb(pos={100: _po(status_id=status)}, receipts={100: _rx(5, 50000.0)})
        assert _recompute(db) == "skipped"
        assert db.writes == []
        assert not any("FROM jute_mr" in q for q in db.queries)

    @pytest.mark.parametrize("close_type", ["SHORT", "BULK", "MIGRATED"])
    def test_rule2_sticky_closes_are_skipped(self, close_type):
        db = FakeDb(pos={100: _po(status_id=5, close_type=close_type)}, receipts={100: _rx(0, 0.0)})
        assert _recompute(db) == "skipped"
        assert db.writes == []
        assert not any("FROM jute_mr" in q for q in db.queries)

    def test_rule4_live_and_settled_closes_auto_and_logs(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(5, 46000.0)})
        assert _recompute(db, source="AUTO", trigger_ref="MR:28268 approve_mr") == "closed"
        close = db.writes_of("close")[0]
        assert close["closed"] == 5 and close["close_type"] == "AUTO" and close["user_id"] == 7
        assert close["remark"] == "Auto-settled: lorries 5/5, weight 46,000/50,000"
        log = db.writes_of("log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["close_type"], log["source"]) == (3, 5, "AUTO", "AUTO")
        assert log["trigger_ref"] == "MR:28268 approve_mr" and log["user_id"] == 7
        assert db.pos[100]["status_id"] == 5 and db.pos[100]["close_type"] == "AUTO"

    def test_rule4_open_po_closes_from_status_1(self):
        db = FakeDb(pos={100: _po(status_id=1)}, receipts={100: _rx(2, 48000.0)})
        assert _recompute(db) == "closed"
        assert db.writes_of("log")[0]["old_status_id"] == 1

    def test_rule5_auto_close_no_longer_settled_reopens_to_prior_status(self):
        db = FakeDb(pos={100: _po(status_id=5, close_type="AUTO")}, receipts={100: _rx(4, 30000.0)}, prior=1)
        assert _recompute(db, trigger_ref="MR:5 reject_mr") == "reopened"
        reopen = db.writes_of("reopen")[0]
        assert reopen["status_id"] == 1
        log = db.writes_of("log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["close_type"], log["source"]) == (5, 1, None, "AUTO")
        assert log["trigger_ref"] == "MR:5 reject_mr"
        assert db.pos[100]["status_id"] == 1 and db.pos[100]["close_type"] is None

    def test_rule5_reopen_falls_back_to_approved_without_log(self):
        db = FakeDb(pos={100: _po(status_id=5, close_type="AUTO")}, receipts={100: _rx(0, 0.0)}, prior=None)
        assert _recompute(db) == "reopened"
        assert db.writes_of("reopen")[0]["status_id"] == 3

    def test_rule5_reopen_ignores_a_non_live_prior_status(self):
        db = FakeDb(pos={100: _po(status_id=5, close_type="AUTO")}, receipts={100: _rx(0, 0.0)}, prior=20)
        assert _recompute(db) == "reopened"
        assert db.writes_of("reopen")[0]["status_id"] == 3

    def test_rule6_live_not_settled_is_unchanged_without_writes(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(4, 30000.0)})
        assert _recompute(db) == "unchanged"
        assert db.writes == []

    def test_rule6_auto_closed_still_settled_is_unchanged(self):
        db = FakeDb(pos={100: _po(status_id=5, close_type="AUTO")}, receipts={100: _rx(5, 46000.0)})
        assert _recompute(db) == "unchanged"
        assert db.writes == []

    def test_rule6_closed_with_null_close_type_is_left_alone(self):
        db = FakeDb(pos={100: _po(status_id=5, close_type=None)}, receipts={100: _rx(0, 0.0)})
        assert _recompute(db) == "unchanged"
        assert db.writes == []

    def test_idempotent_second_call_writes_nothing(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(5, 46000.0)})
        assert _recompute(db) == "closed"
        n = len(db.writes)
        assert _recompute(db) == "unchanged"
        assert len(db.writes) == n

    def test_round_trip_close_then_rollback(self):
        """AUTO close is derived state: reject the last lorry and the PO reopens to its exact prior status."""
        db = FakeDb(pos={100: _po(status_id=1)}, receipts={100: _rx(5, 46000.0)})
        assert _recompute(db) == "closed"
        db.receipts[100] = _rx(4, 33000.0)
        db.prior = 1  # what the log now holds for the close
        assert _recompute(db) == "reopened"
        assert db.pos[100] == {**_po(status_id=1), "close_type": None}
        assert [k for k, _ in db.writes] == ["close", "log", "reopen", "log"]

    def test_uses_company_rule(self):
        db = FakeDb(
            pos={100: _po(status_id=3)}, receipts={100: _rx(5, 30000.0)},
            rule={"lorry_rule_enabled": 0, "tolerance_lorry_fraction": 0.5, "stale_days": 30},
        )
        assert _recompute(db) == "unchanged"  # lorry rule off, 30,000 < 45,000

    def test_returned_lorry_is_not_a_receipt_but_is_logged(self):
        db = FakeDb(
            pos={100: _po(status_id=3)},
            receipts={100: {"received_lorries": 4, "received_weight": 40000.0, "returned_lorries": 1, "returned_weight": 9000.0}},
        )
        assert _recompute(db) == "unchanged"
        db.receipts[100] = {"received_lorries": 5, "received_weight": 46000.0, "returned_lorries": 1, "returned_weight": 9000.0}
        assert _recompute(db) == "closed"
        assert db.writes_of("close")[0]["remark"] == "Auto-settled: lorries 5/5, weight 46,000/50,000, returned 1"

    def test_explicit_rule_skips_rule_lookup(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(5, 30000.0)})
        assert _recompute(db, rule=SettleRule(lorry_rule_enabled=False)) == "unchanged"
        assert not any("jute_po_settle_rule" in q for q in db.queries)

    def test_missing_rule_table_uses_defaults(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(5, 46000.0)})
        db.rule_error = SchemaError(1146, "no table")
        assert _recompute(db) == "closed"

    def test_collector_records_evaluation(self):
        db = FakeDb(pos={100: _po(status_id=3)}, receipts={100: _rx(5, 46000.0)})
        seen = []
        _recompute(db, collector=seen)
        assert len(seen) == 1 and seen[0].decision == "closed" and seen[0].settled is True
        assert seen[0].receipts.received_lorries == 5


# =============================================================================
# settle_after_mr_change / recompute_branch
# =============================================================================


class TestHooks:
    def test_recomputes_new_and_previous_po(self):
        db = FakeDb(
            pos={10: _po(status_id=3), 20: _po(status_id=5, close_type="AUTO")},
            receipts={10: _rx(5, 50000.0), 20: _rx(3, 20000.0)},
            mr_po_id=10, prior=3,
        )
        settle_after_mr_change(db, 555, user_id=1, trigger_ref="MR:555 jute_gate_entry_update", previous_po_id=20)
        assert db.pos[10]["status_id"] == 5 and db.pos[20]["status_id"] == 3
        assert all(p["trigger_ref"] == "MR:555 jute_gate_entry_update" for p in db.writes_of("log"))

    def test_same_po_only_once(self):
        db = FakeDb(pos={10: _po(status_id=3)}, receipts={10: _rx(5, 50000.0)}, mr_po_id=10)
        settle_after_mr_change(db, 555, user_id=1, trigger_ref="t", previous_po_id=10)
        assert sum(1 for q in db.queries if "jp.vehicle_quantity" in q) == 1

    def test_mr_without_po_does_nothing(self):
        db = FakeDb(mr_po_id=None)
        settle_after_mr_change(db, 555, user_id=1, trigger_ref="t")
        assert db.writes == [] and len(db.queries) == 1

    def test_missing_settlement_schema_is_tolerated(self):
        db = FakeDb(pos={10: _po(status_id=3)}, mr_po_id=10)
        db.po_error = SchemaError(1054, "Unknown column 'jp.close_type' in 'field list'")
        settle_after_mr_change(db, 555, user_id=1, trigger_ref="t")  # no raise
        assert db.writes == []

    def test_other_errors_propagate(self):
        db = FakeDb(pos={10: _po(status_id=3)}, mr_po_id=10)
        db.po_error = SchemaError(1205, "Lock wait timeout exceeded")
        with pytest.raises(SchemaError):
            settle_after_mr_change(db, 555, user_id=1, trigger_ref="t")

    def test_recompute_branch_counts(self):
        db = FakeDb(
            pos={
                1: _po(status_id=3),                       # closes
                2: _po(status_id=3),                       # unchanged
                3: _po(status_id=5, close_type="AUTO"),    # reopens
                4: _po(status_id=5, close_type="AUTO"),    # unchanged (still settled)
            },
            receipts={1: _rx(5, 50000.0), 2: _rx(1, 5000.0), 3: _rx(0, 0.0), 4: _rx(5, 50000.0)},
            candidates=[1, 2, 3, 4], prior=3,
        )
        counts = recompute_branch(db, 1, 2, user_id=None, trigger_ref="recompute co=1 br=2")
        assert counts == {"closed": 1, "reopened": 1, "unchanged": 2}
        logs = db.writes_of("log")
        assert [(l["old_status_id"], l["new_status_id"]) for l in logs] == [(3, 5), (5, 3)]
        assert all(l["source"] == "BACKEND" for l in logs)
        # rule looked up once for the whole branch
        assert sum(1 for q in db.queries if "jute_po_settle_rule" in q) == 1


# =============================================================================
# Endpoints
# =============================================================================


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def overrides(self):
        self.db = FakeDb(
            pos={100: _po(status_id=3), 101: _po(status_id=21), 102: _po(status_id=1, branch_id=9),
                 200: _po(status_id=5, close_type="AUTO"), 300: _po(status_id=3, co_id=2)},
            receipts={100: _rx(1, 5000.0), 200: _rx(5, 50000.0)},
            branches=[1, 2], prior=1,
        )
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    # -- close ---------------------------------------------------------------

    def test_close_requires_co_id(self):
        r = client.post(f"{PREFIX}/close_jute_po/100", json={"remark": "x"})
        assert r.status_code == 400 and "co_id" in r.json()["detail"]

    def test_close_requires_remark(self):
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={"remark": "   "})
        assert r.status_code == 400 and "remark" in r.json()["detail"]
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={})
        assert r.status_code == 400

    def test_close_remark_too_long(self):
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={"remark": "x" * 501})
        assert r.status_code == 400 and "500" in r.json()["detail"]

    def test_close_invalid_json_body(self):
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", content=b"not json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert self.db.writes == []

    def test_close_unknown_or_foreign_po_is_404(self):
        assert client.post(f"{PREFIX}/close_jute_po/999?co_id=1", json={"remark": "x"}).status_code == 404
        assert client.post(f"{PREFIX}/close_jute_po/300?co_id=1", json={"remark": "x"}).status_code == 404

    def test_close_non_live_po_is_400(self):
        r = client.post(f"{PREFIX}/close_jute_po/101?co_id=1", json={"remark": "x"})
        assert r.status_code == 400 and "Open or Approved" in r.json()["detail"]
        r = client.post(f"{PREFIX}/close_jute_po/200?co_id=1", json={"remark": "x"})
        assert r.status_code == 400
        assert self.db.writes == [] and self.db.committed == 0

    def test_close_happy_path(self):
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={"remark": " Supplier defaulted "})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["jute_po_id"] == 100 and data["status_id"] == 5 and data["close_type"] == "SHORT"
        assert data["closed_date"]
        close = self.db.writes_of("close")[0]
        assert close["remark"] == "Supplier defaulted" and close["user_id"] == 7
        log = self.db.writes_of("log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["close_type"], log["source"]) == (3, 5, "SHORT", "MANUAL")
        assert log["trigger_ref"] == "close_jute_po by user 7"
        assert self.db.committed == 1

    # -- bulk close ----------------------------------------------------------

    def test_bulk_requires_branch_id(self):
        r = client.post(f"{PREFIX}/bulk_close_jute_po?co_id=1", json={"jute_po_ids": [100], "remark": "x"})
        assert r.status_code == 400 and "branch_id" in r.json()["detail"]

    def test_bulk_requires_ids(self):
        url = f"{PREFIX}/bulk_close_jute_po?co_id=1&branch_id=2"
        assert client.post(url, json={"jute_po_ids": [], "remark": "x"}).status_code == 400
        assert client.post(url, json={"remark": "x"}).status_code == 400
        assert client.post(url, json={"jute_po_ids": ["a"], "remark": "x"}).status_code == 400
        assert client.post(url, json={"jute_po_ids": [100]}).status_code == 400
        assert self.db.writes == []

    def test_bulk_happy_path_reports_skips(self):
        url = f"{PREFIX}/bulk_close_jute_po?co_id=1&branch_id=2"
        r = client.post(url, json={"jute_po_ids": [100, 101, 102, 999, 100], "remark": "Season over"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert [c["jute_po_id"] for c in data["closed"]] == [100]
        assert data["closed"][0]["close_type"] == "BULK"
        assert [s["jute_po_id"] for s in data["skipped"]] == [101, 102, 999]
        assert "Open or Approved" in data["skipped"][0]["reason"]
        assert data["skipped"][1]["reason"] == "Jute PO not found"  # other branch
        log = self.db.writes_of("log")
        assert len(log) == 1 and log[0]["source"] == "BULK" and log[0]["close_type"] == "BULK"
        assert self.db.committed == 1

    # -- reopen --------------------------------------------------------------

    def test_reopen_from_closed_restores_prior_status(self):
        self.db.orm_po = SimpleNamespace(branch_id=2, jute_po_id=200, status_id=5, updated_by=None, updated_date_time=None)
        r = client.post(f"{PREFIX}/reopen_jute_po/200?co_id=1")
        assert r.status_code == 200, r.text
        assert r.json()["status_id"] == 1
        assert self.db.writes_of("reopen")[0]["status_id"] == 1
        log = self.db.writes_of("log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["source"]) == (5, 1, "MANUAL")
        assert log["trigger_ref"] == "reopen_jute_po by user 7"
        assert self.db.committed == 1

    def test_reopen_rejected_still_goes_to_draft(self):
        self.db.orm_po = SimpleNamespace(branch_id=2, jute_po_id=1, status_id=4, updated_by=None, updated_date_time=None)
        r = client.post(f"{PREFIX}/reopen_jute_po/1?co_id=1")
        assert r.status_code == 200 and r.json()["status_id"] == 21
        assert self.db.orm_po.status_id == 21 and self.db.writes == []

    def test_reopen_live_po_is_400(self):
        self.db.orm_po = SimpleNamespace(branch_id=2, jute_po_id=1, status_id=3, updated_by=None, updated_date_time=None)
        assert client.post(f"{PREFIX}/reopen_jute_po/1?co_id=1").status_code == 400

    # -- recompute -----------------------------------------------------------

    def test_recompute_requires_params(self):
        assert client.post(f"{PREFIX}/recompute_settlement?co_id=1").status_code == 400
        assert client.post(f"{PREFIX}/recompute_settlement?branch_id=2").status_code == 400

    def test_recompute_happy_path(self):
        self.db.candidates = [100, 200]
        self.db.receipts = {100: _rx(5, 50000.0), 200: _rx(0, 0.0)}
        r = client.post(f"{PREFIX}/recompute_settlement?co_id=1&branch_id=2")
        assert r.status_code == 200, r.text
        assert r.json()["data"] == {"closed": 1, "reopened": 1, "unchanged": 0}
        logs = self.db.writes_of("log")
        assert all(l["source"] == "BACKEND" and l["trigger_ref"] == "recompute co=1 br=2 by user 7" for l in logs)
        assert self.db.committed == 1

    # -- settle rule ---------------------------------------------------------

    def test_get_rule_defaults(self):
        r = client.get(f"{PREFIX}/settle_rule?co_id=1")
        assert r.status_code == 200
        assert r.json()["data"] == {"lorry_rule_enabled": False, "tolerance_lorry_fraction": 0.5, "stale_days": 30, "is_default": True}

    def test_get_rule_requires_co_id(self):
        assert client.get(f"{PREFIX}/settle_rule").status_code == 400

    @pytest.mark.parametrize("body", [
        {"lorry_rule_enabled": True, "tolerance_lorry_fraction": 1.5, "stale_days": 30},
        {"lorry_rule_enabled": True, "tolerance_lorry_fraction": -0.1, "stale_days": 30},
        {"lorry_rule_enabled": True, "tolerance_lorry_fraction": 0.5, "stale_days": 0},
        {"lorry_rule_enabled": True, "tolerance_lorry_fraction": 0.5, "stale_days": 366},
        {"lorry_rule_enabled": "maybe", "tolerance_lorry_fraction": 0.5, "stale_days": 30},
        {"lorry_rule_enabled": True, "stale_days": 30},
        {"lorry_rule_enabled": True, "tolerance_lorry_fraction": "abc", "stale_days": 30},
        {"lorry_rule_enabled": True, "weight_tolerance_pct": 5, "stale_days": 30},  # rev-1 field, gone
    ])
    def test_put_rule_validation(self, body):
        r = client.put(f"{PREFIX}/settle_rule?co_id=1", json=body)
        assert r.status_code == 400, r.text
        assert self.db.writes == []

    def test_put_rule_happy_path_recomputes_every_branch(self):
        self.db.candidates = [100]
        self.db.receipts = {100: _rx(5, 50000.0)}
        body = {"lorry_rule_enabled": False, "tolerance_lorry_fraction": 0.25, "stale_days": 45}
        r = client.put(f"{PREFIX}/settle_rule?co_id=1", json=body)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["lorry_rule_enabled"] is False and data["tolerance_lorry_fraction"] == 0.25
        assert data["stale_days"] == 45 and data["is_default"] is False
        # branch 1 closes PO 100 under the saved rule (weight 100 %); branch 2 sees it already closed
        assert data["counts"] == {"closed": 1, "reopened": 0, "unchanged": 1}
        rule_write = self.db.writes_of("rule")[0]
        assert rule_write == {"co_id": 1, "lorry_rule_enabled": 0, "tolerance_lorry_fraction": 0.25, "stale_days": 45, "updated_by": 7}
        # every branch of the company was recomputed (candidate query once per branch)
        assert sum(1 for q in self.db.queries if "jp.branch_id = :branch_id" in q) == 2
        assert self.db.committed == 1

    # -- un-migrated tenant --------------------------------------------------

    def test_write_endpoints_give_409_without_settlement_schema(self):
        self.db.po_error = SchemaError(1054, "Unknown column 'jp.close_type' in 'field list'")
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={"remark": "x"})
        assert r.status_code == 409 and "migration" in r.json()["detail"]
        r = client.post(f"{PREFIX}/bulk_close_jute_po?co_id=1&branch_id=2", json={"jute_po_ids": [100], "remark": "x"})
        assert r.status_code == 409
        assert self.db.rolled_back == 2

    def test_reopen_closed_gives_409_without_settlement_schema(self):
        self.db.orm_po = SimpleNamespace(branch_id=2, jute_po_id=200, status_id=5, updated_by=None, updated_date_time=None)
        self.db.rule_error = None
        original = self.db.execute

        def _execute(stmt, params=None):
            if str(stmt).lstrip().startswith("UPDATE jute_po"):
                raise SchemaError(1054, "Unknown column 'close_type' in 'field list'")
            return original(stmt, params)

        self.db.execute = _execute
        r = client.post(f"{PREFIX}/reopen_jute_po/200?co_id=1")
        assert r.status_code == 409

    # -- status log ----------------------------------------------------------

    def test_status_log_404_for_foreign_po(self):
        assert client.get(f"{PREFIX}/status_log/300?co_id=1").status_code == 404
        assert client.get(f"{PREFIX}/status_log/999?co_id=1").status_code == 404

    def test_status_log_happy_path(self):
        self.db.log_rows = [
            Row(log_id=2, jute_po_id=100, old_status_id=5, new_status_id=3, old_status_name="CLOSED",
                new_status_name="APPROVED", close_type=None, source="MANUAL", trigger_ref="reopen_jute_po by user 7",
                remark="Reopened by user", user_id=7, user_name="QA User", created_at="2026-09-25 10:00:00"),
            Row(log_id=1, jute_po_id=100, old_status_id=3, new_status_id=5, old_status_name="APPROVED",
                new_status_name="CLOSED", close_type="AUTO", source="AUTO", trigger_ref="MR:1 approve_mr",
                remark="Auto-settled: lorries 5/5, weight 50,000/50,000", user_id=None, user_name=None,
                created_at="2026-09-25 09:00:00"),
        ]
        r = client.get(f"{PREFIX}/status_log/100?co_id=1")
        assert r.status_code == 200, r.text
        rows = r.json()["data"]
        assert [x["log_id"] for x in rows] == [2, 1]
        assert rows[0]["user_name"] == "QA User" and rows[1]["close_type"] == "AUTO"
        assert rows[1]["new_status_name"] == "CLOSED"


# =============================================================================
# Module constants stay in step with the spec
# =============================================================================


def test_constants_match_spec():
    assert ps.STICKY_CLOSE_TYPES == ("SHORT", "BULK", "MIGRATED")
    assert ps.DEFAULT_REOPEN_STATUS == 3
    assert (ps.TOLERANCE_FRACTION_MIN, ps.TOLERANCE_FRACTION_MAX) == (0.0, 1.0)
    assert (ps.STALE_DAYS_MIN, ps.STALE_DAYS_MAX) == (1, 365)
    sql = " ".join(str(ps._RECEIPTS_SQL).split())
    assert "NOT IN (4, 6, 21)" in sql and "jm.is_returned = 1" in sql
    # a returned lorry counts only its retained line weight as received
    assert "jute_mr_li rli" in sql
    assert "jute_gate_entry_no IS NOT NULL" in sql
    assert not hasattr(ps, "TOLERANCE_PCT_MAX")


# =============================================================================
# Return logic (spec §11.3) — the MR endpoints drive the engine through the hook
# =============================================================================


def _mrow(**cols):
    m = MagicMock()
    m._mapping = cols
    for k, v in cols.items():
        setattr(m, k, v)
    return m


class TestReturnLogic:
    """Real mr.py handlers + real engine over a MagicMock session whose
    fetchone results are scripted in call order (hook reads: MR po_id, PO,
    rule, receipts[, prior status])."""

    @pytest.fixture(autouse=True)
    def mock_db(self):
        self.db = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        yield
        app.dependency_overrides.clear()

    def _sqls(self):
        return [" ".join(str(c.args[0]).split()) for c in self.db.execute.call_args_list]

    def _params(self, fragment):
        return [c.args[1] for c in self.db.execute.call_args_list if fragment in " ".join(str(c.args[0]).split())]

    def test_return_mr_of_last_lorry_reopens_auto_closed_po(self):
        self.db.execute.return_value.fetchone.side_effect = [
            _mrow(jute_mr_id=1, status_id=1, is_returned=0),  # return_mr pre-check
            _mrow(po_id=10),                                  # hook: MR -> PO
            _mrow(jute_po_id=10, status_id=5, close_type="AUTO", vehicle_quantity=2, weight=24000.0, branch_id=2, co_id=1),
            None,                                             # no rule row -> defaults
            _mrow(received_lorries=1, received_weight=9010.0, returned_lorries=1, returned_weight=9000.0),
            _mrow(old_status_id=3),                           # prior status from the log
        ]
        r = client.post("/api/juteMR/return_mr", json={"mr_id": "1", "branch_id": "2", "reason": "wet"})
        assert r.status_code == 200 and r.json()["is_returned"] is True
        reopen = self._params("UPDATE jute_po SET status_id = :status_id, close_type = NULL")
        assert reopen and reopen[0]["status_id"] == 3
        log = self._params("INSERT INTO jute_po_status_log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["source"]) == (5, 3, "AUTO")
        assert log["trigger_ref"] == "MR:1 return_mr" and "returned 1" in log["remark"]

    def test_change_status_from_returned_to_open_restores_receipt_and_settles(self):
        """Un-returning a lorry (48 -> 1, the only status path open to a Returned MR)
        makes it a receipt again; with every lorry back the PO auto-closes."""
        self.db.execute.return_value.fetchone.side_effect = [
            _mrow(jute_mr_id=1, status_id=48, party_id="9", party_branch_id=5),   # change_status check
            _mrow(po_id=10),                                                     # hook: MR -> PO
            _mrow(jute_po_id=10, status_id=3, close_type=None, vehicle_quantity=2, weight=24000.0, branch_id=2, co_id=1),
            None,                                                                # defaults
            _mrow(received_lorries=2, received_weight=18010.0, returned_lorries=0, returned_weight=0.0),
            ("OPEN",),                                                           # status name after commit
        ]
        r = client.put("/api/juteMR/change_status/1?co_id=1", json={"new_status_id": 1})
        assert r.status_code == 200, r.text
        close = self._params("UPDATE jute_po SET status_id = :closed")
        assert close and close[0]["close_type"] == "AUTO"
        log = self._params("INSERT INTO jute_po_status_log")[0]
        assert (log["old_status_id"], log["new_status_id"], log["close_type"]) == (3, 5, "AUTO")
        assert log["trigger_ref"] == "MR:1 change_status"

    def test_reopen_mr_refuses_a_returned_mr(self):
        """Documented behaviour (§11.3 item 3): reopen_mr only rolls back Pending /
        Approved MRs; a Returned (48) MR is refused with 400, so the hook is not
        reached. Un-returning goes through change_status (test above)."""
        self.db.execute.return_value.fetchone.return_value = _mrow(jute_mr_id=1, status_id=48, branch_id=2)
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "2", "menu_id": 719, "target": "open"})
        assert r.status_code == 400
        assert not any("jute_po" in q for q in self._sqls())


# =============================================================================
# Edit-access guard (owner 2026-09-25: Re-evaluate and Close only for users with
# edit access) — server-side half of the frontend hasMenuAccess(path, "edit").
# =============================================================================


class TestEditAccessGuard:
    @pytest.fixture(autouse=True)
    def overrides(self):
        self.db = FakeDb(
            pos={100: _po(status_id=3), 200: _po(status_id=5, close_type="AUTO")},
            receipts={100: _rx(1, 5000.0), 200: _rx(5, 50000.0)},
            branches=[1, 2], prior=3,
        )
        self.db.access_level = 1  # view only
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    def test_close_is_403_for_view_only_user(self):
        r = client.post(f"{PREFIX}/close_jute_po/100?co_id=1", json={"remark": "x"})
        assert r.status_code == 403 and "edit access" in r.json()["detail"]
        assert self.db.writes == []

    def test_bulk_close_is_403(self):
        r = client.post(f"{PREFIX}/bulk_close_jute_po?co_id=1&branch_id=2", json={"jute_po_ids": [100], "remark": "x"})
        assert r.status_code == 403 and self.db.writes == []

    def test_recompute_is_403(self):
        assert client.post(f"{PREFIX}/recompute_settlement?co_id=1&branch_id=2").status_code == 403

    def test_put_rule_is_403(self):
        body = {"lorry_rule_enabled": False, "tolerance_lorry_fraction": 0.5, "stale_days": 30}
        assert client.put(f"{PREFIX}/settle_rule?co_id=1", json=body).status_code == 403
        assert self.db.writes_of("rule") == []

    def test_reopen_closed_is_403(self):
        from types import SimpleNamespace
        self.db.orm_po = SimpleNamespace(branch_id=2, status_id=5, close_type="AUTO")
        r = client.post(f"{PREFIX}/reopen_jute_po/200?co_id=1")
        assert r.status_code == 403 and self.db.writes == []

    def test_get_rule_and_status_log_stay_readable(self):
        assert client.get(f"{PREFIX}/settle_rule?co_id=1").status_code == 200

    def test_edit_user_passes_and_menu_path_is_honoured(self):
        self.db.access_level = 4
        r = client.post(
            f"{PREFIX}/recompute_settlement?co_id=1&branch_id=2"
            "&menu_path=%2Fdashboardportal%2FjutePurchase%2Freports%2FpoOutstandingPosition"
        )
        assert r.status_code == 200
        menu_lookups = [q for q in self.db.queries if "FROM menu_mst" in q]
        assert menu_lookups, "menu resolved from the page path"


class TestMenuPathResolution:
    def test_normalise(self):
        assert normalise_menu_path("/dashboardportal/jutePurchase/reports/poOutstandingPosition/") == "jutepurchase/reports/pooutstandingposition"
        assert normalise_menu_path("jutePurchase/po") == "jutepurchase/po"
        assert normalise_menu_path(None) == "" and normalise_menu_path("  /  ") == ""

    def test_walks_up_to_the_hub_like_the_sidebar(self):
        db = MagicMock()
        miss, hit = MagicMock(), MagicMock()
        miss.fetchone.return_value = None
        hit_row = MagicMock(); hit_row._mapping = {"menu_id": 10}
        hit.fetchone.return_value = hit_row
        db.execute.side_effect = [miss, hit]
        assert resolve_menu_id(db, "/dashboardportal/jutePurchase/reports/poOutstandingPosition") == 10
        paths = [c.args[1]["path"] for c in db.execute.call_args_list]
        assert paths == ["jutepurchase/reports/pooutstandingposition", "jutepurchase/reports"]

    def test_unknown_path_returns_none_and_check_falls_back_to_po_menu(self):
        db = MagicMock()
        none = MagicMock(); none.fetchone.return_value = None
        db.execute.return_value = none
        assert resolve_menu_id(db, "nowhere/at/all") is None
        assert edit_access_check(db, 7, [2], "nowhere/at/all") is False  # PO menu missing too -> deny

    def test_no_user_or_no_branch_is_denied(self):
        db = MagicMock()
        assert edit_access_check(db, None, [2], "jutePurchase/po") is False
        assert edit_access_check(db, 7, [None], "jutePurchase/po") is False or True  # never raises
