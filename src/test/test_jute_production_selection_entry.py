"""Tests for the Selection Trolley Weight entry endpoints.

Tests for src/juteProduction/selection_entry.py (prefix /api/selectionProd), spec
../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md §3.

Portal persona: get_tenant_db + get_current_user_with_refresh are overridden via
app.dependency_overrides (the bale-entry pattern). The router is mounted on a local
FastAPI() app under its real prefix. The DB session is a MagicMock: rows expose a dict
._mapping for fetchall reads and attribute access for fetchone reads. There is NO
database connectivity in this suite.

``_resolve_spell`` (spinning_entry's shared contract) is patched in the router's
namespace to return the given spell_id, except in the test that is about it.

Execute order on the write path (documented in selection_entry._prepare_entry), with
spell patched: branch -> quality (D4) -> trolley (D1) -> [gate entry (D7)] ->
INSERT/UPDATE -> echo SelectionRow. Edit prepends the active-by-id guard; delete is
guard -> soft-delete.

Coverage (spec §3.4):
* entry_setup: shape (qualities / gate_entries with split item_ids / trollies filtered
  to type T / spells de-duplicated / quality_window), default tran_date, missing
  co_id / branch_id 400, bad date 400, bad branch 400
* entries_by_date: rows + totals + by_quality serialisation, missing tran_date 400
* entry_create: happy path (net = gross − tare, snapshot binds, echo row), gate entry +
  spell binds, every 400 branch (future date, gross out of range, bad branch, quality
  outside window, unknown / untagged / wrong-type / other-branch trolley, gate entry
  without the quality, net <= 0, spell rejected by the real _resolve_spell)
* entry_edit: keeps the tare snapshot when the trolley is unchanged, re-reads the
  master when it changes, 404 missing, requires co_id, UPDATE carries co_id
* entry_delete: soft-delete binds include co_id, 404, requires co_id
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.juteProduction.selection_entry as selection_entry
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    SELECTION_GROSS_MAX,
    SELECTION_MACHINE_TYPE_NAME,
    SELECTION_QUALITY_WINDOW_DAYS,
)

app = FastAPI()
app.include_router(selection_entry.router, prefix="/api/selectionProd", tags=["jute-selection"])
client = TestClient(app)

BASE = "/api/selectionProd"


def _mock_row(mapping: dict):
    """A query row whose ._mapping is a plain dict (matches r._mapping usage)."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    """A fetchone() row exposing attributes (e.g. .branch_id, .trolly_weight)."""
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    """Build a MagicMock execute() result; the _UNSET sentinel keeps an explicit
    ``fetchone=None`` ("row not found") honoured."""
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


PAST_DATE = "2026-01-24"
FUTURE_DATE = str(date.today() + timedelta(days=7))

# dev3 fixture: company 1 / Factory (branch 2); quality D TD 5 (item 269847); trolley 4
# "74" tare 77 kg tagged Selection; gate entry 36 = jute_mr 27755. gross 250.5 -> net 173.5.
ITEM_ID = 269847
TROLLY = dict(
    trolly_id=4, trolly_name="74", trolly_weight=77.0, branch_id=2, trolly_type="T",
    machine_type_id=28, machine_type_name=SELECTION_MACHINE_TYPE_NAME,
)


def _branch_ok():
    return _attr_row(branch_id=2)


def _quality_ok():
    return _attr_row(item_id=ITEM_ID)


def _trolly(**over):
    return _attr_row(**dict(TROLLY, **over))


def _echo_row(**over):
    m = {
        "selection_wt_id": 501, "co_id": 1, "branch_id": 2, "tran_date": date(2026, 1, 24),
        "spell_id": 1, "spell_code": "A1", "item_id": ITEM_ID, "item_code": "2",
        "item_name": "D TD 5", "trolly_id": 4, "trolly_name": "74", "jute_mr_id": 27755,
        "gate_entry_no": 36, "gate_entry_date": date(2026, 1, 18), "trolly_wt": 77.0,
        "gross_wt": 250.5, "net_wt": 173.5, "weigh_time": datetime(2026, 1, 24, 10, 15, 0),
        "remarks": None,
    }
    m.update(over)
    return _mock_row(m)


def _create_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 2,
        "tran_date": PAST_DATE,
        "spell_id": 1,
        "item_id": ITEM_ID,
        "trolly_id": 4,
        "jute_mr_id": 27755,
        "gross_wt": 250.5,
        "remarks": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def session(monkeypatch):
    """Mocked session + auth, with _resolve_spell patched to echo the spell_id."""
    mock_session = MagicMock()
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    app.dependency_overrides[get_tenant_db] = lambda: mock_session
    monkeypatch.setattr(selection_entry, "_resolve_spell", lambda db, sid, code, br: int(sid))
    yield mock_session
    app.dependency_overrides.clear()


def _stmt(session, idx) -> str:
    return str(session.execute.call_args_list[idx].args[0])


def _binds(session, idx) -> dict:
    return session.execute.call_args_list[idx].args[1]


# =============================================================================
# entry_setup
# =============================================================================


class TestEntrySetup:
    def test_success_shape(self, session):
        quality_rows = [
            _mock_row({"item_id": ITEM_ID, "item_code": "2", "item_name": "D TD 5", "issued_kg": 21480.13}),
            _mock_row({"item_id": 269848, "item_code": "3", "item_name": "D TD 6", "issued_kg": 10924.56}),
        ]
        ge_rows = [
            _mock_row({"jute_mr_id": 27758, "gate_entry_no": 39, "gate_entry_date": date(2026, 1, 23),
                       "supplier_name": "ALI AHAMAD", "item_ids": "269847,269848", "issued_kg": 9480.0}),
            _mock_row({"jute_mr_id": 27759, "gate_entry_no": 40, "gate_entry_date": date(2026, 1, 19),
                       "supplier_name": None, "item_ids": "269858", "issued_kg": 8410.0}),
        ]
        trolly_rows = [
            _mock_row({"trolly_id": 4, "trolly_name": "74", "trolly_weight": 77.0, "bucket_weight": None,
                       "trolly_posting_code": "T74", "branch_id": 2, "trolly_type": "T",
                       "machine_type_id": 28, "machine_type_name": "Selection"}),
            _mock_row({"trolly_id": 9, "trolly_name": "B1", "trolly_weight": 5.0, "bucket_weight": 5.0,
                       "trolly_posting_code": None, "branch_id": 2, "trolly_type": "B",
                       "machine_type_id": 28, "machine_type_name": "Selection"}),
        ]
        spell_rows = [
            _mock_row({"spell_id": 1, "spell_code": "A1", "spell_name": "A1", "working_hours": 5.0}),
            _mock_row({"spell_id": 9, "spell_code": "A1", "spell_name": "A1", "working_hours": 5.0}),
            _mock_row({"spell_id": 2, "spell_code": "B1", "spell_name": "B1", "working_hours": 5.0}),
        ]
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchall=quality_rows),
            _exec_result(fetchall=ge_rows),
            _exec_result(fetchall=trolly_rows),
            _exec_result(fetchall=spell_rows),
        ]

        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=2&tran_date={PAST_DATE}")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"qualities", "gate_entries", "trollies", "spells", "quality_window"}
        assert data["qualities"] == [
            {"item_id": ITEM_ID, "item_code": "2", "item_name": "D TD 5", "issued_kg": 21480.13},
            {"item_id": 269848, "item_code": "3", "item_name": "D TD 6", "issued_kg": 10924.56},
        ]
        assert data["gate_entries"] == [
            {"jute_mr_id": 27758, "gate_entry_no": 39, "gate_entry_date": "2026-01-23",
             "supplier_name": "ALI AHAMAD", "item_ids": [269847, 269848], "issued_kg": 9480.0},
            {"jute_mr_id": 27759, "gate_entry_no": 40, "gate_entry_date": "2026-01-19",
             "supplier_name": None, "item_ids": [269858], "issued_kg": 8410.0},
        ]
        # basket (type B) row dropped; only type T trolleys are offered (D1)
        assert data["trollies"] == [
            {"trolly_id": 4, "trolly_name": "74", "trolly_weight": 77.0, "trolly_posting_code": "T74"}
        ]
        # duplicate spell_code collapses to one
        assert data["spells"] == [
            {"spell_id": 1, "spell_code": "A1", "spell_name": "A1"},
            {"spell_id": 2, "spell_code": "B1", "spell_name": "B1"},
        ]
        assert data["quality_window"] == {
            "date_from": "2026-01-18", "date_to": PAST_DATE, "days": SELECTION_QUALITY_WINDOW_DAYS,
        }
        # branch guard is co-scoped; the window binds carry the date + days
        assert _binds(session, 0) == {"co_id": 1, "branch_id": 2}
        q_binds = _binds(session, 1)
        assert str(q_binds["tran_date"]) == PAST_DATE and q_binds["days"] == SELECTION_QUALITY_WINDOW_DAYS
        assert q_binds["branch_id"] == 2
        assert "actual_item_id" in _stmt(session, 1) and "status_id <> 4" in _stmt(session, 1)
        assert "jute_supplier_mst" in _stmt(session, 2)
        # trolley picker resolves the Selection stage by NAME
        assert _binds(session, 3) == {"branch_id": 2, "machine_type_name": SELECTION_MACHINE_TYPE_NAME}
        assert _binds(session, 4) == {"branch_id": 2}

    def test_defaults_to_today(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchall=[]), _exec_result(fetchall=[]),
            _exec_result(fetchall=[]), _exec_result(fetchall=[]),
        ]
        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=2")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["quality_window"]["date_to"] == date.today().isoformat()
        assert data["qualities"] == [] and data["gate_entries"] == [] and data["trollies"] == []

    def test_missing_co_id(self, session):
        response = client.get(f"{BASE}/entry_setup?branch_id=2")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        session.execute.assert_not_called()

    def test_missing_branch_id(self, session):
        response = client.get(f"{BASE}/entry_setup?co_id=1")
        assert response.status_code == 400
        assert "branch_id" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_tran_date(self, session):
        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=2&tran_date=24-01-2026")
        assert response.status_code == 400
        assert "tran_date" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_branch(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=99")
        assert response.status_code == 400
        assert "branch" in response.json()["detail"].lower()
        assert session.execute.call_count == 1


# =============================================================================
# entries_by_date
# =============================================================================


class TestEntriesByDate:
    def test_rows_totals_and_by_quality(self, session):
        rows = [
            _echo_row(),
            _echo_row(selection_wt_id=502, gross_wt=300.0, net_wt=223.0, jute_mr_id=None,
                      gate_entry_no=None, gate_entry_date=None, spell_id=None, spell_code=None),
        ]
        by_q = [_mock_row({"item_id": ITEM_ID, "item_name": "D TD 5", "trolleys": 2, "net_wt": 396.5})]
        session.execute.side_effect = [_exec_result(fetchall=rows), _exec_result(fetchall=by_q)]

        response = client.get(
            f"{BASE}/entries_by_date?co_id=1&branch_id=2&tran_date={PAST_DATE}&spell_id=1"
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"rows", "totals", "by_quality"}
        row = data["rows"][0]
        assert row == {
            "selection_wt_id": 501, "co_id": 1, "branch_id": 2, "tran_date": PAST_DATE,
            "spell_id": 1, "spell_code": "A1", "item_id": ITEM_ID, "item_code": "2",
            "item_name": "D TD 5", "trolly_id": 4, "trolly_name": "74", "jute_mr_id": 27755,
            "gate_entry_no": 36, "gate_entry_date": "2026-01-18", "trolly_wt": 77.0,
            "gross_wt": 250.5, "net_wt": 173.5, "weigh_time": "2026-01-24 10:15:00",
            "remarks": None,
        }
        assert data["rows"][1]["jute_mr_id"] is None and data["rows"][1]["gate_entry_no"] is None
        assert data["totals"] == {"trolleys": 2, "gross_wt": 550.5, "trolly_wt": 154.0, "net_wt": 396.5}
        assert data["by_quality"] == [
            {"item_id": ITEM_ID, "item_name": "D TD 5", "trolleys": 2, "net_wt": 396.5}
        ]
        binds = _binds(session, 0)
        assert binds["spell_id"] == 1 and binds["branch_id"] == 2 and binds["selection_wt_id"] is None
        assert str(binds["tran_date"]) == PAST_DATE
        assert _binds(session, 1)["spell_id"] == 1

    def test_missing_tran_date(self, session):
        response = client.get(f"{BASE}/entries_by_date?co_id=1&branch_id=2")
        assert response.status_code == 400
        assert "tran_date" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_missing_branch_id(self, session):
        response = client.get(f"{BASE}/entries_by_date?co_id=1&tran_date={PAST_DATE}")
        assert response.status_code == 400
        assert "branch_id" in response.json()["detail"]


# =============================================================================
# entry_create
# =============================================================================


def _happy_side_effects(*, gate=True, trolly=None, echo=None):
    """branch -> quality -> trolley -> [gate entry] -> insert -> echo."""
    effects = [
        _exec_result(fetchone=_branch_ok()),
        _exec_result(fetchone=_quality_ok()),
        _exec_result(fetchone=trolly or _trolly()),
    ]
    if gate:
        effects.append(_exec_result(fetchone=_attr_row(jute_mr_id=27755)))
    effects += [
        _exec_result(lastrowid=501),
        _exec_result(fetchone=echo or _echo_row()),
    ]
    return effects


class TestEntryCreate:
    def test_happy_path_net_and_snapshot(self, session):
        session.execute.side_effect = _happy_side_effects()

        response = client.post(f"{BASE}/entry_create", json=_create_payload())

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"row"}
        assert data["row"]["selection_wt_id"] == 501
        assert data["row"]["net_wt"] == 173.5 and data["row"]["trolly_wt"] == 77.0
        assert data["row"]["weigh_time"] == "2026-01-24 10:15:00"

        # gates in order, all scoped
        assert _binds(session, 0) == {"co_id": 1, "branch_id": 2}
        q = _binds(session, 1)
        assert q["item_id"] == ITEM_ID and q["branch_id"] == 2 and q["days"] == SELECTION_QUALITY_WINDOW_DAYS
        assert _binds(session, 2) == {"trolly_id": 4}
        g = _binds(session, 3)
        assert g["jute_mr_id"] == 27755 and g["item_id"] == ITEM_ID and g["branch_id"] == 2
        # D2 + snapshot
        assert "INSERT INTO jute_prod_selection_trolly_wt" in _stmt(session, 4)
        ins = _binds(session, 4)
        assert ins["gross_wt"] == 250.5 and ins["trolly_wt"] == 77.0 and ins["net_wt"] == 173.5
        assert ins["co_id"] == 1 and ins["branch_id"] == 2 and ins["item_id"] == ITEM_ID
        assert ins["trolly_id"] == 4 and ins["spell_id"] == 1 and ins["jute_mr_id"] == 27755
        assert ins["remarks"] is None and ins["updated_by"] == 7
        assert str(ins["tran_date"]) == PAST_DATE
        # echo is by id + co
        echo = _binds(session, 5)
        assert echo["selection_wt_id"] == 501 and echo["co_id"] == 1
        session.commit.assert_called_once()

    def test_happy_path_without_spell_or_gate_entry(self, session):
        session.execute.side_effect = _happy_side_effects(
            gate=False,
            echo=_echo_row(spell_id=None, spell_code=None, jute_mr_id=None, gate_entry_no=None,
                           gate_entry_date=None),
        )

        response = client.post(
            f"{BASE}/entry_create",
            json=_create_payload(spell_id=None, jute_mr_id=None, remarks="  wet lot  "),
        )

        assert response.status_code == 200, response.text
        ins = _binds(session, 3)
        assert "INSERT INTO" in _stmt(session, 3)
        assert ins["spell_id"] is None and ins["jute_mr_id"] is None
        assert ins["remarks"] == "wet lot"
        assert session.execute.call_count == 5

    # ------------------------------------------------------------ 400 branches
    def test_missing_item_id_is_schema_error(self, session):
        payload = _create_payload()
        payload.pop("item_id")
        response = client.post(f"{BASE}/entry_create", json=payload)
        assert response.status_code == 422
        session.execute.assert_not_called()

    def test_future_date(self, session):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(tran_date=FUTURE_DATE))
        assert response.status_code == 400
        assert "future" in response.json()["detail"].lower()
        session.execute.assert_not_called()

    @pytest.mark.parametrize("gross", [0, 0.5, SELECTION_GROSS_MAX + 1, -5])
    def test_gross_out_of_range(self, session, gross):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(gross_wt=gross))
        assert response.status_code == 400
        assert "gross_wt" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_branch(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(branch_id=99))
        assert response.status_code == 400
        assert "branch" in response.json()["detail"].lower()
        assert session.execute.call_count == 1
        session.rollback.assert_called()

    def test_quality_outside_window(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=None),  # D4 guard misses
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(item_id=999))
        assert response.status_code == 400
        assert response.json()["detail"] == selection_entry.QUALITY_WINDOW_MSG
        assert "7 days" in response.json()["detail"]
        assert session.execute.call_count == 2
        session.commit.assert_not_called()

    def test_unknown_trolly(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=None),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(trolly_id=999))
        assert response.status_code == 400
        assert "trolly_id" in response.json()["detail"]

    @pytest.mark.parametrize(
        "over",
        [
            {"machine_type_name": None, "machine_type_id": None},  # untagged
            {"machine_type_name": "Spinning"},                     # tagged to another stage
            {"trolly_type": "B"},                                  # a basket, not a trolley
            {"branch_id": 1},                                      # another branch's trolley
        ],
    )
    def test_trolley_not_tagged_to_selection(self, session, over):
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=_trolly(**over)),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload())
        assert response.status_code == 400
        assert response.json()["detail"] == selection_entry.TROLLY_NOT_TAGGED_MSG
        assert session.execute.call_count == 3
        session.commit.assert_not_called()

    def test_selection_tag_is_case_insensitive(self, session):
        session.execute.side_effect = _happy_side_effects(trolly=_trolly(machine_type_name="SELECTION"))
        response = client.post(f"{BASE}/entry_create", json=_create_payload())
        assert response.status_code == 200, response.text

    def test_gate_entry_without_quality(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=None),  # D7 guard misses
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(jute_mr_id=27759))
        assert response.status_code == 400
        assert response.json()["detail"] == selection_entry.GATE_ENTRY_MSG
        assert _binds(session, 3)["jute_mr_id"] == 27759
        assert session.execute.call_count == 4

    def test_net_not_positive(self, session):
        """gross 50 − tare 77 = −27 -> 400, nothing inserted."""
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=_attr_row(jute_mr_id=27755)),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(gross_wt=50))
        assert response.status_code == 400
        assert "net_wt" in response.json()["detail"]
        assert session.execute.call_count == 4
        session.rollback.assert_called()

    def test_spell_rejected_by_resolver(self, session, monkeypatch):
        """The real _resolve_spell runs: no status=1 spell row -> its own 400 surfaces."""
        from src.juteProduction.spinning_entry import _resolve_spell as real_resolve
        monkeypatch.setattr(selection_entry, "_resolve_spell", real_resolve)
        session.execute.side_effect = [
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=None),  # spell lookup
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(spell_id=9999))
        assert response.status_code == 400
        assert "spell" in response.json()["detail"].lower()
        assert session.execute.call_count == 4

    def test_spell_resolved_with_branch(self, session, monkeypatch):
        seen = {}

        def fake(db, sid, code, br):
            seen.update(sid=sid, code=code, br=br)
            return int(sid)

        monkeypatch.setattr(selection_entry, "_resolve_spell", fake)
        session.execute.side_effect = _happy_side_effects()
        response = client.post(f"{BASE}/entry_create", json=_create_payload(spell_id=3))
        assert response.status_code == 200, response.text
        assert seen == {"sid": 3, "code": None, "br": 2}


# =============================================================================
# entry_edit
# =============================================================================


def _existing(**over):
    base = dict(
        selection_wt_id=501, co_id=1, branch_id=2, tran_date=date(2026, 1, 24), spell_id=1,
        item_id=ITEM_ID, trolly_id=4, jute_mr_id=27755, trolly_wt=75.0, gross_wt=250.5,
        net_wt=175.5, weigh_time=datetime(2026, 1, 24, 10, 15, 0), remarks=None,
    )
    base.update(over)
    return _attr_row(**base)


class TestEntryEdit:
    def _edit_effects(self, existing, trolly=None, echo=None, gate=True):
        effects = [
            _exec_result(fetchone=existing),
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=_quality_ok()),
            _exec_result(fetchone=trolly or _trolly()),
        ]
        if gate:
            effects.append(_exec_result(fetchone=_attr_row(jute_mr_id=27755)))
        effects += [_exec_result(), _exec_result(fetchone=echo or _echo_row())]
        return effects

    def test_edit_keeps_snapshot_when_trolley_unchanged(self, session):
        """Stored tare 75 (master now 77) is KEPT: net = 260 − 75 = 185."""
        session.execute.side_effect = self._edit_effects(
            _existing(trolly_wt=75.0), echo=_echo_row(trolly_wt=75.0, gross_wt=260.0, net_wt=185.0)
        )

        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(gross_wt=260.0))

        assert response.status_code == 200, response.text
        assert set(response.json()["data"]) == {"row"}
        assert response.json()["data"]["row"]["net_wt"] == 185.0
        assert "UPDATE jute_prod_selection_trolly_wt" in _stmt(session, 5)
        assert "co_id = :co_id" in _stmt(session, 5)
        upd = _binds(session, 5)
        assert upd["id"] == 501 and upd["co_id"] == 1
        assert upd["trolly_wt"] == 75.0 and upd["gross_wt"] == 260.0 and upd["net_wt"] == 185.0
        assert upd["updated_by"] == 7
        session.commit.assert_called_once()

    def test_edit_rereads_tare_when_trolley_changes(self, session):
        """Trolley 4 -> 7 (tare 80): the master value replaces the old 75 snapshot."""
        session.execute.side_effect = self._edit_effects(
            _existing(trolly_id=4, trolly_wt=75.0),
            trolly=_trolly(trolly_id=7, trolly_name="8", trolly_weight=80.0),
            echo=_echo_row(trolly_id=7, trolly_name="8", trolly_wt=80.0, net_wt=170.5),
        )

        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(trolly_id=7))

        assert response.status_code == 200, response.text
        upd = _binds(session, 5)
        assert upd["trolly_id"] == 7 and upd["trolly_wt"] == 80.0 and upd["net_wt"] == 170.5

    def test_edit_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(f"{BASE}/entry_edit/999?co_id=1", json=_create_payload())
        assert response.status_code == 404
        assert "co_id = :co_id" in _stmt(session, 0) and _binds(session, 0)["co_id"] == 1
        session.commit.assert_not_called()

    def test_edit_requires_co_id(self, session):
        response = client.put(f"{BASE}/entry_edit/501", json=_create_payload())
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        session.execute.assert_not_called()

    def test_edit_quality_outside_window(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_existing()),
            _exec_result(fetchone=_branch_ok()),
            _exec_result(fetchone=None),
        ]
        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(item_id=1))
        assert response.status_code == 400
        assert response.json()["detail"] == selection_entry.QUALITY_WINDOW_MSG
        session.rollback.assert_called()


# =============================================================================
# entry_delete
# =============================================================================


class TestEntryDelete:
    def test_soft_delete_binds_co_id(self, session):
        session.execute.side_effect = [_exec_result(fetchone=_existing()), _exec_result()]

        response = client.delete(f"{BASE}/entry_delete/501?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"] == {"selection_wt_id": 501}
        stmt = _stmt(session, 1)
        assert "active = 0" in stmt and "co_id = :co_id" in stmt
        assert _binds(session, 1) == {"id": 501, "co_id": 1, "updated_by": 7}
        session.commit.assert_called_once()

    def test_delete_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.delete(f"{BASE}/entry_delete/999?co_id=99")
        assert response.status_code == 404
        assert _binds(session, 0)["co_id"] == 99
        session.commit.assert_not_called()

    def test_delete_requires_co_id(self, session):
        response = client.delete(f"{BASE}/entry_delete/501")
        assert response.status_code == 400
        session.execute.assert_not_called()


# =============================================================================
# window_bounds helper
# =============================================================================


def test_window_bounds_are_inclusive_calendar_days():
    """issue_date > d − 7 AND issue_date <= d  ⇔  d − 6 <= issue_date <= d."""
    w = selection_entry.window_bounds(date(2026, 1, 24))
    assert w == {"date_from": "2026-01-18", "date_to": "2026-01-24", "days": 7}
