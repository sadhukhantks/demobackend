"""Tests for the R-08-15 Yarn QR & CV % SQC endpoints.

Covers src/juteSQC/spinning_sqc.py — the QR/CV surface added on top of the
R-08-16 count data (prefix /api/juteSQC):
  * GET  /sqc_qr_cv_setup       - dropdowns + per-yarn R-08-16 averages + groups
  * POST /sqc_qr_cv_save        - insert-only header + 30 detail rows per entry
  * GET  /sqc_qr_cv_by_date     - saved groups + observed_count/mr_pct + stats
  * DELETE /sqc_qr_cv_delete/{id} - soft-delete the header

Portal persona: DB + auth are mocked via FastAPI dependency overrides, mirroring
test_spinning_sqc.py. The MagicMock session returns rows whose ``._mapping`` is a
plain dict, matching how the endpoints read results.

QR/CV stores raw readings only — observed_count / mr_pct are NOT written by the
save path; they are read from R-08-16's already-saved values at read time and the
QR%/CV%/std-dev stats are computed server-side (statistics.stdev, sample n-1).
"""

import statistics

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteSQC.spinning_sqc import _qr_cv_stats

client = TestClient(app)

ENTRY_DATE = "2026-06-13"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _machine_row():
    return _mock_row(
        {
            "machine_id": 1,
            "machine_name": "Spin-1",
            "mech_code": "SP1",
            "branch_id": 1,
            "no_of_spindle": 200,
        }
    )


def _quality_row():
    return _mock_row(
        {
            "item_id": 1,
            "item_code": "Q1",
            "item_name": "Quality One",
            "std_count": 10.0,
            "std_mr_pct": 13.75,
        }
    )


def _spell_row():
    return _mock_row(
        {
            "spell_id": 2,
            "spell_code": "A",
            "spell_name": "Spell A",
            "working_hours": 8.0,
        }
    )


def _yarn_obs_row(observed_count=18.5, mr_pct=12.0, obs_count=2, spell_id=2, mc_id=1):
    return _mock_row(
        {
            "spell_id": spell_id,
            "mc_id": mc_id,
            "item_id": 1,
            "item_code": "Q1",
            "item_name": "Quality One",
            "observed_count": observed_count,
            "mr_pct": mr_pct,
            "obs_count": obs_count,
        }
    )


def _qr_cv_header_row(spell_id=2):
    return _mock_row(
        {
            "spinning_sqc_qr_cv_id": 70,
            "co_id": 1,
            "branch_id": 1,
            "entry_date": "2026-06-13",
            "spell_id": spell_id,
            "spell_code": "A" if spell_id is not None else None,
            "mc_id": 1,
            "mech_code": "SP1",
            "machine_name": "Spin-1",
            "item_id": 1,
            "item_code": "Q1",
            "item_name": "Quality One",
        }
    )


# A known 30-reading fixture: 6 spindles x 5 readings. The assertions below derive
# every expected stat from this same list so the test stays self-consistent.
_FIXTURE_VALS = [
    2.0, 4.0, 4.0, 4.0, 5.0,
    5.0, 7.0, 9.0, 6.0, 6.0,
    3.0, 8.0, 7.0, 5.0, 4.0,
    6.0, 6.0, 7.0, 5.0, 4.0,
    8.0, 9.0, 3.0, 2.0, 5.0,
    6.0, 7.0, 4.0, 5.0, 6.0,
]


