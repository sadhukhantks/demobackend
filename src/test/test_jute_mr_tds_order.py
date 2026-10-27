"""194Q TDS counts only the party's MRs that come BEFORE this one (owner ruling
2026-10-03), at approve and at bill pass alike.

Order ("before") = (jute_mr_date, COALESCE(branch_mr_no, 0), jute_mr_id) ascending,
over the party's status-3 MRs in the FY of the MR date — see
mr.get_cumulative_mr_value_for_party_in_fy.

Part A runs the REAL SQL of that function on an in-memory SQLite stand-in with the
MySQL date functions registered (same approach as test_jute_mr_movement_report.py);
no real database. Part B checks the approve / bill pass endpoints hand the function
the MR's own keys (mocked DB, as the other MR tests do).
"""
import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement import billPass as bp
from src.juteProcurement import mr as mr_module
from src.juteProcurement.mr import (
    MR_ORDER_KEY_UNASSIGNED,
    TDS_THRESHOLD,
    calculate_tds_amount,
    derive_mr_tds,
    get_cumulative_mr_value_for_party_in_fy,
)

# PyMySQL sends a date as 'YYYY-MM-DD'; make sqlite3 do the same (its built-in
# default adapter does exactly this but is deprecated in Python 3.12).
sqlite3.register_adapter(date, date.isoformat)

LAKH = 100_000.0
PARTY = "8646"
OTHER_PARTY = "9999"


# =============================================================================
# Part A — the rule, on the real SQL
# =============================================================================

