"""Tests for the Overhaul register — Jute Mechanical & Maintenance.

Covers ``src/juteMechanical/overhaul.py`` (prefix ``/api/mechOverhaul``).

The bulk of the value here is ``_add_months`` — the calendar-clamping helper that
drives ``next_overhaul_due``. It is the subtlest code in the module: adding a
month to 31 Jan cannot produce 31 Feb, so the day is clamped to the last valid day
of the target month, and the clamp must respect leap years. It is a pure function,
so it is tested directly with no DB and no mocking.

The remaining derived fields (``downtime_days``, ``spare_cost``, ``total_cost``)
and the vendor-party gate are tested the same way where they are pure, and through
``app.dependency_overrides`` where a session is involved — the handlers take the
session via ``Depends(get_tenant_db)``, which FastAPI binds at registration time,
so a module-attribute patch would not intercept it.

Never touches a real database.
"""

from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteMechanical import overhaul as oh

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


def _machine_row(**over):
    mapping = {
        "machine_id": 10,
        "machine_name": "SPR-1",
        "mech_code": "S1",
        "machine_type_id": 3,
        "dept_id": 50,
        "branch_id": 1,
    }
    mapping.update(over)
    return _mock_row(mapping)


# =============================================================================
# 1. _add_months — pure calendar clamping
# =============================================================================


class TestAddMonths:
    """Day-of-month overflow must clamp to the target month's last day."""

    def test_plain_month_add_keeps_the_day(self):
        assert oh._add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)

    def test_jan_31_plus_1_month_clamps_to_feb_28(self):
        assert oh._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_jan_31_plus_1_month_clamps_to_feb_29_in_a_leap_year(self):
        assert oh._add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)

    def test_jan_30_plus_1_month_also_clamps(self):
        assert oh._add_months(date(2026, 1, 30), 1) == date(2026, 2, 28)

    def test_mar_31_plus_1_month_clamps_to_apr_30(self):
        assert oh._add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)

    def test_may_31_plus_1_month_clamps_to_jun_30(self):
        assert oh._add_months(date(2026, 5, 31), 1) == date(2026, 6, 30)

    def test_aug_31_plus_1_month_clamps_to_sep_30(self):
        assert oh._add_months(date(2026, 8, 31), 1) == date(2026, 9, 30)

    def test_jul_31_plus_1_month_stays_31_aug(self):
        """Two consecutive 31-day months — no clamp should happen."""
        assert oh._add_months(date(2026, 7, 31), 1) == date(2026, 8, 31)

    def test_plus_12_months_is_the_same_date_next_year(self):
        assert oh._add_months(date(2026, 6, 15), 12) == date(2027, 6, 15)

    def test_plus_12_months_from_feb_29_clamps_to_feb_28(self):
        assert oh._add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)

    def test_plus_18_months_crosses_the_year_boundary(self):
        assert oh._add_months(date(2026, 6, 15), 18) == date(2027, 12, 15)

    def test_plus_18_months_from_a_31st_clamps_in_the_target_month(self):
        # Aug 2026 + 18 = Feb 2028 (a leap year) -> 29th, not 31st.
        assert oh._add_months(date(2026, 8, 31), 18) == date(2028, 2, 29)

    def test_december_rollover(self):
        assert oh._add_months(date(2026, 12, 31), 1) == date(2027, 1, 31)

    def test_december_rollover_with_clamp(self):
        assert oh._add_months(date(2026, 12, 31), 2) == date(2027, 2, 28)

    def test_zero_months_is_identity(self):
        assert oh._add_months(date(2026, 3, 31), 0) == date(2026, 3, 31)

    def test_negative_months_go_backwards_and_clamp(self):
        assert oh._add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)

    @pytest.mark.parametrize("months", [1, 3, 6, 12, 18, 24])
    def test_result_is_always_a_valid_date_from_every_31st(self, months):
        """No combination of a 31st and an interval may produce an invalid date."""
        for month in (1, 3, 5, 7, 8, 10, 12):
            result = oh._add_months(date(2026, month, 31), months)
            assert isinstance(result, date)


# =============================================================================
# 2. Pure cost / date derivation
# =============================================================================