def _qr_cv_dtl_rows(hdr_id=70, vals=None):
    vals = _FIXTURE_VALS if vals is None else vals
    rows = []
    for i, v in enumerate(vals):
        spindle_no = 101 + (i // 5)        # 6 spindles
        reading_no = (i % 5) + 1           # 1..5
        rows.append(
            _mock_row(
                {
                    "spinning_sqc_qr_cv_id": hdr_id,
                    "spindle_no": spindle_no,
                    "reading_no": reading_no,
                    "reading_val": v,
                }
            )
        )
    return rows


class TestSqcQrCvSetup:
    """GET /sqc_qr_cv_setup."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_sqc_qr_cv_setup_success(self):
        # _fetch_machines, _fetch_qualities, _fetch_spells, _fetch_yarn_obs, then
        # _qr_cv_groups (headers query). Headers empty -> groups builder returns []
        # without issuing the detail / obs-map queries, so 5 fetchall calls.
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_machine_row()],
            [_quality_row()],
            [_spell_row()],
            [_yarn_obs_row()],
            [],  # no existing QR/CV headers for the date
        ]

        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_setup?co_id=1&entry_date={ENTRY_DATE}"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert "machines" in data
        assert "yarn_items" in data
        assert "spells" in data
        assert "yarn_obs" in data
        assert "groups" in data
        assert data["machines"][0]["machine_id"] == 1
        assert data["yarn_items"][0]["item_id"] == 1
        assert data["spells"][0]["spell_id"] == 2
        # yarn_obs rows are keyed by (spell, machine, yarn) — no date-level AVG.
        assert data["yarn_obs"][0]["item_id"] == 1
        assert data["yarn_obs"][0]["spell_id"] == 2
        assert data["yarn_obs"][0]["mc_id"] == 1
        assert data["yarn_obs"][0]["observed_count"] == 18.5
        assert data["yarn_obs"][0]["obs_count"] == 2
        assert data["groups"] == []

    def test_sqc_qr_cv_setup_missing_co_id(self):
        response = client.get(f"/api/juteSQC/sqc_qr_cv_setup?entry_date={ENTRY_DATE}")
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    def test_sqc_qr_cv_setup_missing_entry_date(self):
        response = client.get("/api/juteSQC/sqc_qr_cv_setup?co_id=1")
        assert response.status_code == 400
        assert "entry_date" in response.json().get("detail", "").lower()

    def test_sqc_qr_cv_setup_db_error(self):
        self._mock_session.execute.side_effect = Exception("DB connection failed")
        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_setup?co_id=1&entry_date={ENTRY_DATE}"
        )
        assert response.status_code == 500


class TestSqcQrCvSave:
    """POST /sqc_qr_cv_save."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @staticmethod
    def _payload_one_group():
        return {
            "co_id": 1,
            "branch_id": 1,
            "entry_date": ENTRY_DATE,
            "entries": [
                {
                    "spell_id": 2,
                    "mc_id": 1,
                    "item_id": 1,
                    "spindles": [
                        {"spindle_no": 101 + s, "readings": [1.0, 2.0, 3.0, 4.0, 5.0]}
                        for s in range(6)
                    ],
                }
            ],
        }

    def test_sqc_qr_cv_save_inserts_header_and_30_detail_rows(self):
        # lastrowid on the header insert -> used as hdr_id for the detail rows.
        self._mock_session.execute.return_value.lastrowid = 70

        response = client.post(
            "/api/juteSQC/sqc_qr_cv_save", json=self._payload_one_group()
        )

        assert response.status_code == 200
        body = response.json()["data"]
        assert body["saved"] == 1
        assert body["ids"] == [70]
        self._mock_session.commit.assert_called_once()

        # Partition the execute calls into header inserts vs detail inserts by the
        # bind-param shape. Exactly 1 header + 30 detail rows = 31 execute calls.
        header_calls = []
        detail_calls = []
        for call in self._mock_session.execute.call_args_list:
            params = call.args[1] if len(call.args) > 1 else None
            if not isinstance(params, dict):
                continue
            if "item_id" in params and "entry_date" in params:
                header_calls.append(params)
            elif "hdr_id" in params and "reading_no" in params:
                detail_calls.append(params)

        assert len(header_calls) == 1
        assert len(detail_calls) == 30

        # Header carries NO observed_count / mr_pct columns (QR/CV doesn't store them).
        hp = header_calls[0]
        assert "observed_count" not in hp
        assert "mr_pct" not in hp
        assert hp["co_id"] == 1
        assert hp["item_id"] == 1
        assert hp["mc_id"] == 1
        assert hp["spell_id"] == 2

        # Detail rows: 6 distinct spindles, reading_no 1..5, hdr_id wired from lastrowid.
        assert all(d["hdr_id"] == 70 for d in detail_calls)
        assert sorted({d["spindle_no"] for d in detail_calls}) == [101, 102, 103, 104, 105, 106]
        assert sorted({d["reading_no"] for d in detail_calls}) == [1, 2, 3, 4, 5]
        # No observed_count / mr_pct on detail rows either.
        assert all("observed_count" not in d and "mr_pct" not in d for d in detail_calls)

    def test_sqc_qr_cv_save_empty_entries(self):
        payload = {"co_id": 1, "entry_date": ENTRY_DATE, "entries": []}
        response = client.post("/api/juteSQC/sqc_qr_cv_save", json=payload)
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["saved"] == 0
        assert body["ids"] == []

    def test_sqc_qr_cv_save_missing_co_id_in_body(self):
        # co_id is required on SqcQrCvSave -> pydantic 422
        payload = {"entry_date": ENTRY_DATE, "entries": []}
        response = client.post("/api/juteSQC/sqc_qr_cv_save", json=payload)
        assert response.status_code == 422

    def test_sqc_qr_cv_save_db_error_rolls_back(self):
        self._mock_session.execute.side_effect = Exception("insert failed")
        response = client.post(
            "/api/juteSQC/sqc_qr_cv_save", json=self._payload_one_group()
        )
        assert response.status_code == 500
        self._mock_session.rollback.assert_called_once()


class TestSqcQrCvByDate:
    """GET /sqc_qr_cv_by_date — groups with observed_count/mr_pct + computed stats."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_sqc_qr_cv_by_date_returns_groups_with_obs_and_stats(self):
        observed_count = 18.5
        # _qr_cv_groups issues, in order: headers, detail rows, then the obs-map
        # (yarn_obs) query.
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_qr_cv_header_row()],
            _qr_cv_dtl_rows(hdr_id=70),
            [_yarn_obs_row(observed_count=observed_count, mr_pct=12.0)],
        ]

        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_by_date?co_id=1&entry_date={ENTRY_DATE}"
        )

        assert response.status_code == 200
        groups = response.json()["data"]["groups"]
        assert len(groups) == 1
        g = groups[0]

        assert g["spinning_sqc_qr_cv_id"] == 70
        assert g["item_id"] == 1
        assert g["spell_id"] == 2
        assert g["spell_code"] == "A"
        # observed_count / mr_pct came from the R-08-16 row matching the group's
        # exact (spell, machine, yarn) key.
        assert g["observed_count"] == observed_count
        assert g["mr_pct"] == 12.0
        assert len(g["readings"]) == 30

        stats = g["stats"]
        # Derive the expected values from the same fixture (population std dev,
        # CV% = SD / mean × 100 — the lab's Excel method).
        vals = _FIXTURE_VALS
        n = len(vals)
        avg_bs = sum(vals) / n
        std_dev = statistics.pstdev(vals)           # population (÷n)
        qr_pct = (avg_bs / observed_count) * 100
        cv_pct = (std_dev / avg_bs) * 100

        assert stats["n"] == n
        assert stats["max"] == round(max(vals), 2)
        assert stats["min"] == round(min(vals), 2)
        assert stats["avg_bs"] == round(avg_bs, 2)
        assert stats["std_dev"] == round(std_dev, 2)
        assert stats["qr_pct"] == round(qr_pct, 2)
        assert stats["cv_pct"] == round(cv_pct, 2)

    def test_sqc_qr_cv_by_date_no_matching_key_blank_obs(self):
        # Legacy group (spell_id NULL) or no R-08-16 row for the exact
        # (spell, machine, yarn) key -> observed_count/mr_pct blank, QR% null;
        # CV% (= SD / avg_bs) does not need observed_count, so it is still computed.
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_qr_cv_header_row(spell_id=None)],
            _qr_cv_dtl_rows(hdr_id=70),
            [_yarn_obs_row(spell_id=2, mc_id=1)],  # keyed row can't match NULL spell
        ]

        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_by_date?co_id=1&entry_date={ENTRY_DATE}"
        )

        assert response.status_code == 200
        g = response.json()["data"]["groups"][0]
        assert g["observed_count"] is None
        assert g["mr_pct"] is None
        assert g["stats"]["qr_pct"] is None
        # Readings-only stats still computed.
        assert g["stats"]["avg_bs"] is not None
        assert g["stats"]["cv_pct"] is not None

    def test_sqc_qr_cv_by_date_empty(self):
        # No headers -> groups builder short-circuits (single fetchall call).
        self._mock_session.execute.return_value.fetchall.side_effect = [[]]
        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_by_date?co_id=1&entry_date={ENTRY_DATE}"
        )
        assert response.status_code == 200
        assert response.json()["data"]["groups"] == []

    def test_sqc_qr_cv_by_date_missing_co_id(self):
        response = client.get(f"/api/juteSQC/sqc_qr_cv_by_date?entry_date={ENTRY_DATE}")
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    def test_sqc_qr_cv_by_date_divide_by_zero_guard(self):
        # observed_count = 0 -> qr_pct must be null; cv_pct (SD/mean) is unaffected.
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_qr_cv_header_row()],
            _qr_cv_dtl_rows(hdr_id=70),
            [_yarn_obs_row(observed_count=0.0, mr_pct=12.0)],
        ]

        response = client.get(
            f"/api/juteSQC/sqc_qr_cv_by_date?co_id=1&entry_date={ENTRY_DATE}"
        )

        assert response.status_code == 200
        g = response.json()["data"]["groups"][0]
        assert g["observed_count"] == 0.0
        stats = g["stats"]
        assert stats["qr_pct"] is None
        # avg_bs / std_dev / cv_pct are still computable from the readings.
        assert stats["avg_bs"] is not None
        assert stats["std_dev"] is not None
        assert stats["cv_pct"] == round(
            statistics.pstdev(_FIXTURE_VALS) / (sum(_FIXTURE_VALS) / len(_FIXTURE_VALS)) * 100, 2
        )


class TestSqcQrCvStatsHelper:
    """Pure formula checks for the server-side QR/CV stats (statistics.pstdev,
    population ÷n; QR% = avg_bs/observed_count*100; CV% = std_dev/avg_bs*100 —
    matches the lab's Excel sheet: SD = SQRT(SUM((x-mean)^2)/n), CV = SD/mean*100)."""

    @staticmethod
    def _readings(vals):
        return [{"reading_val": v} for v in vals]

    def test_stats_known_fixture(self):
        observed_count = 18.5
        vals = _FIXTURE_VALS
        stats = _qr_cv_stats(self._readings(vals), observed_count)

        n = len(vals)
        avg_bs = sum(vals) / n
        std_dev = statistics.pstdev(vals)
        qr_pct = (avg_bs / observed_count) * 100
        cv_pct = (std_dev / avg_bs) * 100

        assert stats["n"] == n
        assert stats["max"] == round(max(vals), 2)
        assert stats["min"] == round(min(vals), 2)
        assert stats["avg_bs"] == round(avg_bs, 2)
        assert stats["std_dev"] == round(std_dev, 2)
        assert stats["qr_pct"] == round(qr_pct, 2)
        assert stats["cv_pct"] == round(cv_pct, 2)

    def test_stats_population_std_dev(self):
        # Two readings -> population std dev = |a-b| / 2.
        stats = _qr_cv_stats(self._readings([4.0, 8.0]), observed_count=10.0)
        assert stats["std_dev"] == round(statistics.pstdev([4.0, 8.0]), 2) == 2.0
        # CV% = SD / mean × 100 = 2 / 6 × 100
        assert stats["cv_pct"] == round(2.0 / 6.0 * 100, 2)
        # Single reading -> std dev defined as 0.0 (not an error).
        stats1 = _qr_cv_stats(self._readings([5.0]), observed_count=10.0)
        assert stats1["std_dev"] == 0.0
        assert stats1["n"] == 1

    def test_stats_observed_count_zero_guards_qr_only(self):
        stats = _qr_cv_stats(self._readings(_FIXTURE_VALS), observed_count=0)
        assert stats["qr_pct"] is None
        assert stats["cv_pct"] is not None  # SD/mean does not need observed_count

    def test_stats_observed_count_none_guards_qr_only(self):
        stats = _qr_cv_stats(self._readings(_FIXTURE_VALS), observed_count=None)
        assert stats["qr_pct"] is None
        assert stats["cv_pct"] is not None

    def test_stats_lab_excel_example(self):
        # e:\ejmhands\cvp.xlsx — 30 readings, MV 10.86, SD 1.6159, CV 14.88.
        vals = [11.4, 9.8, 12.2, 10.4, 11, 10.2, 10.4, 10.2, 9.6, 11,
                12, 10.4, 10.4, 16.2, 10, 10.2, 9.4, 9.6, 12.4, 10.2,
                11.4, 9.8, 16.2, 10.4, 10.4, 9.8, 10.2, 9.6, 11, 10]
        stats = _qr_cv_stats(self._readings(vals), observed_count=10.0)
        assert stats["avg_bs"] == 10.86
        assert stats["std_dev"] == 1.62
        assert stats["cv_pct"] == 14.88

    def test_stats_no_readings(self):
        stats = _qr_cv_stats([], observed_count=18.5)
        assert stats["n"] == 0
        assert stats["avg_bs"] is None
        assert stats["max"] is None
        assert stats["min"] is None
        assert stats["std_dev"] is None
        assert stats["qr_pct"] is None
        assert stats["cv_pct"] is None


class TestSqcQrCvDelete:
    """DELETE /sqc_qr_cv_delete/{qr_cv_id} — soft-delete the header."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_sqc_qr_cv_delete_success(self):
        existing = MagicMock()
        existing.spinning_sqc_qr_cv_id = 70
        self._mock_session.execute.return_value.fetchone.return_value = existing

        response = client.delete("/api/juteSQC/sqc_qr_cv_delete/70")

        assert response.status_code == 200
        assert response.json()["data"]["message"] == "Deleted"
        self._mock_session.commit.assert_called_once()

    def test_sqc_qr_cv_delete_not_found(self):
        self._mock_session.execute.return_value.fetchone.return_value = None

        response = client.delete("/api/juteSQC/sqc_qr_cv_delete/999")

        assert response.status_code == 404
        assert "not found" in response.json().get("detail", "").lower()

    def test_sqc_qr_cv_delete_db_error_rolls_back(self):
        self._mock_session.execute.side_effect = Exception("delete failed")
        response = client.delete("/api/juteSQC/sqc_qr_cv_delete/70")
        assert response.status_code == 500
        self._mock_session.rollback.assert_called_once()
