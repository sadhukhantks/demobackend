"""TKT-000119 — Loom->Quality map bulk tools.

Covers src/juteProduction/weaving_quality_map_bulk.py:
  * plan_carry_forward (pure): carry order, existing rows untouched, production
    quality wins, NULL production re-stamp, locked cells skipped but carried through,
    pointer ownership (last_fill).
  * latest_per_loom / validate_range / collect_groups helpers.
  * POST /api/weavingProd/quality_map_carry_forward — dry run writes nothing; real run
    inserts, re-stamps, moves the pointer, flags reprocess, commits; 400s.
  * GET /api/weavingProd/quality_map_groups — both sources, missing-table tolerance.

DB + auth mocked; the endpoint's DB helpers are patched (no execute-order coupling).
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from src.main import app
from src.juteProduction import weaving_quality_map_bulk as bulk
from src.juteProduction.weaving_quality_map_bulk import (
    collect_groups,
    get_current_user_with_refresh,
    get_tenant_db,
    latest_per_loom,
    plan_carry_forward,
    validate_range,
)

client = TestClient(app)

D1, D2 = date(2026, 10, 7), date(2026, 10, 8)
A, B, C = 102, 105, 108  # spell ids in start-time order


def _cells(*days):
    return [(d, s) for d in days for s in (A, B, C)]


# =============================================================================
# plan_carry_forward
# =============================================================================


class TestPlanCarryForward:
    def test_seed_carries_into_every_unmapped_target_cell(self):
        cells = _cells(D1)
        fills, summary, last_fill = plan_carry_forward(
            cells, set(cells), [1, 2], {1: 10, 2: 20}, {}, {}, set()
        )
        assert fills == {c: {1: 10, 2: 20} for c in cells}
        assert [s["to_fill"] for s in summary] == [2, 2, 2]
        assert last_fill == {1: (D1, C), 2: (D1, C)}

    def test_existing_rows_are_never_refilled_and_counted(self):
        cells = _cells(D1)
        existing = {(D1, A): {1: 11}}
        fills, summary, _ = plan_carry_forward(
            cells, set(cells), [1, 2], {1: 10, 2: 20}, existing, {}, set()
        )
        assert 1 not in fills[(D1, A)]
        assert fills[(D1, A)] == {2: 20}
        assert summary[0]["already_mapped"] == 1 and summary[0]["to_fill"] == 1

    def test_saved_change_mid_range_propagates_forward(self):
        """Loom 1 remapped to 99 on D1/B: D1/C and all of D2 carry 99, not the seed."""
        cells = _cells(D1, D2)
        existing = {(D1, B): {1: 99}}
        fills, _, _ = plan_carry_forward(cells, set(cells), [1], {1: 10}, existing, {}, set())
        assert fills[(D1, A)] == {1: 10}
        assert (D1, B) not in fills
        assert fills[(D1, C)] == {1: 99}
        assert all(fills[(D2, s)] == {1: 99} for s in (A, B, C))

    def test_non_target_saved_spell_still_moves_the_carry(self):
        """Only A spells are targets; a saved D1/C row (not a target) feeds D2/A."""
        cells = _cells(D1, D2)
        targets = {(D1, A), (D2, A)}
        existing = {(D1, C): {1: 55}}
        fills, summary, _ = plan_carry_forward(cells, targets, [1], {1: 10}, existing, {}, set())
        assert set(fills) == {(D1, A), (D2, A)}
        assert fills[(D1, A)] == {1: 10}
        assert fills[(D2, A)] == {1: 55}
        assert len(summary) == 2

    def test_loom_without_previous_is_left_unmapped(self):
        cells = _cells(D1)
        fills, summary, _ = plan_carry_forward(cells, set(cells), [1, 2], {1: 10}, {}, {}, set())
        assert all(2 not in v for v in fills.values())
        assert [s["no_previous"] for s in summary] == [1, 1, 1]

    def test_production_quality_wins_over_carry_and_then_carries(self):
        cells = _cells(D1)
        production = {(D1, A): {1: 77}}
        fills, summary, _ = plan_carry_forward(
            cells, set(cells), [1], {1: 10}, {}, production, set()
        )
        assert fills[(D1, A)] == {1: 77}
        assert fills[(D1, B)] == {1: 77}
        assert summary[0]["from_production"] == 1 and summary[0]["restamp"] == 0

    def test_null_quality_production_is_counted_for_restamp(self):
        cells = _cells(D1)
        production = {(D1, A): {1: None}}
        fills, summary, _ = plan_carry_forward(
            cells, set(cells), [1], {1: 10}, {}, production, set()
        )
        assert fills[(D1, A)] == {1: 10}
        assert summary[0]["restamp"] == 1 and summary[0]["from_production"] == 0

    def test_locked_cell_is_skipped_but_carry_passes_through(self):
        cells = _cells(D1)
        fills, summary, _ = plan_carry_forward(
            cells, set(cells), [1], {1: 10}, {}, {(D1, B): {1: 33}}, {(D1, B)}
        )
        assert (D1, B) not in fills
        assert summary[1]["locked"] is True
        assert summary[1]["skipped_locked"] == 1 and summary[1]["to_fill"] == 0
        # Production on the locked spell recorded 33 -> the next spell carries it.
        assert fills[(D1, C)] == {1: 33}

    def test_later_saved_row_takes_pointer_ownership(self):
        cells = _cells(D1)
        existing = {(D1, C): {1: 10}}
        _, _, last_fill = plan_carry_forward(
            cells, set(cells), [1, 2], {1: 10, 2: 20}, existing, {}, set()
        )
        assert 1 not in last_fill  # D1/C saved row is newer than the D1/B fill
        assert last_fill[2] == (D1, C)


# =============================================================================
# helpers
# =============================================================================


class TestHelpers:
    def test_latest_per_loom_orders_by_date_then_spell_then_id(self):
        rank = {A: 0, B: 1, C: 2}
        rows = [
            {"machine_id": 1, "tran_date": D1, "spell_id": C, "weaving_quality_id": 3,
             "weaving_quality_map_id": 5},
            {"machine_id": 1, "tran_date": D1, "spell_id": A, "weaving_quality_id": 1,
             "weaving_quality_map_id": 9},
            {"machine_id": 2, "tran_date": D1, "spell_id": B, "weaving_quality_id": 4,
             "weaving_quality_map_id": 1},
            {"machine_id": 2, "tran_date": D1, "spell_id": B, "weaving_quality_id": 6,
             "weaving_quality_map_id": 2},
        ]
        assert latest_per_loom(rows, rank) == {1: 3, 2: 6}

    @pytest.mark.parametrize(
        "f,t,msg",
        [
            (D2, D1, "on or before"),
            (D1, date(2026, 10, 10), "Future"),
            (D1 - timedelta(days=40), D1, "At most"),
        ],
    )
    def test_validate_range_rejects(self, f, t, msg):
        with pytest.raises(HTTPException) as ei:
            validate_range(f, t, date(2026, 10, 9))
        assert ei.value.status_code == 400 and msg in ei.value.detail

    def test_validate_range_accepts_today_and_31_days(self):
        today = date(2026, 10, 9)
        validate_range(today - timedelta(days=30), today, today)

    def test_plant_today_is_never_behind_ist(self):
        """00:30 IST = 19:00 UTC the previous day: the IST date must win."""
        fixed_utc = bulk.datetime(2026, 10, 8, 19, 0, tzinfo=bulk.timezone.utc)

        class FakeDT(bulk.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

        class FakeDate(bulk.date):
            @classmethod
            def today(cls):
                return bulk.date(2026, 10, 8)

        with patch.object(bulk, "datetime", FakeDT), patch.object(bulk, "date", FakeDate):
            assert bulk.plant_today() == date(2026, 10, 9)

    def test_collect_groups_folds_and_dedupes(self):
        rows = [
            {"group_id": 1, "group_code": "WVGS4", "group_name": "S4", "machine_id": 5},
            {"group_id": 1, "group_code": "WVGS4", "group_name": "S4", "machine_id": 5},
            {"group_id": 1, "group_code": "WVGS4", "group_name": "S4", "machine_id": 6},
            {"group_id": 2, "group_code": "H", "group_name": "Hessian", "machine_id": 7},
        ]
        out = collect_groups("machine_group", rows)
        assert out == [
            {"source": "machine_group", "group_id": 1, "group_code": "WVGS4",
             "group_name": "S4", "machine_ids": [5, 6]},
            {"source": "machine_group", "group_id": 2, "group_code": "H",
             "group_name": "Hessian", "machine_ids": [7]},
        ]


# =============================================================================
# POST /quality_map_carry_forward
# =============================================================================

URL = "/api/weavingProd/quality_map_carry_forward"
SPELLS = [{"spell_id": A, "spell_code": "A1"}, {"spell_id": B, "spell_code": "B1"},
          {"spell_id": C, "spell_code": "C"}]


class TestCarryForwardEndpoint:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _post(self, body, *, locked=(), seed_rows=None, range_rows=None, prod_rows=None,
              pointers=None):
        seed_rows = seed_rows if seed_rows is not None else [
            {"machine_id": 1, "tran_date": D1 - timedelta(days=1), "spell_id": C,
             "weaving_quality_id": 10, "weaving_quality_map_id": 1},
        ]
        with patch.object(bulk, "_branch_spells", return_value=SPELLS), \
             patch.object(bulk, "_branch_looms", return_value=[1, 2]), \
             patch.object(bulk, "_map_rows", side_effect=[seed_rows, range_rows or []]), \
             patch.object(bulk, "_production_rows", return_value=prod_rows or []), \
             patch.object(bulk, "_pointers", return_value=pointers or []), \
             patch.object(bulk, "_is_locked",
                          side_effect=lambda db, co, br, d, s: (d, s) in set(locked)), \
             patch.object(bulk, "plant_today", return_value=date(2026, 10, 9)), \
             patch.object(bulk, "flag_reprocess_if_locked") as flag:
            resp = client.post(URL, json=body)
            return resp, flag

    def test_dry_run_reports_counts_and_writes_nothing(self):
        resp, flag = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-07",
                                 "dry_run": True})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["dry_run"] is True and data["to_fill"] == 3 and data["filled"] == 0
        assert [c["spell_code"] for c in data["cells"]] == ["A1", "B1", "C"]
        assert [c["no_previous"] for c in data["cells"]] == [1, 1, 1]  # loom 2 never mapped
        self.db.execute.assert_not_called()
        self.db.commit.assert_not_called()
        flag.assert_not_called()

    def test_pointer_seeds_loom_missing_from_lookback(self):
        resp, _ = self._post(
            {"co_id": 106, "branch_id": 87, "to_date": "2026-10-07", "dry_run": True},
            pointers=[{"machine_id": 2, "weaving_quality_id": 20,
                       "tran_date": date(2026, 8, 1), "spell_id": A}],
        )
        assert resp.json()["data"]["to_fill"] == 6

    def test_pointer_on_or_after_from_date_is_not_a_seed(self):
        resp, _ = self._post(
            {"co_id": 106, "branch_id": 87, "to_date": "2026-10-07", "dry_run": True},
            pointers=[{"machine_id": 2, "weaving_quality_id": 20, "tran_date": D1,
                       "spell_id": C}],
        )
        assert resp.json()["data"]["to_fill"] == 3

    def test_real_run_inserts_restamps_moves_pointer_flags_and_commits(self):
        prod = [{"weaving_daily_id": 501, "tran_date": D1, "spell_id": A, "machine_id": 1,
                 "weaving_quality_id": None}]
        resp, flag = self._post(
            {"co_id": 106, "branch_id": 87, "to_date": "2026-10-07"}, prod_rows=prod
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["filled"] == 3 and data["restamped"] == 1
        calls = self.db.execute.call_args_list
        assert len(calls) == 3  # insert batch, restamp batch, pointer batch
        inserted = calls[0].args[1]
        assert {(r["spell_id"], r["machine_id"], r["weaving_quality_id"]) for r in inserted} \
            == {(A, 1, 10), (B, 1, 10), (C, 1, 10)}
        assert all(r["updated_by"] == 7 and r["branch_id"] == 87 for r in inserted)
        assert calls[1].args[1] == [{"id": 501, "weaving_quality_id": 10, "updated_by": 7}]
        assert calls[2].args[1] == [{"co_id": 106, "branch_id": 87, "machine_id": 1,
                                     "weaving_quality_id": 10, "tran_date": D1,
                                     "spell_id": C}]
        assert flag.call_count == 3
        self.db.commit.assert_called_once()

    def test_locked_cell_is_reported_and_not_written(self):
        resp, flag = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-07"},
                                locked=[(D1, B)])
        data = resp.json()["data"]
        assert data["filled"] == 2
        assert data["cells"][1]["locked"] is True and data["cells"][1]["skipped_locked"] == 1
        written = {r["spell_id"] for r in self.db.execute.call_args_list[0].args[1]}
        assert written == {A, C}
        assert flag.call_count == 2

    def test_existing_rows_mean_nothing_to_do_and_no_writes(self):
        rng = [{"machine_id": 1, "tran_date": D1, "spell_id": s, "weaving_quality_id": 10,
                "weaving_quality_map_id": 100 + s} for s in (A, B, C)]
        resp, _ = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-07"},
                             range_rows=rng)
        data = resp.json()["data"]
        assert data["filled"] == 0 and data["to_fill"] == 0
        assert [c["already_mapped"] for c in data["cells"]] == [1, 1, 1]
        self.db.execute.assert_not_called()
        self.db.commit.assert_not_called()

    def test_spell_subset_only_fills_those_spells(self):
        resp, _ = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-07",
                              "spell_ids": [C]})
        data = resp.json()["data"]
        assert [c["spell_id"] for c in data["cells"]] == [C]
        assert data["filled"] == 1

    def test_unknown_spell_is_400(self):
        resp, _ = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-07",
                              "spell_ids": [999]})
        assert resp.status_code == 400 and "999" in resp.json()["detail"]
        self.db.commit.assert_not_called()

    def test_future_date_is_400(self):
        resp, _ = self._post({"co_id": 106, "branch_id": 87, "to_date": "2026-10-10"})
        assert resp.status_code == 400

    def test_missing_branch_is_422(self):
        resp = client.post(URL, json={"co_id": 106, "to_date": "2026-10-07"})
        assert resp.status_code == 422


# =============================================================================
# GET /quality_map_groups
# =============================================================================


def _rows(*maps):
    out = []
    for m in maps:
        r = MagicMock()
        r._mapping = m
        out.append(r)
    return out


class TestGroupsEndpoint:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_machine_and_loom_groups(self):
        mg, lg = MagicMock(), MagicMock()
        mg.fetchall.return_value = _rows(
            {"group_id": 1, "group_code": "WVGS4", "group_name": "WVGS4", "machine_id": 5})
        lg.fetchall.return_value = _rows(
            {"group_id": 3, "group_code": "WVR-01", "group_name": "Weaver 1", "machine_id": 6})
        self.db.execute.side_effect = [mg, lg]
        resp = client.get("/api/weavingProd/quality_map_groups?co_id=2&branch_id=29")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert [(g["source"], g["machine_ids"]) for g in data] == [
            ("machine_group", [5]), ("loom_group", [6])]

    def test_missing_table_on_tenant_is_skipped(self):
        mg = MagicMock()
        mg.fetchall.return_value = []
        orig = Exception(1146, "Table 'x.jute_prod_loom_group' doesn't exist")
        orig.args = (1146, "Table doesn't exist")
        self.db.execute.side_effect = [mg, ProgrammingError("SELECT", {}, orig)]
        resp = client.get("/api/weavingProd/quality_map_groups?co_id=2&branch_id=29")
        assert resp.status_code == 200 and resp.json()["data"] == []

    def test_branch_required(self):
        resp = client.get("/api/weavingProd/quality_map_groups?co_id=2")
        assert resp.status_code == 400