class TestSpareAmount:
    def test_typed_amount_wins(self):
        line = oh.OverhaulSpareLine(item_id=1, qty=2, rate=10, amount=15)
        assert oh._spare_amount(line) == 15.0

    def test_derived_from_qty_times_rate(self):
        line = oh.OverhaulSpareLine(item_id=1, qty=2.5, rate=10)
        assert oh._spare_amount(line) == 25.0

    def test_zero_when_nothing_resolvable(self):
        line = oh.OverhaulSpareLine(item_id=1)
        assert oh._spare_amount(line) == 0.0


class TestDeriveCosts:
    def _body(self, **over):
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "from_date": date(2026, 6, 1),
        }
        payload.update(over)
        return oh.OverhaulCreate(**payload)

    def test_downtime_days_is_inclusive(self):
        """1 Jun -> 3 Jun is 3 days, not 2 (spec: (to - from).days + 1)."""
        costs = self._body(to_date=date(2026, 6, 3))
        assert oh._derive_costs(costs)["downtime_days"] == 3

    def test_same_day_overhaul_is_one_day(self):
        costs = self._body(to_date=date(2026, 6, 1))
        assert oh._derive_costs(costs)["downtime_days"] == 1

    def test_downtime_days_none_while_running(self):
        assert oh._derive_costs(self._body())["downtime_days"] is None

    def test_spare_cost_sums_the_lines(self):
        body = self._body(
            spares=[
                {"item_id": 1, "qty": 2, "rate": 10},
                {"item_id": 2, "amount": 5.5},
            ]
        )
        assert oh._derive_costs(body)["spare_cost"] == 25.5

    def test_total_cost_is_labour_plus_spares(self):
        body = self._body(labour_cost=100, spares=[{"item_id": 1, "amount": 25}])
        costs = oh._derive_costs(body)
        assert costs["labour_cost"] == 100.0
        assert costs["total_cost"] == 125.0

    def test_missing_labour_cost_counts_as_zero(self):
        body = self._body(spares=[{"item_id": 1, "amount": 25}])
        assert oh._derive_costs(body)["total_cost"] == 25.0


class TestNextDueDate:
    """base_date + the machine's overhaul_freq_months, or None when unset."""

    def _db(self, attr_mapping):
        db = MagicMock()
        db.execute.return_value = _exec_result(
            fetchone=None if attr_mapping is None else _mock_row(attr_mapping)
        )
        return db

    def test_none_when_the_machine_has_no_attr_row(self):
        assert oh._next_due_date(self._db(None), 1, 10, date(2026, 6, 1)) is None

    def test_none_when_the_interval_is_blank(self):
        db = self._db({"overhaul_freq_months": None})
        assert oh._next_due_date(db, 1, 10, date(2026, 6, 1)) is None

    def test_none_when_the_interval_is_zero(self):
        db = self._db({"overhaul_freq_months": 0})
        assert oh._next_due_date(db, 1, 10, date(2026, 6, 1)) is None

    def test_adds_the_configured_months(self):
        db = self._db({"overhaul_freq_months": 12})
        assert oh._next_due_date(db, 1, 10, date(2026, 6, 15)) == date(2027, 6, 15)

    def test_uses_the_clamping_add(self):
        db = self._db({"overhaul_freq_months": 1})
        assert oh._next_due_date(db, 1, 10, date(2026, 1, 31)) == date(2026, 2, 28)


# =============================================================================
# 3. Endpoint tests — dependency_overrides, never a real DB
# =============================================================================


class TestOverhaulEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ------------------------------------------------------------ co_id gate
    def test_overhaul_table_missing_co_id_returns_400(self):
        response = client.get("/api/mechOverhaul/overhaul_table")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_overhaul_create_setup_missing_co_id_returns_400(self):
        response = client.get("/api/mechOverhaul/overhaul_create_setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # ---------------------------------------------------------------- read
    def test_overhaul_table_returns_data_and_total(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(total=1)),
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "overhaul_id": 3,
                            "machine_id": 10,
                            "machine_name": "SPR-1",
                            "overhaul_type": "MAJOR",
                            "from_date": date(2026, 6, 1),
                            "to_date": date(2026, 6, 3),
                            "next_due_date": date(2027, 6, 3),
                            "downtime_days": 3,
                            "labour_cost": 100.0,
                            "spare_cost": 25.0,
                            "total_cost": 125.0,
                        }
                    )
                ]
            ),
        ]

        response = client.get("/api/mechOverhaul/overhaul_table?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert payload["total"] == 1
        row = payload["data"][0]
        assert row["from_date"] == "2026-06-01"
        assert row["downtime_days"] == 3.0
        assert row["total_cost"] == 125.0

    def test_overhaul_table_rejects_unknown_overhaul_type(self):
        response = client.get("/api/mechOverhaul/overhaul_table?co_id=1&overhaul_type=HUGE")
        assert response.status_code == 400
        assert response.json()["detail"] == oh.BAD_TYPE_MSG

    def test_overhaul_by_id_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get("/api/mechOverhaul/overhaul_by_id/999?co_id=1")
        assert response.status_code == 404

    # -------------------------------------------------------------- create
    def _create_payload(self, **over):
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "overhaul_type": "MAJOR",
            "from_date": "2026-06-01",
            "to_date": "2026-06-03",
            "done_by": "INHOUSE",
            "labour_cost": 100,
        }
        payload.update(over)
        return payload

    def test_overhaul_create_vendor_without_party_id_returns_400(self):
        """done_by = VENDOR with no party_id is rejected before any DB read."""
        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(done_by="VENDOR"),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == oh.VENDOR_PARTY_MSG
        assert "party_id" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()

    def test_overhaul_create_vendor_with_unknown_party_returns_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # party lookup -> missing
        ]

        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(done_by="VENDOR", party_id=77),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == oh.UNKNOWN_PARTY_MSG

    def test_overhaul_create_rejects_unknown_type(self):
        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(overhaul_type="HUGE"),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == oh.BAD_TYPE_MSG

    def test_overhaul_create_rejects_to_date_before_from_date(self):
        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(to_date="2026-05-01"),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == oh.DATE_ORDER_MSG

    def test_overhaul_create_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),  # _machine_context -> missing
        ]
        response = client.post(
            "/api/mechOverhaul/overhaul_create", json=self._create_payload()
        )
        assert response.status_code == 400
        assert response.json()["detail"] == oh.UNKNOWN_MACHINE_MSG

    def test_overhaul_create_success_returns_overhaul_id(self):
        """Full happy path with the machine-attribute feedback write."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),                       # machine ctx
            _exec_result(fetchone=_mock_row({"overhaul_freq_months": 12})),  # next due
            _exec_result(lastrowid=301),                                 # insert header
            _exec_result(                                                # latest date
                fetchone=_mock_row({"last_overhaul_date": date(2026, 6, 3)})
            ),
            _exec_result(fetchone=_mock_row({"overhaul_freq_months": 12})),  # next due
            _exec_result(),                                              # attr upsert
        ]

        response = client.post(
            "/api/mechOverhaul/overhaul_create", json=self._create_payload()
        )

        assert response.status_code == 200
        assert response.json()["data"]["overhaul_id"] == 301
        self._mock_session.commit.assert_called_once()

        # The header carries every derived column.
        insert_params = self._mock_session.execute.call_args_list[2].args[1]
        assert insert_params["downtime_days"] == 3
        assert insert_params["labour_cost"] == 100.0
        assert insert_params["spare_cost"] == 0.0
        assert insert_params["total_cost"] == 100.0
        assert insert_params["branch_id"] == 1        # derived machine -> dept
        assert insert_params["next_due_date"] == date(2027, 6, 3)

        # ... and the attr row is fed with the recomputed dates.
        attr_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert attr_params["last_overhaul_date"] == date(2026, 6, 3)
        assert attr_params["next_overhaul_due"] == date(2027, 6, 3)

    def test_overhaul_create_without_interval_leaves_next_due_null(self):
        """A machine with no configured interval gets no invented due date."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=None),  # no attr row at all
            _exec_result(lastrowid=302),
            _exec_result(fetchone=_mock_row({"last_overhaul_date": date(2026, 6, 3)})),
            _exec_result(fetchone=None),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechOverhaul/overhaul_create", json=self._create_payload()
        )

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[2].args[1]
        assert insert_params["next_due_date"] is None

    def test_overhaul_create_body_missing_required_field_returns_422(self):
        response = client.post(
            "/api/mechOverhaul/overhaul_create", json={"co_id": 1, "machine_id": 10}
        )
        assert response.status_code == 422

    def test_overhaul_create_spare_lines_roll_into_total_cost(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=_mock_row({"overhaul_freq_months": 6})),
            _exec_result(lastrowid=303),
            _exec_result(),  # spare insert
            _exec_result(fetchone=_mock_row({"last_overhaul_date": date(2026, 6, 3)})),
            _exec_result(fetchone=_mock_row({"overhaul_freq_months": 6})),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(spares=[{"item_id": 5, "qty": 4, "rate": 12.5}]),
        )

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[2].args[1]
        assert insert_params["spare_cost"] == 50.0
        assert insert_params["total_cost"] == 150.0
        spare_params = self._mock_session.execute.call_args_list[3].args[1]
        assert spare_params["amount"] == 50.0

    def test_overhaul_create_negative_qty_rejected_by_pydantic(self):
        response = client.post(
            "/api/mechOverhaul/overhaul_create",
            json=self._create_payload(spares=[{"item_id": 5, "qty": -1}]),
        )
        assert response.status_code == 422


