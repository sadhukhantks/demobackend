"""Winding spell report (Winder Wise Production tab) — machine + target columns (TKT-000169).

``GET /api/juteProductionReports/winding_spell_report`` feeds the Winder Wise
Production tab of the Winding Production page. TKT-000169 asked for three things
on it; the two that need the backend are tested here:

* the machine the winder worked on  -> ``mech_code`` / ``machine_name`` per row,
  read from the Quality tab's person -> quality map (winding entry is
  person-keyed, so the doff row has no machine of its own);
* the target production            -> ``target_kg`` per row, the winding standard
  (KG per winder per 8-hour shift) scaled to the spell's working hours, or None
  when the quality has no target configured.

(The third ask — other shifts visible, not just the selected spell — is a
front-end change: the endpoint already returned every spell for the date.)

The arithmetic lives in pure builders and is exercised on stub rows; the machine
query is real SQL, so it is run against a throwaway in-memory SQLite table
(test_winding_quality_wise_report.py's convention) instead of grepped; only the
wiring goes through TestClient with the DB + auth dependencies overridden.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.main import app
from src.juteProduction.winding_query import get_winding_machine_by_winder_query
from src.juteProduction.winding_reports import (
    build_spell_report_rows,
    get_current_user_with_refresh,
    get_tenant_db,
    index_winder_machines,
    spell_report_note,
    spell_target_kg,
)

client = TestClient(app)

URL = "/api/juteProductionReports/winding_spell_report"


def _recon(spell_id, shift, eb_id, item_id, kg, spell_code=None):
    return {
        "tran_date": date(2026, 10, 1),
        "spell_id": spell_id,
        "spell_code": spell_code or shift,
        "shift": shift,
        "eb_id": eb_id,
        "emp_code": f"E{eb_id}",
        "worker_name": f"WINDER {eb_id}",
        "item_id": item_id,
        "item_code": f"Q{item_id}",
        "item_name": f"QUALITY {item_id}",
        "sum_production": kg,
        "opening_jugar": 0,
        "closing_jugar": 0,
        "production_kg": kg,
    }


def _machine(spell_id, eb_id, machine_id, code=None):
    return {
        "spell_id": spell_id,
        "eb_id": eb_id,
        "machine_id": machine_id,
        "mech_code": code,
        "machine_name": f"WINDING {code}" if code else None,
    }


# 109 = A (8 h), 110 = B (8 h), 101 = C (7.5 h) — the sls branch-29 spells.
SPELL_HOURS = {109: 8.0, 110: 8.0, 101: 7.5}


class TestMachineIndex:
    def test_maps_spell_and_winder_to_machine(self):
        idx = index_winder_machines([_machine(109, 10, 5, "W-05")])
        assert idx[(109, 10)] == {
            "machine_id": 5, "mech_code": "W-05", "machine_name": "WINDING W-05",
        }

    def test_same_winder_in_two_spells_keeps_each_spells_machine(self):
        idx = index_winder_machines([_machine(109, 10, 5, "W-05"), _machine(110, 10, 8, "W-08")])
        assert idx[(109, 10)]["mech_code"] == "W-05"
        assert idx[(110, 10)]["mech_code"] == "W-08"

    def test_a_blank_machine_row_does_not_erase_a_real_one(self):
        idx = index_winder_machines([_machine(109, 10, 5, "W-05"), _machine(109, 10, None)])
        assert idx[(109, 10)]["machine_id"] == 5

    def test_a_real_machine_replaces_an_earlier_blank(self):
        idx = index_winder_machines([_machine(109, 10, None), _machine(109, 10, 5, "W-05")])
        assert idx[(109, 10)]["machine_id"] == 5


class TestSpellTarget:
    def test_eight_hour_spell_carries_the_full_standard(self):
        assert spell_target_kg(1, 109, "A", {1: 120.0}, SPELL_HOURS) == 120.0

    def test_shorter_spell_is_scaled_to_its_hours(self):
        # 7.5 h of an 8 h standard
        assert spell_target_kg(1, 101, "C", {1: 120.0}, SPELL_HOURS) == 112.5

    def test_quality_without_a_target_is_none_not_zero(self):
        assert spell_target_kg(2, 109, "A", {1: 120.0}, SPELL_HOURS) is None

    def test_a_zero_target_is_a_real_zero(self):
        assert spell_target_kg(1, 109, "A", {1: 0.0}, SPELL_HOURS) == 0.0

    def test_spell_with_no_configured_hours_falls_back_like_the_low_producer_report(self):
        # 8 h for A/B, 7.5 h for C — lowprod_winding.FALLBACK_*
        assert spell_target_kg(1, 999, "A", {1: 80.0}, {}) == 80.0
        assert spell_target_kg(1, 999, "C", {1: 80.0}, {}) == 75.0


class TestBuildSpellReportRows:
    def _build(self):
        recon = [
            _recon(109, "A", 10, 1, 400.0),
            _recon(110, "B", 11, 1, 380.0),
            _recon(101, "C", 12, 2, 300.0),
            _recon(101, "C", 13, 3, 250.0),
        ]
        machines = index_winder_machines(
            [_machine(109, 10, 5, "W-05"), _machine(110, 11, 6, "W-06"), _machine(101, 12, None)]
        )
        return build_spell_report_rows(recon, machines, {1: 120.0}, SPELL_HOURS)

    def test_every_spell_of_the_day_is_returned_not_one(self):
        rows, _ = self._build()
        assert [r["shift"] for r in rows] == ["A", "B", "C", "C"]

    def test_machine_attached_per_winder_and_spell(self):
        rows, _ = self._build()
        assert rows[0]["mech_code"] == "W-05" and rows[0]["machine_id"] == 5
        assert rows[1]["mech_code"] == "W-06"

    def test_winder_with_no_machine_gets_blank_machine_fields(self):
        rows, _ = self._build()
        assert rows[2]["machine_id"] is None and rows[2]["mech_code"] is None
        # no quality-map row at all for eb 13
        assert rows[3]["machine_id"] is None and rows[3]["machine_name"] is None

    def test_target_per_row_and_none_when_unconfigured(self):
        rows, no_target = self._build()
        assert rows[0]["target_kg"] == 120.0
        assert rows[1]["target_kg"] == 120.0
        assert rows[2]["target_kg"] is None
        assert no_target == 2  # qualities 2 and 3

    def test_no_target_count_is_per_distinct_quality(self):
        recon = [_recon(109, "A", 10, 2, 100.0), _recon(110, "B", 11, 2, 90.0)]
        _, no_target = build_spell_report_rows(recon, {}, {}, SPELL_HOURS)
        assert no_target == 1

    def test_reconciled_production_is_passed_through_untouched(self):
        recon = [dict(_recon(109, "A", 10, 1, 454.0), opening_jugar=12, closing_jugar=5,
                      production_kg=447.0)]
        rows, _ = build_spell_report_rows(recon, {}, {}, SPELL_HOURS)
        assert rows[0]["sum_production"] == 454.0
        assert (rows[0]["opening_jugar"], rows[0]["closing_jugar"]) == (12.0, 5.0)
        assert rows[0]["production_kg"] == 447.0
        assert rows[0]["tran_date"] == "2026-10-01"

    def test_note_names_the_count_and_the_scaling_rule(self):
        note = spell_report_note(2)
        assert "2 quality(ies)" in note and "spell hours" in note


class TestMachineQuerySql:
    """get_winding_machine_by_winder_query run for real against SQLite."""

    def _rows(self, branch_id=29, tran_date="2026-10-01"):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE machine_mst (machine_id INTEGER PRIMARY KEY, "
                "mech_code TEXT, machine_name TEXT)"
            ))
            conn.execute(text(
                "CREATE TABLE jute_prod_winding_daily_qlty ("
                "winding_daily_qlty_id INTEGER PRIMARY KEY, branch_id INTEGER, "
                "tran_date TEXT, spell_id INTEGER, eb_id INTEGER, machine_id INTEGER, "
                "active INTEGER)"
            ))
            conn.execute(text("INSERT INTO machine_mst VALUES (5, 'W-05', 'WINDING 05')"))
            data = [
                # (id, branch, date, spell, eb, machine, active)
                (1, 29, "2026-10-01", 109, 10, 5, 1),        # normal
                (2, 29, "2026-10-01", 109, 11, None, 1),     # no machine -> still returned
                (3, 29, "2026-10-01", 109, 12, 5, 0),        # soft-deleted -> excluded
                (4, 29, "2026-10-01", 109, None, 5, 1),      # no winder -> excluded
                (5, 87, "2026-10-01", 109, 13, 5, 1),        # other branch -> excluded
                (6, 29, "2026-09-30", 109, 14, 5, 1),        # other date -> excluded
                (7, 29, "2026-10-01", 110, 15, 5, None),     # NULL active counts as active
            ]
            for r in data:
                conn.execute(
                    text("INSERT INTO jute_prod_winding_daily_qlty VALUES "
                         "(:a, :b, :c, :d, :e, :f, :g)"),
                    dict(zip("abcdefg", r)),
                )
            return conn.execute(
                get_winding_machine_by_winder_query(),
                {"branch_id": branch_id, "tran_date": tran_date},
            ).fetchall()

    def test_scoped_to_branch_date_active_and_winder(self):
        got = {(r.spell_id, r.eb_id): r.mech_code for r in self._rows()}
        assert got == {(109, 10): "W-05", (109, 11): None, (110, 15): "W-05"}

    def test_another_branch_sees_only_its_own(self):
        got = {r.eb_id for r in self._rows(branch_id=87)}
        assert got == {13}


class TestSpellReportEndpoint:
    def setup_method(self):
        self._session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}

    def teardown_method(self):
        app.dependency_overrides.clear()

    @staticmethod
    def _mapped(mappings):
        res = MagicMock()
        rows = []
        for m in mappings:
            row = MagicMock()
            row._mapping = m
            rows.append(row)
        res.fetchall.return_value = rows
        return res

    @staticmethod
    def _attrs(items):
        res = MagicMock()
        res.fetchall.return_value = [SimpleNamespace(**i) for i in items]
        return res

    @staticmethod
    def _branch_co(co_id):
        res = MagicMock()
        res.first.return_value = SimpleNamespace(co_id=co_id)
        return res

    def test_missing_branch_id_400(self):
        resp = client.get(URL, params={"report_date": "2026-10-01"})
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_malformed_date_400(self):
        resp = client.get(URL, params={"branch_id": 29, "report_date": "01-10-2026"})
        assert resp.status_code == 400

    def test_unknown_branch_400(self):
        missing = MagicMock()
        missing.first.return_value = None
        self._session.execute.return_value = missing
        resp = client.get(URL, params={"branch_id": 999, "report_date": "2026-10-01"})
        assert resp.status_code == 400
        assert "branch_id" in resp.json()["detail"]

    def test_happy_path_carries_machine_target_and_every_shift(self):
        self._session.execute.side_effect = [
            self._branch_co(2),
            self._mapped([
                _recon(109, "A", 10, 1, 400.0),
                _recon(110, "B", 11, 1, 380.0),
                _recon(101, "C", 12, 2, 300.0),
            ]),
            self._mapped([_machine(109, 10, 5, "W-05"), _machine(110, 11, 6, "W-06")]),
            self._attrs([{"item_id": 1, "value": 120.0}]),
            self._attrs([
                {"spell_id": 109, "working_hours": 8},
                {"spell_id": 110, "working_hours": 8},
                {"spell_id": 101, "working_hours": 7.5},
            ]),
        ]
        resp = client.get(URL, params={"branch_id": 29, "report_date": "2026-10-01"})
        assert resp.status_code == 200
        data = resp.json()["data"]

        by_shift = {r["shift"]: r for r in data["rows"]}
        assert set(by_shift) == {"A", "B", "C"}
        assert by_shift["A"]["mech_code"] == "W-05" and by_shift["A"]["target_kg"] == 120.0
        assert by_shift["B"]["mech_code"] == "W-06"
        assert by_shift["C"]["mech_code"] is None and by_shift["C"]["target_kg"] is None

        assert data["shift_rollup"] == [
            {"shift": "A", "production_kg": 400.0},
            {"shift": "B", "production_kg": 380.0},
            {"shift": "C", "production_kg": 300.0},
        ]
        assert data["total_production_kg"] == 1080.0
        assert "1 quality(ies)" in data["note"]

    def test_binds_branch_date_and_the_branchs_company(self):
        self._session.execute.side_effect = [
            self._branch_co(7),
            self._mapped([]),
            self._mapped([]),
            self._attrs([]),
            self._attrs([]),
        ]
        client.get(URL, params={"branch_id": 29, "tran_date": "2026-10-01"})
        calls = self._session.execute.call_args_list
        # call 0: company of the branch; 1: reconciliation; 2: machines; 3: targets
        assert calls[1].args[1]["branch_id"] == 29
        assert calls[2].args[1] == {"branch_id": 29, "tran_date": date(2026, 10, 1)}
        assert calls[3].args[1] == {"co_id": 7, "on_date": date(2026, 10, 1)}

    def test_db_error_500(self):
        self._session.execute.side_effect = RuntimeError("db exploded")
        resp = client.get(URL, params={"branch_id": 29, "report_date": "2026-10-01"})
        assert resp.status_code == 500
        assert "db exploded" in resp.json()["detail"]
