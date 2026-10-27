"""Tests for the Bale Production (Bale Press) endpoints.

Tests for src/juteProduction/bale_entry.py (prefix /api/baleProd).

Portal persona: get_tenant_db + get_current_user_with_refresh are overridden via
app.dependency_overrides (the waste-entry pattern). The router is mounted on a local
FastAPI() app here because src/main.py registration is owned by the coordinator; the
prefix matches the spec (/api/baleProd) so every URL below is the real one. The DB
session is a MagicMock: rows expose a dict ._mapping for fetchall reads and attribute
access for fetchone reads. There is NO database connectivity in this suite.

Two collaborators are patched in the router's namespace where a test is not about
them: ``_resolve_spell`` (spinning_entry's shared contract — returns the given spell_id)
and ``sync_balepress_daily`` (F10, unit-tested in test_bale_sync.py) — the latter is a
MagicMock so the "sync called with key" assertions are exact.

Execute order on the write path (documented in bale_entry._prepare_entry), with spell
and sync patched: quality -> series -> [machine] -> [trolly] -> [original (REPACK)] ->
duplicate -> INSERT/UPDATE -> bump series -> echo BaleRow. Edit prepends the
active-by-id guard; delete is guard -> soft-delete.

Coverage:
* entry_setup: shape (qualities/series/machines/trollies/spells) + entries_today_count
  with tran_date + missing co_id 400
* next_bale_no: with and without a prior entry, 404 unknown series, 400 missing param
* entries_by_date: rows + totals serialisation, missing tran_date 400, bad entry_type 400
* bale_search: rows + bale_no required
* entry_create: happy path (net_wt math, snapshot binds, F9 bump, sync called with the
  key, echo row), every 400 branch (missing co_id -> 422 body, future date, gross out of
  range, net <= 0, series/quality type mismatch, series item mismatch, PROD with
  original, REPACK without original, unknown quality / series / machine / trolly, bad
  entry_type, spell rejected by _resolve_spell), 409 clash + allow_duplicate bypass,
  REPACK unknown original 409 + bypass, REPACK happy path
* entry_edit: re-sync both keys when the key changed, single sync when unchanged,
  404 missing, UPDATE carries co_id, clash probe excludes self
* entry_delete: 404, soft-delete binds include co_id, sync called with the row's key
* series master: setup, list, create (happy / blank name / bad type / dup / bad item),
  edit (happy, 404, quality_type locked when referenced), delete (happy, blocked 400, 404)
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.juteProduction.bale_entry as bale_entry
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    BALE_GROSS_MAX,
    BALE_NO_MAX,
    FINISHING_ITEM_TYPE_ID,
    FINISHING_MACHINE_TYPE_NAMES,
)

app = FastAPI()
app.include_router(bale_entry.router, prefix="/api/baleProd", tags=["jute-bale-production"])
client = TestClient(app)

BASE = "/api/baleProd"


def _mock_row(mapping: dict):
    """A query row whose ._mapping is a plain dict (matches r._mapping usage)."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    """A fetchone() row exposing attributes (e.g. .branch_id, .quality_type)."""
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


PAST_DATE = str(date.today() - timedelta(days=10))  # inside the 60-day backdate window (spec §7d)
FUTURE_DATE = str(date.today() + timedelta(days=7))

# Sacking quality 12 (item 5001), packsheet 5 kg, std 290 kg; series 3 = PUNJAB (type 2,
# unbound), last_bale_no 24746; trolley 61 tare 12 kg. gross 294 -> net 277.
QUALITY = dict(
    finishing_quality_id=12, co_id=1, quality_type=2, item_id=5001,
    packsheet_wt=5.0, std_bale_weight=290.0, std_mr_pct=20.0, item_name="Govt-BT-WILL 580 GM BAG",
)
SERIES = dict(
    bale_series_id=3, co_id=1, branch_id=4, series_name="PUNJAB", quality_type=2,
    item_id=None, state_id=None, party_id=None, last_bale_no=24746,
)
MACHINE = dict(machine_id=210, machine_name="Bale Press 2", mech_code="BP2", branch_id=4,
               machine_type_name="Bale Press")
TROLLY = dict(trolly_id=61, trolly_name="61", trolly_weight=12.0, branch_id=4)


def _quality(**over):
    return _attr_row(**dict(QUALITY, **over))


def _series(**over):
    return _attr_row(**dict(SERIES, **over))


def _machine(**over):
    return _attr_row(**dict(MACHINE, **over))


def _trolly(**over):
    return _attr_row(**dict(TROLLY, **over))


def _echo_row(**over):
    m = {
        "bale_entry_id": 501, "co_id": 1, "branch_id": 4, "tran_date": date.fromisoformat(PAST_DATE),
        "spell_id": 91, "spell_code": "A1", "machine_id": 210, "machine_name": "Bale Press 2",
        "entry_type": "PROD", "finishing_quality_id": 12, "item_id": 5001,
        "item_name": QUALITY["item_name"], "quality_type": 2, "bale_series_id": 3,
        "series_name": "PUNJAB", "bale_no": 24747, "original_bale_no": None, "trolly_id": 61,
        "trolly_name": "61", "trolly_wt": 12.0, "packsheet_wt": 5.0, "gross_wt": 294.0,
        "net_wt": 277.0, "std_bale_wt": 290.0, "weigh_time": datetime(2026, 9, 10, 9, 15, 30),
        "remarks": None,
    }
    m.update(over)
    return _mock_row(m)


def _create_payload(**overrides):
    payload = {
        "co_id": 1,
        "branch_id": 4,
        "tran_date": PAST_DATE,
        "spell_id": 91,
        "machine_id": 210,
        "entry_type": "PROD",
        "finishing_quality_id": 12,
        "bale_series_id": 3,
        "bale_no": 24747,
        "trolly_id": 61,
        "gross_wt": 294.0,
        "remarks": None,
        "allow_duplicate": False,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def session(monkeypatch):
    """Mocked session + auth, with _resolve_spell and the F10 sync patched.

    Yields the MagicMock session; ``session.sync`` is the MagicMock standing in for
    sync_balepress_daily so tests can assert on the key(s) it was called with.
    """
    mock_session = MagicMock()
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    app.dependency_overrides[get_tenant_db] = lambda: mock_session
    monkeypatch.setattr(bale_entry, "_resolve_spell", lambda db, sid, code, br: int(sid))
    sync = MagicMock(return_value={"action": "updated"})
    monkeypatch.setattr(bale_entry, "sync_balepress_daily", sync)
    mock_session.sync = sync
    yield mock_session
    app.dependency_overrides.clear()


def _sync_kwargs(sync, idx=0):
    return sync.call_args_list[idx].kwargs


# =============================================================================
# entry_setup
# =============================================================================


class TestEntrySetup:
    def test_success_shape(self, session):
        quality_row = _mock_row(
            {
                "finishing_quality_id": 12, "item_id": 5001, "item_code": "2124",
                "item_name": QUALITY["item_name"], "quality_type": 2, "std_bale_weight": 290.0,
                "packsheet_wt": 5.0, "std_mr_pct": 20.0, "weft_yarn_item_id": 7001,
                "weft_count": 12.0,
            }
        )
        series_row = _mock_row(
            {"bale_series_id": 3, "series_name": "PUNJAB", "quality_type": 2, "item_id": None,
             "state_id": 14, "party_id": None, "last_bale_no": 24746}
        )
        machine_row = _mock_row(
            {"machine_id": 210, "machine_name": "Bale Press 2", "mech_code": "BP2",
             "dept_id": 60, "dept_name": "Finishing", "branch_id": 4}
        )
        trolly_row = _mock_row(
            {"trolly_id": 61, "trolly_name": "61", "trolly_weight": 12.0,
             "machine_type_id": 30, "machine_type_name": "Bale Press"}
        )
        spell_rows = [
            _mock_row({"spell_id": 91, "spell_code": "A1", "spell_name": "A1", "working_hours": 5.0}),
            _mock_row({"spell_id": 97, "spell_code": "A1", "spell_name": "A1", "working_hours": 5.0}),
        ]
        session.execute.return_value.fetchall.side_effect = [
            [quality_row], [series_row], [machine_row], [trolly_row], spell_rows,
        ]
        session.execute.return_value.fetchone.return_value = _attr_row(cnt=17)

        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=4&tran_date={PAST_DATE}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert set(data) == {"qualities", "series", "machines", "trollies", "spells",
                             "entries_today_count"}
        assert data["qualities"] == [
            {
                "finishing_quality_id": 12, "item_id": 5001, "item_code": "2124",
                "item_name": QUALITY["item_name"], "quality_type": 2, "std_bale_weight": 290.0,
                "packsheet_wt": 5.0, "std_mr_pct": 20.0, "weft_yarn_item_id": 7001,
                "weft_count": 12.0,
            }
        ]
        assert data["series"] == [
            {"bale_series_id": 3, "series_name": "PUNJAB", "quality_type": 2, "item_id": None,
             "state_id": 14, "party_id": None, "last_bale_no": 24746}
        ]
        assert data["machines"] == [
            {"machine_id": 210, "machine_name": "Bale Press 2", "mech_code": "BP2"}
        ]
        assert data["trollies"] == [{"trolly_id": 61, "trolly_name": "61", "trolly_weight": 12.0}]
        # duplicate spell_code (branch fan-out) collapses to one
        assert data["spells"] == [
            {"spell_id": 91, "spell_code": "A1", "spell_name": "A1", "working_hours": 5.0}
        ]
        assert data["entries_today_count"] == 17
        # machines + trollies bind the Bale Press type names (expanding bindparam)
        machine_binds = session.execute.call_args_list[2].args[1]
        assert machine_binds["machine_types"] == list(FINISHING_MACHINE_TYPE_NAMES["balepress"])
        trolly_binds = session.execute.call_args_list[3].args[1]
        assert trolly_binds["machine_types"] == list(FINISHING_MACHINE_TYPE_NAMES["balepress"])

    def test_without_tran_date_omits_count(self, session):
        session.execute.return_value.fetchall.return_value = []

        response = client.get(f"{BASE}/entry_setup?co_id=1&branch_id=4")

        assert response.status_code == 200
        data = response.json()["data"]
        assert "entries_today_count" not in data
        assert data["qualities"] == [] and data["spells"] == []

    def test_missing_co_id(self, session):
        response = client.get(f"{BASE}/entry_setup?branch_id=4")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        session.execute.assert_not_called()


# =============================================================================
# next_bale_no
# =============================================================================


class TestNextBaleNo:
    def test_with_prior_entry(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_series()),
            _exec_result(
                fetchone=_attr_row(
                    bale_entry_id=500, tran_date=date(2026, 9, 10), bale_no=24746,
                    gross_wt=292.5, net_wt=275.5, trolly_id=61, finishing_quality_id=12,
                )
            ),
        ]

        response = client.get(f"{BASE}/next_bale_no?co_id=1&bale_series_id=3")

        assert response.status_code == 200
        assert response.json()["data"] == {
            "bale_series_id": 3,
            "series_name": "PUNJAB",
            "last_bale_no": 24746,
            "next_bale_no": 24747,
            "last_entry": {
                "bale_entry_id": 500, "tran_date": "2026-09-10", "bale_no": 24746,
                "gross_wt": 292.5, "net_wt": 275.5, "trolly_id": 61, "finishing_quality_id": 12,
            },
        }
        # both lookups are co-scoped
        assert session.execute.call_args_list[0].args[1]["co_id"] == 1
        assert session.execute.call_args_list[1].args[1]["co_id"] == 1

    def test_without_prior_entry(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_series(last_bale_no=0, series_name='39"- 5.85', quality_type=1)),
            _exec_result(fetchone=None),
        ]

        response = client.get(f"{BASE}/next_bale_no?co_id=1&bale_series_id=3")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["last_bale_no"] == 0
        assert data["next_bale_no"] == 1
        assert data["last_entry"] is None

    def test_unknown_series_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get(f"{BASE}/next_bale_no?co_id=1&bale_series_id=999")
        assert response.status_code == 404

    def test_missing_series_param_400(self, session):
        response = client.get(f"{BASE}/next_bale_no?co_id=1")
        assert response.status_code == 400
        assert "bale_series_id" in response.json()["detail"]


# =============================================================================
# entries_by_date / bale_search
# =============================================================================


class TestReads:
    def test_entries_by_date_rows_and_totals(self, session):
        totals_row = _mock_row(
            {"finishing_quality_id": 12, "item_name": QUALITY["item_name"], "quality_type": 2,
             "entry_type": "PROD", "bales": 1, "total_gross": 294.0, "total_net": 277.0,
             "avg_net": 277.0, "std_bale_wt": 290.0}
        )
        session.execute.side_effect = [
            _exec_result(fetchall=[_echo_row()]),
            _exec_result(fetchall=[totals_row]),
        ]

        response = client.get(
            f"{BASE}/entries_by_date?co_id=1&branch_id=4&tran_date={PAST_DATE}&spell_id=91&entry_type=prod"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert set(data) == {"rows", "totals"}
        row = data["rows"][0]
        assert row["tran_date"] == PAST_DATE  # the echo row carries the relative fixture date
        assert row["weigh_time"] == "2026-09-10T09:15:30"
        assert row["net_wt"] == 277.0 and row["gross_wt"] == 294.0
        assert row["series_name"] == "PUNJAB" and row["original_bale_no"] is None
        assert data["totals"] == [
            {"finishing_quality_id": 12, "item_name": QUALITY["item_name"], "quality_type": 2,
             "entry_type": "PROD", "bales": 1, "total_gross": 294.0, "total_net": 277.0,
             "avg_net": 277.0, "std_bale_wt": 290.0}
        ]
        binds = session.execute.call_args_list[0].args[1]
        assert binds["spell_id"] == 91 and binds["entry_type"] == "PROD"
        assert str(binds["tran_date"]) == PAST_DATE

    def test_entries_by_date_missing_tran_date(self, session):
        response = client.get(f"{BASE}/entries_by_date?co_id=1&branch_id=4")
        assert response.status_code == 400
        assert "tran_date" in response.json()["detail"]

    def test_entries_by_date_bad_entry_type(self, session):
        response = client.get(
            f"{BASE}/entries_by_date?co_id=1&branch_id=4&tran_date={PAST_DATE}&entry_type=BOGUS"
        )
        assert response.status_code == 400
        assert "entry_type" in response.json()["detail"]

    def test_bale_search(self, session):
        session.execute.return_value.fetchall.return_value = [
            _echo_row(), _echo_row(bale_entry_id=502, entry_type="REPACK", bale_no=24790,
                                   original_bale_no=24747),
        ]

        response = client.get(f"{BASE}/bale_search?co_id=1&branch_id=4&bale_no=24747&bale_series_id=3")

        assert response.status_code == 200
        rows = response.json()["data"]["rows"]
        assert [r["entry_type"] for r in rows] == ["PROD", "REPACK"]
        binds = session.execute.call_args_list[0].args[1]
        assert binds["bale_no"] == 24747 and binds["bale_series_id"] == 3
        stmt = str(session.execute.call_args_list[0].args[0])
        assert "be.original_bale_no = :bale_no" in stmt

    def test_bale_search_requires_bale_no(self, session):
        response = client.get(f"{BASE}/bale_search?co_id=1&branch_id=4")
        assert response.status_code == 400
        assert "bale_no" in response.json()["detail"]


# =============================================================================
# entry_create
# =============================================================================


def _happy_side_effects(*, repack=False, quality=None, series=None, echo=None):
    """quality -> series -> machine -> trolly -> [original] -> duplicate -> insert -> bump -> echo."""
    effects = [
        _exec_result(fetchone=quality or _quality()),
        _exec_result(fetchone=series or _series()),
        _exec_result(fetchone=_machine()),
        _exec_result(fetchone=_trolly()),
    ]
    if repack:
        effects.append(_exec_result(fetchone=_attr_row(bale_entry_id=400, tran_date=date(2026, 9, 1),
                                                       entry_type="PROD", gross_wt=291, net_wt=274)))
    effects += [
        _exec_result(fetchone=None),           # duplicate probe -> clear
        _exec_result(lastrowid=501),           # insert
        _exec_result(),                        # bump series
        _exec_result(fetchone=echo or _echo_row()),
    ]
    return effects


class TestEntryCreate:
    def test_happy_path(self, session):
        session.execute.side_effect = _happy_side_effects()

        response = client.post(f"{BASE}/entry_create", json=_create_payload())

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"row", "last_bale_no"}
        assert data["row"]["bale_entry_id"] == 501
        assert data["row"]["net_wt"] == 277.0
        assert data["last_bale_no"] == 24747  # F9: max(24746, 24747)

        insert_stmt = str(session.execute.call_args_list[5].args[0])
        assert "INSERT INTO jute_prod_bale_entry" in insert_stmt
        ins = session.execute.call_args_list[5].args[1]
        # F0 + snapshots
        assert ins["gross_wt"] == 294.0
        assert ins["trolly_wt"] == 12.0
        assert ins["packsheet_wt"] == 5.0
        assert ins["net_wt"] == 277.0
        assert ins["std_bale_wt"] == 290.0
        assert ins["entry_type"] == "PROD" and ins["original_bale_no"] is None
        assert ins["co_id"] == 1 and ins["branch_id"] == 4 and ins["spell_id"] == 91
        assert ins["machine_id"] == 210 and ins["trolly_id"] == 61
        assert ins["bale_series_id"] == 3 and ins["bale_no"] == 24747
        assert ins["updated_by"] == 1
        # F9 bump
        bump_stmt = str(session.execute.call_args_list[6].args[0])
        assert "GREATEST(last_bale_no, :bale_no)" in bump_stmt
        bump = session.execute.call_args_list[6].args[1]
        assert bump == {"bale_series_id": 3, "co_id": 1, "bale_no": 24747, "updated_by": 1}
        # F10 sync with the row's key
        session.sync.assert_called_once()
        kw = _sync_kwargs(session.sync)
        assert kw["co_id"] == 1 and kw["branch_id"] == 4 and kw["spell_id"] == 91
        assert kw["machine_id"] == 210 and kw["finishing_quality_id"] == 12
        assert str(kw["tran_date"]) == PAST_DATE and kw["user_id"] == 1
        session.commit.assert_called_once()

    def test_happy_path_without_machine_or_trolly(self, session):
        """Branch without a Bale Press row + tared scale: machine/trolly None, tare 0."""
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=None),      # duplicate
            _exec_result(lastrowid=501),
            _exec_result(),
            _exec_result(fetchone=_echo_row(machine_id=None, trolly_id=None, trolly_wt=0.0, net_wt=289.0)),
        ]

        response = client.post(
            f"{BASE}/entry_create", json=_create_payload(machine_id=None, trolly_id=None)
        )

        assert response.status_code == 200, response.text
        ins = session.execute.call_args_list[3].args[1]
        assert ins["machine_id"] is None and ins["trolly_id"] is None
        assert ins["trolly_wt"] == 0.0 and ins["net_wt"] == 289.0
        assert _sync_kwargs(session.sync)["machine_id"] is None

    def test_repack_happy_path(self, session):
        session.execute.side_effect = _happy_side_effects(
            repack=True,
            echo=_echo_row(entry_type="REPACK", bale_no=24747, original_bale_no=24700),
        )

        response = client.post(
            f"{BASE}/entry_create",
            json=_create_payload(entry_type="REPACK", original_bale_no=24700),
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["row"]["original_bale_no"] == 24700
        orig_stmt = str(session.execute.call_args_list[4].args[0])
        assert "bale_no = :original_bale_no" in orig_stmt
        assert session.execute.call_args_list[4].args[1]["original_bale_no"] == 24700
        ins = session.execute.call_args_list[6].args[1]
        assert ins["entry_type"] == "REPACK" and ins["original_bale_no"] == 24700
        # REPACK still bumps the counter (F9 applies to both types)
        assert session.execute.call_args_list[7].args[1]["bale_no"] == 24747
        session.sync.assert_called_once()

    # ------------------------------------------------------------ 400 branches
    def test_missing_co_id_is_schema_error(self, session):
        payload = _create_payload()
        payload.pop("co_id")
        response = client.post(f"{BASE}/entry_create", json=payload)
        assert response.status_code == 422
        session.execute.assert_not_called()

    def test_future_date(self, session):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(tran_date=FUTURE_DATE))
        assert response.status_code == 400
        assert "future" in response.json()["detail"].lower()
        session.execute.assert_not_called()

    def test_older_than_backdate_window(self, session):
        """Spec §7d: a press date more than 60 days back is refused before any SQL."""
        too_old = str(date.today() - timedelta(days=61))
        response = client.post(f"{BASE}/entry_create", json=_create_payload(tran_date=too_old))
        assert response.status_code == 400
        assert "60 days" in response.json()["detail"]
        session.execute.assert_not_called()

    @pytest.mark.parametrize("gross", [0, 0.5, BALE_GROSS_MAX + 1, -5])
    def test_gross_out_of_range(self, session, gross):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(gross_wt=gross))
        assert response.status_code == 400
        assert "gross_wt" in response.json()["detail"]
        session.execute.assert_not_called()

    @pytest.mark.parametrize("bale_no", [0, -1, BALE_NO_MAX + 1])
    def test_bale_no_out_of_range(self, session, bale_no):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(bale_no=bale_no))
        assert response.status_code == 400
        assert "bale_no" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_bad_entry_type(self, session):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(entry_type="LOOSE"))
        assert response.status_code == 400
        assert "entry_type" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_net_not_positive(self, session):
        """gross 15 − trolley 12 − packsheet 5 = −2 -> 400, nothing inserted."""
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload(gross_wt=15))

        assert response.status_code == 400
        assert "net_wt" in response.json()["detail"]
        assert session.execute.call_count == 4
        session.sync.assert_not_called()
        session.rollback.assert_called()

    def test_type_mismatch(self, session):
        """Hessian series with a sacking quality -> 400 before the spell is resolved."""
        session.execute.side_effect = [
            _exec_result(fetchone=_quality(quality_type=2)),
            _exec_result(fetchone=_series(quality_type=1)),
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload())

        assert response.status_code == 400
        assert "type" in response.json()["detail"].lower()
        assert session.execute.call_count == 2

    def test_series_item_mismatch(self, session):
        """A series bound to item 5002 refuses quality item 5001."""
        session.execute.side_effect = [
            _exec_result(fetchone=_quality(item_id=5001)),
            _exec_result(fetchone=_series(item_id=5002)),
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload())

        assert response.status_code == 400
        assert "item" in response.json()["detail"].lower()
        assert session.execute.call_count == 2

    def test_series_other_branch(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series(branch_id=29)),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(branch_id=4))
        assert response.status_code == 400
        assert "branch" in response.json()["detail"].lower()

    def test_prod_with_original(self, session):
        response = client.post(
            f"{BASE}/entry_create", json=_create_payload(entry_type="PROD", original_bale_no=24700)
        )
        assert response.status_code == 400
        assert "original_bale_no" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_repack_without_original(self, session):
        response = client.post(f"{BASE}/entry_create", json=_create_payload(entry_type="REPACK"))
        assert response.status_code == 400
        assert "original_bale_no" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_repack_original_below_one(self, session):
        response = client.post(
            f"{BASE}/entry_create", json=_create_payload(entry_type="REPACK", original_bale_no=0)
        )
        assert response.status_code == 400
        assert "original_bale_no" in response.json()["detail"]

    def test_unknown_quality(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(finishing_quality_id=999))
        assert response.status_code == 400
        assert "finishing_quality_id" in response.json()["detail"]
        assert session.execute.call_args_list[0].args[1] == {"finishing_quality_id": 999, "co_id": 1}

    def test_unknown_series(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=None),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(bale_series_id=999))
        assert response.status_code == 400
        assert "bale_series_id" in response.json()["detail"]

    def test_unknown_machine(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=None),  # machine guard
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(machine_id=999))
        assert response.status_code == 400
        assert "machine_id" in response.json()["detail"]

    def test_machine_other_branch(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine(branch_id=29)),
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload())
        assert response.status_code == 400
        assert "branch" in response.json()["detail"].lower()

    def test_unknown_trolly(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=None),  # trolly
        ]
        response = client.post(f"{BASE}/entry_create", json=_create_payload(trolly_id=999))
        assert response.status_code == 400
        assert "trolly_id" in response.json()["detail"]

    def test_spell_rejected_by_resolver(self, session, monkeypatch):
        """The real _resolve_spell runs: no status=1 spell row -> its own 400 surfaces."""
        from src.juteProduction.spinning_entry import _resolve_spell as real_resolve
        monkeypatch.setattr(bale_entry, "_resolve_spell", real_resolve)
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=None),  # spell lookup
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload(spell_id=9999))

        assert response.status_code == 400
        assert "spell" in response.json()["detail"].lower()
        assert session.execute.call_count == 3

    # ------------------------------------------------------------ 409 branches
    def test_clash_409(self, session):
        clash = _attr_row(bale_entry_id=444, tran_date=date(2026, 9, 9), spell_id=91,
                          spell_code="A1", entry_type="PROD", gross_wt=291.0, net_wt=274.0)
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=clash),  # duplicate probe hits
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload())

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert "24747" in detail and "PUNJAB" in detail and "#444" in detail and "2026-09-09" in detail
        dup_binds = session.execute.call_args_list[4].args[1]
        assert dup_binds == {"bale_series_id": 3, "bale_no": 24747, "exclude_id": None}
        assert session.execute.call_count == 5
        session.sync.assert_not_called()
        session.commit.assert_not_called()

    def test_clash_bypassed_with_allow_duplicate(self, session):
        """allow_duplicate skips the probe entirely: no duplicate SELECT is issued."""
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(lastrowid=501),   # insert (no probe in between)
            _exec_result(),                # bump
            _exec_result(fetchone=_echo_row()),
        ]

        response = client.post(f"{BASE}/entry_create", json=_create_payload(allow_duplicate=True))

        assert response.status_code == 200, response.text
        assert "INSERT INTO jute_prod_bale_entry" in str(session.execute.call_args_list[4].args[0])
        session.sync.assert_called_once()

    def test_repack_unknown_original_409(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series(series_name="CHHATTISGARH")),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=None),  # original lookup -> none
        ]

        response = client.post(
            f"{BASE}/entry_create", json=_create_payload(entry_type="REPACK", original_bale_no=4456)
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "No bale 4456 found in series CHHATTISGARH to repack"
        assert session.execute.call_count == 5
        session.commit.assert_not_called()

    def test_repack_unknown_original_bypassed(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(lastrowid=501),   # insert — neither probe runs
            _exec_result(),
            _exec_result(fetchone=_echo_row(entry_type="REPACK", original_bale_no=4456)),
        ]

        response = client.post(
            f"{BASE}/entry_create",
            json=_create_payload(entry_type="REPACK", original_bale_no=4456, allow_duplicate=True),
        )

        assert response.status_code == 200, response.text
        ins = session.execute.call_args_list[4].args[1]
        assert ins["entry_type"] == "REPACK" and ins["original_bale_no"] == 4456


# =============================================================================
# entry_edit
# =============================================================================


def _existing(**over):
    base = dict(
        bale_entry_id=501, co_id=1, branch_id=4, tran_date=date.fromisoformat(PAST_DATE), spell_id=91,
        machine_id=210, entry_type="PROD", finishing_quality_id=12, bale_series_id=3,
        bale_no=24747, original_bale_no=None, trolly_id=61, trolly_wt=12.0, packsheet_wt=5.0,
        gross_wt=294.0, net_wt=277.0, std_bale_wt=290.0,
        weigh_time=datetime(2026, 9, 10, 9, 15, 30), remarks=None,
    )
    base.update(over)
    return _attr_row(**base)


class TestEntryEdit:
    def _edit_effects(self, existing, quality=None, series=None, echo=None):
        return [
            _exec_result(fetchone=existing),
            _exec_result(fetchone=quality or _quality()),
            _exec_result(fetchone=series or _series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=None),  # duplicate (excluding self)
            _exec_result(),               # update
            _exec_result(),               # bump
            _exec_result(fetchone=echo or _echo_row()),
        ]

    def test_edit_moves_key_syncs_old_and_new(self, session):
        """Spell 91 -> 92 changes the F10 key: sync runs for the NEW key, then the OLD."""
        session.execute.side_effect = self._edit_effects(
            _existing(spell_id=91), echo=_echo_row(spell_id=92, spell_code="A2")
        )

        response = client.put(
            f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(spell_id=92, gross_wt=300.0)
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["row"]["spell_id"] == 92 and data["last_bale_no"] == 24747
        upd_stmt = str(session.execute.call_args_list[6].args[0])
        assert "UPDATE jute_prod_bale_entry" in upd_stmt and "co_id = :co_id" in upd_stmt
        upd = session.execute.call_args_list[6].args[1]
        assert upd["id"] == 501 and upd["co_id"] == 1
        assert upd["spell_id"] == 92 and upd["gross_wt"] == 300.0 and upd["net_wt"] == 283.0
        # clash probe excludes the row itself
        assert session.execute.call_args_list[5].args[1]["exclude_id"] == 501
        assert session.sync.call_count == 2
        new_kw, old_kw = _sync_kwargs(session.sync, 0), _sync_kwargs(session.sync, 1)
        assert new_kw["spell_id"] == 92 and old_kw["spell_id"] == 91
        assert new_kw["finishing_quality_id"] == old_kw["finishing_quality_id"] == 12
        session.commit.assert_called_once()

    def test_edit_same_key_syncs_once(self, session):
        session.execute.side_effect = self._edit_effects(_existing())

        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(gross_wt=296.0))

        assert response.status_code == 200, response.text
        session.sync.assert_called_once()

    def test_edit_moves_machine_and_quality_syncs_both(self, session):
        session.execute.side_effect = self._edit_effects(
            _existing(machine_id=None, finishing_quality_id=13),
            echo=_echo_row(),
        )

        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload())

        assert response.status_code == 200, response.text
        assert session.sync.call_count == 2
        assert _sync_kwargs(session.sync, 0)["machine_id"] == 210
        assert _sync_kwargs(session.sync, 1)["machine_id"] is None
        assert _sync_kwargs(session.sync, 1)["finishing_quality_id"] == 13

    def test_edit_renumber_rewinds_old_top(self, session):
        """Mistyped 24900 corrected to 24747: bump (no-op) then rewind the old number;
        the echoed counter is the post-rewind value."""
        effects = self._edit_effects(_existing(bale_no=24900))
        effects[-1:-1] = [
            _exec_result(),                                        # rewind old number
            _exec_result(fetchone=_attr_row(last_bale_no=24747)),  # counter echo
        ]
        session.execute.side_effect = effects

        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload())

        assert response.status_code == 200, response.text
        assert response.json()["data"]["last_bale_no"] == 24747
        rewind = session.execute.call_args_list[8]
        assert "opening_bale_no" in str(rewind.args[0])
        assert rewind.args[1] == {"bale_series_id": 3, "co_id": 1, "bale_no": 24900, "updated_by": 1}

    def test_edit_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(f"{BASE}/entry_edit/999?co_id=1", json=_create_payload())
        assert response.status_code == 404
        lookup = session.execute.call_args_list[0]
        assert "co_id = :co_id" in str(lookup.args[0]) and lookup.args[1]["co_id"] == 1
        session.sync.assert_not_called()

    def test_edit_requires_co_id(self, session):
        response = client.put(f"{BASE}/entry_edit/501", json=_create_payload())
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()
        session.execute.assert_not_called()

    def test_edit_clash_409_names_other_row(self, session):
        clash = _attr_row(bale_entry_id=444, tran_date=date(2026, 9, 9), spell_id=91,
                          spell_code="A1", entry_type="PROD", gross_wt=291.0, net_wt=274.0)
        session.execute.side_effect = [
            _exec_result(fetchone=_existing()),
            _exec_result(fetchone=_quality()),
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_trolly()),
            _exec_result(fetchone=clash),
        ]
        response = client.put(f"{BASE}/entry_edit/501?co_id=1", json=_create_payload(bale_no=24700))
        assert response.status_code == 409
        assert "#444" in response.json()["detail"]
        session.sync.assert_not_called()


# =============================================================================
# entry_delete
# =============================================================================


class TestEntryDelete:
    def test_soft_delete_binds_co_id_and_syncs(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_existing()),
            _exec_result(),  # soft delete
            _exec_result(),  # rewind counter
            _exec_result(fetchone=_attr_row(last_bale_no=24746)),  # counter echo
        ]

        response = client.delete(f"{BASE}/entry_delete/501?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"] == {
            "bale_entry_id": 501, "bale_series_id": 3, "last_bale_no": 24746,
        }
        stmt = str(session.execute.call_args_list[1].args[0])
        assert "active = 0" in stmt and "co_id = :co_id" in stmt
        assert session.execute.call_args_list[1].args[1] == {"id": 501, "co_id": 1, "updated_by": 1}
        # counter rewind: only when this bale was the top, floored at opening_bale_no
        rewind_stmt = str(session.execute.call_args_list[2].args[0])
        assert "opening_bale_no" in rewind_stmt and "bs.last_bale_no = :bale_no" in rewind_stmt
        assert session.execute.call_args_list[2].args[1] == {
            "bale_series_id": 3, "co_id": 1, "bale_no": 24747, "updated_by": 1,
        }
        session.sync.assert_called_once()
        kw = _sync_kwargs(session.sync)
        assert kw["spell_id"] == 91 and kw["machine_id"] == 210 and kw["finishing_quality_id"] == 12
        assert str(kw["tran_date"]) == PAST_DATE
        session.commit.assert_called_once()

    def test_delete_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.delete(f"{BASE}/entry_delete/999?co_id=99")
        assert response.status_code == 404
        assert session.execute.call_args_list[0].args[1]["co_id"] == 99
        session.sync.assert_not_called()

    def test_delete_requires_co_id(self, session):
        response = client.delete(f"{BASE}/entry_delete/501")
        assert response.status_code == 400
        session.execute.assert_not_called()


# =============================================================================
# Series master
# =============================================================================


def _series_payload(**over):
    payload = {
        "co_id": 1, "branch_id": 4, "series_name": "PUNJAB", "quality_type": 2,
        "item_id": None, "state_id": 14, "party_id": None, "last_bale_no": 24746,
    }
    payload.update(over)
    return payload


class TestSeriesMaster:
    def test_series_setup(self, session):
        session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row({"item_id": 5001, "item_code": "2124",
                                              "item_name": "Govt-BT-WILL", "full_item_code": "x"})]),
            _exec_result(fetchall=[_mock_row({"state_id": 14, "state": "Punjab", "state_code": "PB"})]),
            _exec_result(fetchall=[_mock_row({"party_id": 88, "supp_name": "FCI"})]),
        ]

        response = client.get(f"{BASE}/series_setup?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"] == {
            "items": [{"item_id": 5001, "item_code": "2124", "item_name": "Govt-BT-WILL"}],
            "states": [{"state_id": 14, "state": "Punjab"}],
            "parties": [{"party_id": 88, "supp_name": "FCI"}],
        }
        item_binds = session.execute.call_args_list[0].args[1]
        assert item_binds["item_type_id"] == FINISHING_ITEM_TYPE_ID and item_binds["co_id"] == 1
        party_stmt = str(session.execute.call_args_list[2].args[0])
        assert "co_id = :co_id" in party_stmt and "active = 1" in party_stmt

    def test_series_list(self, session):
        session.execute.return_value.fetchall.return_value = [
            _mock_row({
                "bale_series_id": 3, "co_id": 1, "branch_id": 4, "branch_name": "Factory",
                "series_name": "PUNJAB", "quality_type": 2, "item_id": None, "item_name": None,
                "state_id": 14, "state": "Punjab", "party_id": None, "party_name": None,
                "last_bale_no": 24746, "bales_total": 1204,
            })
        ]

        response = client.get(f"{BASE}/series_list?co_id=1&branch_id=4")

        assert response.status_code == 200
        rows = response.json()["data"]["rows"]
        assert rows[0]["series_name"] == "PUNJAB" and rows[0]["bales_total"] == 1204
        assert rows[0]["branch_name"] == "Factory" and rows[0]["state"] == "Punjab"

    def test_series_list_missing_co_id(self, session):
        response = client.get(f"{BASE}/series_list")
        assert response.status_code == 400

    def test_series_create_success(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=None),      # duplicate
            _exec_result(lastrowid=3),        # insert
        ]

        response = client.post(f"{BASE}/series_create", json=_series_payload())

        assert response.status_code == 200
        assert response.json()["data"] == {"bale_series_id": 3}
        ins = session.execute.call_args_list[1].args[1]
        assert ins["co_id"] == 1 and ins["branch_id"] == 4 and ins["series_name"] == "PUNJAB"
        assert ins["quality_type"] == 2 and ins["item_id"] is None and ins["state_id"] == 14
        assert ins["last_bale_no"] == 24746 and ins["updated_by"] == 1
        session.commit.assert_called_once()

    def test_series_create_bound_item_verified(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(item_id=5001)),  # item verify
            _exec_result(fetchone=None),                     # duplicate
            _exec_result(lastrowid=4),
        ]

        response = client.post(
            f"{BASE}/series_create",
            json=_series_payload(series_name='39"- 5.85', quality_type=1, item_id=5001, last_bale_no=0),
        )

        assert response.status_code == 200
        verify = session.execute.call_args_list[0].args[1]
        assert verify == {"item_id": 5001, "co_id": 1, "item_type_id": FINISHING_ITEM_TYPE_ID}

    def test_series_create_bad_item(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(f"{BASE}/series_create", json=_series_payload(item_id=999))
        assert response.status_code == 400
        assert "item_id" in response.json()["detail"]

    def test_series_create_blank_name(self, session):
        response = client.post(f"{BASE}/series_create", json=_series_payload(series_name="   "))
        assert response.status_code == 400
        assert "series_name" in response.json()["detail"]
        session.execute.assert_not_called()

    def test_series_create_bad_type(self, session):
        response = client.post(f"{BASE}/series_create", json=_series_payload(quality_type=3))
        assert response.status_code == 400
        assert "quality_type" in response.json()["detail"]

    def test_series_create_negative_last_no(self, session):
        response = client.post(f"{BASE}/series_create", json=_series_payload(last_bale_no=-1))
        assert response.status_code == 400
        assert "last_bale_no" in response.json()["detail"]

    def test_series_create_duplicate(self, session):
        session.execute.side_effect = [_exec_result(fetchone=_attr_row(bale_series_id=3))]
        response = client.post(f"{BASE}/series_create", json=_series_payload())
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]
        dup = session.execute.call_args_list[0].args[1]
        assert dup == {"co_id": 1, "branch_id": 4, "series_name": "PUNJAB", "exclude_id": None}

    def test_series_create_requires_co_id(self, session):
        response = client.post(f"{BASE}/series_create", json=_series_payload(co_id=None))
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"]

    def test_series_edit_reset_counter(self, session):
        """Editing last_bale_no IS the manual reset (D3); the dup probe excludes self."""
        session.execute.side_effect = [
            _exec_result(fetchone=_series()),   # existing (co-scoped)
            _exec_result(fetchone=None),        # duplicate
            _exec_result(),                     # update
        ]

        response = client.put(f"{BASE}/series_edit/3?co_id=1", json=_series_payload(last_bale_no=0))

        assert response.status_code == 200
        assert response.json()["data"] == {"bale_series_id": 3}
        assert session.execute.call_args_list[1].args[1]["exclude_id"] == 3
        upd_stmt = str(session.execute.call_args_list[2].args[0])
        assert "UPDATE jute_prod_bale_series" in upd_stmt and "co_id = :co_id" in upd_stmt
        upd = session.execute.call_args_list[2].args[1]
        assert upd["last_bale_no"] == 0 and upd["bale_series_id"] == 3 and upd["co_id"] == 1

    def test_series_edit_type_locked_when_referenced(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_series(quality_type=2)),
            _exec_result(fetchone=None),              # duplicate
            _exec_result(fetchone=_attr_row(cnt=12)),  # active bales referencing
        ]

        response = client.put(f"{BASE}/series_edit/3?co_id=1", json=_series_payload(quality_type=1))

        assert response.status_code == 400
        assert "quality_type" in response.json()["detail"] and "12" in response.json()["detail"]
        assert session.execute.call_count == 3

    def test_series_edit_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(f"{BASE}/series_edit/3?co_id=99", json=_series_payload())
        assert response.status_code == 404
        assert session.execute.call_args_list[0].args[1]["co_id"] == 99

    def test_series_delete_success(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_attr_row(cnt=0)),
            _exec_result(),
        ]

        response = client.delete(f"{BASE}/series_delete/3?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"] == {"bale_series_id": 3}
        stmt = str(session.execute.call_args_list[-1].args[0])
        assert "active = 0" in stmt and "co_id = :co_id" in stmt
        assert session.execute.call_args_list[-1].args[1] == {
            "bale_series_id": 3, "co_id": 1, "updated_by": 1,
        }

    def test_series_delete_blocked_when_referenced(self, session):
        session.execute.side_effect = [
            _exec_result(fetchone=_series()),
            _exec_result(fetchone=_attr_row(cnt=1204)),
        ]

        response = client.delete(f"{BASE}/series_delete/3?co_id=1")

        assert response.status_code == 400
        assert "1204" in response.json()["detail"]
        assert session.execute.call_count == 2
        session.commit.assert_not_called()

    def test_series_delete_not_found_404(self, session):
        session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.delete(f"{BASE}/series_delete/999?co_id=1")
        assert response.status_code == 404