class TestMachineAttrFeedback:
    """The attr write reads MAX(date) over live rows, never the saved row."""

    def test_clears_both_columns_when_no_live_overhaul_remains(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_mock_row({"last_overhaul_date": None})),
            _exec_result(),  # clear
        ]

        oh._write_machine_attr_feedback(db, co_id=1, machine_id=10, user_id=1)

        assert db.execute.call_count == 2
        clear_params = db.execute.call_args_list[-1].args[1]
        assert clear_params["machine_id"] == 10

    def test_upserts_the_recomputed_dates(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_mock_row({"last_overhaul_date": date(2026, 1, 31)})),
            _exec_result(fetchone=_mock_row({"overhaul_freq_months": 1})),
            _exec_result(),  # upsert
        ]

        oh._write_machine_attr_feedback(db, co_id=1, machine_id=10, user_id=1)

        params = db.execute.call_args_list[-1].args[1]
        assert params["last_overhaul_date"] == date(2026, 1, 31)
        # 31 Jan + 1 month clamps to 28 Feb.
        assert params["next_overhaul_due"] == date(2026, 2, 28)


class TestValidateHeaderDirect:
    """The gate on its own, so the 400s are provably pre-DB."""

    def test_vendor_without_party_raises_before_touching_the_db(self):
        db = MagicMock()
        body = oh.OverhaulCreate(
            co_id=1, machine_id=10, from_date=date(2026, 6, 1), done_by="VENDOR"
        )
        with pytest.raises(HTTPException) as exc:
            oh._validate_header(db, body)
        assert exc.value.status_code == 400
        assert exc.value.detail == oh.VENDOR_PARTY_MSG
        db.execute.assert_not_called()

    def test_inhouse_needs_no_party(self):
        db = MagicMock()
        body = oh.OverhaulCreate(
            co_id=1, machine_id=10, from_date=date(2026, 6, 1), done_by="INHOUSE"
        )
        oh._validate_header(db, body)
        db.execute.assert_not_called()

    def test_done_by_is_case_insensitive(self):
        db = MagicMock()
        body = oh.OverhaulCreate(
            co_id=1, machine_id=10, from_date=date(2026, 6, 1), done_by="inhouse"
        )
        oh._validate_header(db, body)

    def test_rejects_unknown_done_by(self):
        db = MagicMock()
        body = oh.OverhaulCreate(
            co_id=1, machine_id=10, from_date=date(2026, 6, 1), done_by="CONTRACTOR"
        )
        with pytest.raises(HTTPException) as exc:
            oh._validate_header(db, body)
        assert exc.value.detail == oh.BAD_DONE_BY_MSG