def _engine():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_mysql_functions(dbapi_conn, _record):
        def concat(*args):
            if any(a is None for a in args):
                return None
            return "".join(str(a) for a in args)

        def lpad(value, length, pad):
            if value is None:
                return None
            s = str(value)
            return s[:length] if len(s) >= length else (pad * length + s)[-length:]

        dbapi_conn.create_function("CONCAT", -1, concat)
        dbapi_conn.create_function("LPAD", 3, lpad)
        dbapi_conn.create_function("MOD", 2, lambda a, b: None if a is None else a % b)
        dbapi_conn.create_function("MONTH", 1, lambda d: d and int(d[5:7]))
        dbapi_conn.create_function("YEAR", 1, lambda d: d and int(d[:4]))

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE jute_mr (
                jute_mr_id INTEGER PRIMARY KEY,
                branch_id INTEGER,
                party_id TEXT,
                branch_mr_no INTEGER,
                jute_mr_date TEXT,
                status_id INTEGER,
                total_amount REAL,
                tds_amount REAL
            )
        """))
    return engine


def _insert(db, mr_id, mr_date, mr_no, total, *, status=3, party=PARTY, branch=29, tds=None):
    db.execute(text("""
        INSERT INTO jute_mr (jute_mr_id, branch_id, party_id, branch_mr_no, jute_mr_date, status_id, total_amount, tds_amount)
        VALUES (:id, :branch, :party, :no, :dt, :st, :total, :tds)
    """), {"id": mr_id, "branch": branch, "party": party, "no": mr_no,
           "dt": mr_date.isoformat() if mr_date else None, "st": status, "total": total, "tds": tds})


def _cum(db, mr_id, mr_date, mr_no, party=PARTY):
    return get_cumulative_mr_value_for_party_in_fy(db, party, mr_date, exclude_mr_id=mr_id, branch_mr_no=mr_no)


def _approve(db, mr_id, mr_date, mr_no, total, party=PARTY):
    """What approve_mr does for the TDS: place the MR by the date / number it is
    about to write, derive TDS from the earlier MRs, store the row as approved."""
    out = derive_mr_tds(db, party_id=party, mr_date=mr_date, mr_id=mr_id, branch_mr_no=mr_no, total_amount=total)
    _insert(db, mr_id, mr_date, mr_no, total, party=party, tds=out["tds_amount"])
    return out


def _bill_pass(db, mr_id):
    """What update_jute_bill_pass does for the TDS: re-derive from the STORED keys."""
    row = db.execute(text("SELECT party_id, jute_mr_date, branch_mr_no, total_amount FROM jute_mr WHERE jute_mr_id = :id"),
                     {"id": mr_id}).fetchone()
    return derive_mr_tds(db, party_id=row.party_id, mr_date=date.fromisoformat(row.jute_mr_date),
                         mr_id=mr_id, branch_mr_no=row.branch_mr_no, total_amount=row.total_amount)


@pytest.fixture
def db():
    engine = _engine()
    with Session(engine) as session:
        yield session


@pytest.fixture
def ledger(db):
    """Four MRs of the party approved in date order (FY 26-27, branch 29):
    20L, 20L, 20L (crosses 50L), 10L — plus rows that must never count."""
    _approve(db, 101, date(2026, 8, 5), 1, 20 * LAKH)
    _approve(db, 102, date(2026, 8, 9), 2, 20 * LAKH)
    _approve(db, 103, date(2026, 8, 10), 3, 20 * LAKH)
    _approve(db, 104, date(2026, 8, 12), 4, 10 * LAKH)
    # noise: never part of the party's 194Q cumulative
    _insert(db, 901, None, None, 99 * LAKH, status=13)                      # Pending hand-off: no date/number yet
    _insert(db, 902, date(2026, 3, 31), 77, 99 * LAKH)                     # previous FY (25-26)
    _insert(db, 903, date(2026, 8, 1), 5, 99 * LAKH, party=OTHER_PARTY)    # another party
    _insert(db, 904, date(2026, 8, 1), None, 99 * LAKH, status=4)          # rejected
    _insert(db, 905, date(2026, 8, 1), None, 99 * LAKH, status=48)         # returned
    _insert(db, 906, date(2026, 8, 1), None, 99 * LAKH, status=1)          # open
    return db


class TestFirstBelowCrossingLater:
    def test_first_mr_below_threshold_has_no_tds(self, ledger):
        assert _cum(ledger, 101, date(2026, 8, 5), 1) == 0.0
        assert calculate_tds_amount(0.0, 20 * LAKH) == (0.0, 0.0, False)
        assert ledger.execute(text("SELECT tds_amount FROM jute_mr WHERE jute_mr_id = 101")).scalar() == 0.0

    def test_second_mr_still_below_threshold(self, ledger):
        assert _cum(ledger, 102, date(2026, 8, 9), 2) == 20 * LAKH
        assert calculate_tds_amount(20 * LAKH, 20 * LAKH) == (0.0, 0.0, False)

    def test_crossing_mr_pays_only_on_the_excess(self, ledger):
        # before = 40L, after = 60L -> TDS on the 10L above 50L
        assert _cum(ledger, 103, date(2026, 8, 10), 3) == 40 * LAKH
        assert calculate_tds_amount(40 * LAKH, 20 * LAKH) == (1000.0, 10 * LAKH, True)
        assert ledger.execute(text("SELECT tds_amount FROM jute_mr WHERE jute_mr_id = 103")).scalar() == 1000.0

    def test_later_mr_pays_on_its_full_amount(self, ledger):
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 60 * LAKH
        assert calculate_tds_amount(60 * LAKH, 10 * LAKH) == (1000.0, 10 * LAKH, True)

    def test_exactly_at_threshold_is_exempt(self):
        assert calculate_tds_amount(40 * LAKH, 10 * LAKH) == (0.0, 0.0, False)
        assert TDS_THRESHOLD == 50 * LAKH

    def test_three_bills_of_30_lakh(self):
        # owner's example: 0 / TDS on 10L / TDS on the full 30L
        assert calculate_tds_amount(0.0, 30 * LAKH)[0] == 0.0
        assert calculate_tds_amount(30 * LAKH, 30 * LAKH)[:2] == (1000.0, 10 * LAKH)
        assert calculate_tds_amount(60 * LAKH, 30 * LAKH)[:2] == (3000.0, 30 * LAKH)

    def test_tds_is_whole_rupees_half_up(self):
        assert calculate_tds_amount(50 * LAKH, 221_789.6)[0] == 222.0     # 221.79 -> 222
        assert calculate_tds_amount(50 * LAKH, 221_489.6)[0] == 221.0     # 221.49 -> 221
        assert calculate_tds_amount(50 * LAKH, 2_500.0)[0] == 3.0         # 2.50 -> 3, never banker's 2

    def test_noise_rows_never_count(self, ledger):
        # a hypothetical MR dated after everything: only the four approved FY MRs count
        assert _cum(ledger, None, date(2026, 12, 31), None) == 70 * LAKH


class TestBillPassKeepsTheExemption:
    """The bug: with every later MR approved, a bill pass save used to count them all,
    charging the first MRs of the year TDS on their full amount."""

    def test_bill_pass_on_first_mr_after_later_approvals_is_still_exempt(self, ledger):
        for mr_id, expected in ((101, 0.0), (102, 0.0), (103, 1000.0), (104, 1000.0)):
            out = _bill_pass(ledger, mr_id)
            stored = ledger.execute(text("SELECT tds_amount FROM jute_mr WHERE jute_mr_id = :id"), {"id": mr_id}).scalar()
            assert out["tds_amount"] == expected == stored, mr_id

    def test_bill_pass_cumulative_is_the_approve_cumulative(self, ledger):
        assert _bill_pass(ledger, 101)["cumulative_previous"] == 0.0
        assert _bill_pass(ledger, 103)["cumulative_previous"] == 40 * LAKH
        # the old "every other approved MR" basis would have been 50L for MR 101
        every_other = ledger.execute(text(
            "SELECT SUM(total_amount) FROM jute_mr WHERE party_id = :p AND status_id = 3 "
            "AND jute_mr_date >= '2026-04-01' AND jute_mr_id != 101"), {"p": PARTY}).scalar()
        assert every_other == 50 * LAKH


class TestBackDatedMr:
    def test_back_dated_mr_takes_its_place_by_date_at_approve(self, ledger):
        # Approved last (number 5) but dated between 101 and 102: only 101 is before it,
        # although 102-104 are already approved.
        out = _approve(ledger, 105, date(2026, 8, 7), 5, 5 * LAKH)
        assert out["cumulative_previous"] == 20 * LAKH
        assert out["tds_amount"] == 0.0

    def test_back_dated_mr_moves_the_crossing_for_later_dated_mrs(self, ledger):
        _approve(ledger, 105, date(2026, 8, 7), 5, 5 * LAKH)
        # 103's stored TDS (1000) is stale now; its next bill pass save picks up 105
        assert _cum(ledger, 103, date(2026, 8, 10), 3) == 45 * LAKH
        assert _bill_pass(ledger, 103)["tds_amount"] == 1500.0
        assert _bill_pass(ledger, 102)["tds_amount"] == 0.0     # 25L before, 45L after
        assert _bill_pass(ledger, 104)["tds_amount"] == 1000.0  # 65L before: full
        # total TDS of the party is unchanged by the order; only its distribution moved
        assert sum(_bill_pass(ledger, m)["tds_amount"] for m in (101, 102, 103, 104, 105)) == 2500.0

    def test_mr_dated_after_this_one_never_counts(self, ledger):
        _approve(ledger, 106, date(2026, 9, 1), 6, 30 * LAKH)
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 60 * LAKH


class TestSameDayOrder:
    def test_same_day_same_branch_goes_by_mr_number(self, ledger):
        # 09-08: MR 102 (no 2); a same-day MR approved later gets no 5 and comes after 102
        _approve(ledger, 107, date(2026, 8, 9), 5, 1 * LAKH)
        assert _cum(ledger, 107, date(2026, 8, 9), 5) == 40 * LAKH   # 101 + 102
        assert _cum(ledger, 102, date(2026, 8, 9), 2) == 20 * LAKH   # 107 is after 102

    def test_same_day_two_branches_goes_by_number_then_id(self, ledger):
        # The party's MRs at a second branch of the company: its number sequence is
        # separate, so same-day ties fall to COALESCE(number) then jute_mr_id.
        _approve(ledger, 108, date(2026, 8, 12), 1, 2 * LAKH, )  # branch 30, no 1 -> before 104 (no 4)
        ledger.execute(text("UPDATE jute_mr SET branch_id = 30 WHERE jute_mr_id = 108"))
        assert _cum(ledger, 108, date(2026, 8, 12), 1) == 60 * LAKH
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 62 * LAKH

    def test_legacy_rows_without_number_fall_back_to_id(self, ledger):
        # An un-numbered row sorts first on its day (COALESCE -> 0) whether it is the
        # MR being placed or one of the other rows, so the order stays a total order.
        _insert(ledger, 109, date(2026, 8, 12), None, 1 * LAKH)
        _insert(ledger, 110, date(2026, 8, 12), None, 2 * LAKH)
        assert _cum(ledger, 109, date(2026, 8, 12), None) == 60 * LAKH   # 104 (no 4) is after a NULL number
        assert _cum(ledger, 110, date(2026, 8, 12), None) == 61 * LAKH   # 109 is before by id
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 63 * LAKH      # both NULL-number rows are before
        # each same-day pair is counted once, from exactly one side
        pairs = [(109, None), (110, None), (104, 4)]
        befores = {m: _cum(ledger, m, date(2026, 8, 12), n) for m, n in pairs}
        assert befores[109] < befores[110] < befores[104]

    def test_preview_without_an_id_sorts_after_its_ties(self, ledger):
        # Not yet an MR (exclude_mr_id None): every MR tying on date and number counts.
        assert _cum(ledger, None, date(2026, 8, 9), 2) == 40 * LAKH      # 101 and the tying 102
        assert _cum(ledger, None, date(2026, 8, 9), 1) == 20 * LAKH      # number 1 < 102's 2
        assert MR_ORDER_KEY_UNASSIGNED == 2147483647

    def test_the_mr_itself_never_counts_even_with_stale_stored_keys(self, ledger):
        # re-approve with a changed date: the row still carries the old date (08-10)
        # while the cumulative is asked for the new date — it must not count itself.
        assert _cum(ledger, 103, date(2026, 8, 20), 3) == 50 * LAKH   # 101, 102, 104 (20+20+10); not 103's 20


class TestReopenReapprove:
    def test_reopen_then_reapprove_restores_the_same_tds(self, ledger):
        # Reopen (P2-2): status 1, TDS cleared, MR number and date kept
        ledger.execute(text("UPDATE jute_mr SET status_id = 1, tds_amount = NULL WHERE jute_mr_id = 103"))
        # while reopened it does not count for the MRs after it
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 40 * LAKH
        # re-approve with the kept number 3 and the same date: same place, same TDS
        out = derive_mr_tds(ledger, party_id=PARTY, mr_date=date(2026, 8, 10), mr_id=103, branch_mr_no=3,
                            total_amount=20 * LAKH)
        ledger.execute(text("UPDATE jute_mr SET status_id = 3, tds_amount = :t WHERE jute_mr_id = 103"), {"t": out["tds_amount"]})
        assert out["cumulative_previous"] == 40 * LAKH and out["tds_amount"] == 1000.0
        assert _cum(ledger, 104, date(2026, 8, 12), 4) == 60 * LAKH

    def test_reapprove_with_a_new_date_moves_the_mr(self, ledger):
        ledger.execute(text("UPDATE jute_mr SET status_id = 1, tds_amount = NULL WHERE jute_mr_id = 103"))
        # re-approved dated 08-20 (after 104): 104 now comes before it
        out = derive_mr_tds(ledger, party_id=PARTY, mr_date=date(2026, 8, 20), mr_id=103, branch_mr_no=3,
                            total_amount=20 * LAKH)
        assert out["cumulative_previous"] == 50 * LAKH and out["tds_amount"] == 2000.0


class TestDeriveMrTds:
    def test_no_party_or_no_date_means_no_tds_and_no_query(self):
        db = MagicMock()
        for kwargs in ({"party_id": None, "mr_date": date(2026, 8, 5)}, {"party_id": PARTY, "mr_date": None}):
            out = derive_mr_tds(db, mr_id=1, branch_mr_no=1, total_amount=90 * LAKH, **kwargs)
            assert out == {"tds_amount": 0.0, "tds_applicable_amount": 0.0, "is_tds_applicable": False,
                           "cumulative_previous": 0.0, "cumulative_after": 90 * LAKH}
        db.execute.assert_not_called()

    def test_sql_places_the_mr_by_date_number_id(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (123.0,)
        assert get_cumulative_mr_value_for_party_in_fy(db, PARTY, date(2026, 8, 10), exclude_mr_id=103, branch_mr_no=3) == 123.0
        sql, params = db.execute.call_args.args
        sql = str(sql)
        assert "jute_mr_date < :mr_date" in sql
        assert "jute_mr_date = :mr_date AND COALESCE(branch_mr_no, 0) < :mr_no" in sql
        assert "COALESCE(branch_mr_no, 0) = :mr_no AND jute_mr_id < :mr_id" in sql
        assert "jute_mr_id != :exclude_mr_id" in sql and "status_id = :status_approved" in sql
        assert params["mr_date"] == date(2026, 8, 10) and params["mr_no"] == 3 and params["mr_id"] == 103
        assert params["exclude_mr_id"] == 103 and params["status_approved"] == 3 and params["fy_string"] == "26-27"


# =============================================================================
# Part B — approve and bill pass hand the function the MR's own keys
# =============================================================================

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def _row(**cols):
    m = MagicMock()
    m._mapping = cols
    for k, v in cols.items():
        setattr(m, k, v)
    return m


class TestApproveEndpoint:
    def test_approve_places_the_mr_by_the_number_it_assigns(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=55, status_id=1, party_id=PARTY, party_branch_id=5, branch_id=29,
                 branch_mr_no=None, jute_mr_date=None, bill_pass_no=None),
            (22,),                                               # get_next_mr_no_for_fy (reads result[0])
            (22,),                                               # get_next_bill_pass_no_for_fy
            _row(total_amount=20 * LAKH, claim_amount=0.0),      # calculate_mr_amounts (tds calc)
            _row(total_amount=20 * LAKH, claim_amount=0.0),      # calculate_mr_amounts (inside recompute_mr_money)
            _row(po_id=None),                                    # settle_after_mr_change
            _row(co_prefix="AB", branch_prefix="CD"),            # doc-number prefix lookup
        ]
        with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy", return_value=45 * LAKH) as cum:
            r = client.post("/api/juteMR/approve_mr", json={"mr_id": "55", "branch_id": "29", "mr_date": "2026-08-10"})
        assert r.status_code == 200, r.text
        cum.assert_called_once_with(mock_db, PARTY, date(2026, 8, 10), exclude_mr_id=55, branch_mr_no=22)
        amounts = r.json()["calculated_amounts"]
        assert amounts["cumulative_previous"] == 45 * LAKH and amounts["cumulative_after"] == 65 * LAKH
        assert amounts["tds_amount"] == 1500.0 and amounts["tds_applicable_amount"] == 15 * LAKH
        assert amounts["is_tds_applicable"] is True and amounts["tds_threshold"] == TDS_THRESHOLD
        assert r.json()["branch_mr_no"] == 22

    def test_reapprove_keeps_its_number_as_the_sort_key(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=55, status_id=1, party_id=PARTY, party_branch_id=5, branch_id=29,
                 branch_mr_no=3, jute_mr_date=date(2026, 8, 10), bill_pass_no=3),
            _row(total_amount=20 * LAKH, claim_amount=0.0),
            _row(total_amount=20 * LAKH, claim_amount=0.0),
            _row(po_id=None),
            _row(co_prefix="AB", branch_prefix="CD"),
        ]
        with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy", return_value=40 * LAKH) as cum:
            r = client.post("/api/juteMR/approve_mr", json={"mr_id": "55", "branch_id": "29", "mr_date": "2026-08-10"})
        assert r.status_code == 200, r.text
        cum.assert_called_once_with(mock_db, PARTY, date(2026, 8, 10), exclude_mr_id=55, branch_mr_no=3)
        assert r.json()["calculated_amounts"]["tds_amount"] == 1000.0


class TestBillPassEndpoint:
    def test_bill_pass_places_the_mr_by_its_stored_keys(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=101, status_id=3, total_amount=20 * LAKH, claim_amount=0.0, tds_amount=2000.0,
            party_id=PARTY, jute_mr_date=date(2026, 8, 5), branch_mr_no=1,
        )
        with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy", return_value=0.0) as cum, \
             patch.object(bp, "calculate_mr_amounts", return_value={"total_amount": 20 * LAKH, "claim_amount": 0.0}), \
             patch.object(bp, "recompute_mr_money", return_value={}) as rc:
            r = client.put("/api/juteBillPass/update_bill_pass/101?co_id=2", json={"invoice_no": "INV-1"})
        assert r.status_code == 200, r.text
        cum.assert_called_once_with(mock_db, PARTY, date(2026, 8, 5), exclude_mr_id=101, branch_mr_no=1)
        assert rc.call_args.kwargs["tds_amount"] == 0.0   # the first MR of the year stays exempt

    def test_bill_pass_without_party_stores_zero_tds_without_a_cumulative_query(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=101, status_id=3, total_amount=1.0, claim_amount=0.0, tds_amount=5.0,
            party_id=None, jute_mr_date=date(2026, 8, 5), branch_mr_no=1,
        )
        with patch.object(mr_module, "get_cumulative_mr_value_for_party_in_fy") as cum, \
             patch.object(bp, "calculate_mr_amounts", return_value={"total_amount": 90 * LAKH, "claim_amount": 0.0}), \
             patch.object(bp, "recompute_mr_money", return_value={}) as rc:
            r = client.put("/api/juteBillPass/update_bill_pass/101?co_id=2", json={"remarks": "x"})
        assert r.status_code == 200, r.text
        cum.assert_not_called()
        assert rc.call_args.kwargs["tds_amount"] == 0.0

    def test_bill_pass_reads_branch_mr_no(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        r = client.put("/api/juteBillPass/update_bill_pass/1?co_id=2", json={"remarks": "x"})
        assert r.status_code == 404
        assert "jm.branch_mr_no" in str(mock_db.execute.call_args_list[0].args[0])
