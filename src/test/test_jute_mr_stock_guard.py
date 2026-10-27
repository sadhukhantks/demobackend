"""Tests for the MR-stock write guard on the Jute Issue page.

The WASTE stock source has been guarded against over-issue since it was added;
the MR source was not — create_issue/update_issue only read `actual_rate` off
the line and wrote the row, so purchased jute could be issued into the negative.
`_resolve_mr_stock_for_issue` closes that, with two deliberate asymmetries that
are the whole point of this file:

  1. the QUANTITY and WEIGHT ledgers are judged independently — every real
     over-issue found on sls over-drew quantity while weight stayed in credit,
     so a weight-only check would have caught none of them;
  2. they are not judged alike — weight over-draw is a hard 400, quantity
     over-draw only WARNS, because ~581 branch-87 MR lines carry junk
     actual_qty and a hard block there would reject legitimate weight draws;
  3. only the POSITIVE direction is guarded — a negative correction row returns
     stock, and blocking it would forbid the one entry that repairs a line
     that has already gone negative.

Follows test_jute_waste_stock.py: FastAPI dependency overrides + a mocked
session, so nothing here touches a database.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import (
    JUTE_MR_APPROVED_STATUSES,
    JUTE_MR_ISSUABLE_STATUSES,
)
from src.juteProcurement.query import (
    get_jute_mr_line_for_issue_query,
    get_jute_stock_outstanding_by_item_query,
    get_jute_stock_outstanding_query,
    get_jute_waste_stock_outstanding_query,
)
from src.main import app

client = TestClient(app)

BRANCH = 29


def _stock_row(bal_qty, bal_weight, branch_id=BRANCH, rate=1500.0, uom="BALE",
               is_waste=0, line_rate=None):
    """One row of get_jute_mr_line_for_issue_query.

    SimpleNamespace rather than MagicMock on purpose: a typo'd attribute must
    blow up here instead of quietly auto-vivifying into a truthy stub.

    `rate` is jml.actual_rate (what the MR page saves); `line_rate` is jml.rate
    (what the QC insert writes) — the guard falls back to it for a waste line.
    """
    return SimpleNamespace(
        jute_mr_li_id=24143,
        branch_id=branch_id,
        unit_conversion=uom,
        actual_rate=rate,
        rate=line_rate,
        is_waste=is_waste,
        bal_qty=bal_qty,
        bal_weight=bal_weight,
    )


def _stock_row_via_join(mr_status_id, bal_qty, bal_weight, **kw):
    """What the guard query returns for an MR sitting in `mr_status_id`.

    The status filter lives ON the LEFT JOIN, so a non-issuable MR comes back as
    the line row with NULL balances no matter how much stock the (deliberately
    broader) view itself holds for it. `test_the_status_filter_is_on_the_join`
    is what keeps this emulation honest.
    """
    issuable = mr_status_id in JUTE_MR_ISSUABLE_STATUSES
    return _stock_row(
        bal_qty=bal_qty if issuable else None,
        bal_weight=bal_weight if issuable else None,
        **kw,
    )


def _existing_row(quantity=10.0, weight=100.0, status_id=21):
    """One row of update_issue's check_sql — the line being edited."""
    return SimpleNamespace(
        jute_issue_id=1,
        status_id=status_id,
        jute_mr_li_id=24143,
        waste_entry_id=None,
        branch_id=BRANCH,
        issue_date="2026-08-10",
        quantity=quantity,
        weight=weight,
    )


def _router(stock_row=None, existing=None, prior=None):
    """Route each db.execute() to the right canned row by looking at the SQL."""

    def _execute(sql, params=None):
        normalised = " ".join(str(sql).split())
        result = MagicMock()
        if "FROM jute_mr_li jml" in normalised:
            result.fetchone.return_value = stock_row
        elif "SELECT quantity, weight FROM jute_issue" in normalised:
            result.fetchone.return_value = prior
        elif "FROM jute_issue ji INNER JOIN branch_mst bm" in normalised:
            result.fetchone.return_value = existing
        elif "LAST_INSERT_ID" in normalised:
            result.fetchone.return_value = SimpleNamespace(id=4242)
        else:
            result.fetchone.return_value = None
        return result

    return _execute


class TestMrStockGuard:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1,
            "sub": "test_user",
            "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_db(self):
        return self._mock_session

    @staticmethod
    def _payload(**overrides):
        body = {
            "branch_id": BRANCH,
            "issue_date": "2026-08-10",
            "jute_mr_li_id": 24143,
            "yarn_type_id": 5,
            "item_id": 335637,
            "quantity": 10.0,
            "weight": 100.0,
            "unit_conversion": "BALE",
        }
        body.update(overrides)
        return body

    def _create(self, **overrides):
        return client.post(
            "/api/juteIssue/create_issue?co_id=2", json=self._payload(**overrides)
        )

    # -- hard rejections ---------------------------------------------------

    def test_unknown_mr_line_is_rejected(self, mock_db):
        mock_db.execute.side_effect = _router(stock_row=None)
        response = self._create()
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid MR line item ID"

    def test_mr_line_from_another_branch_is_rejected(self, mock_db):
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=500, bal_weight=5000, branch_id=4)
        )
        response = self._create()
        assert response.status_code == 400
        assert "different branch" in response.json()["detail"].lower()

    def test_ineligible_mr_is_rejected_distinctly_from_a_bad_id(self, mock_db):
        """LEFT JOIN: the line resolved but the stock view has nothing for it."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=None, bal_weight=None)
        )
        response = self._create()
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "not available for issue" in detail.lower()
        assert detail != "Invalid MR line item ID"

    def test_a_draft_mr_holding_stock_in_the_view_is_still_rejected(self, mock_db):
        """Post-migration shape: the widened view carries no row for a status-21
        (Draft) MR, and the join filter keeps it out even if it did — a direct
        POST never sees the picker's restriction."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row_via_join(mr_status_id=21, bal_qty=500.0, bal_weight=5000.0)
        )
        response = self._create()
        assert response.status_code == 400
        assert "not available for issue" in response.json()["detail"].lower()

    def test_an_open_mr_is_issuable(self, mock_db):
        """The floor draws jute against an MR as soon as it is OPEN (status 1),
        well before approval. This is the behaviour change; it used to 400."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row_via_join(mr_status_id=1, bal_qty=500.0, bal_weight=5000.0)
        )
        assert self._create().status_code == 200

    def test_a_finalised_mr_with_the_same_stock_is_accepted(self, mock_db):
        """Status 13 is issuable — proves the rejection above is the status,
        not the fixture always answering NULL."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row_via_join(mr_status_id=13, bal_qty=500.0, bal_weight=5000.0)
        )
        assert self._create().status_code == 200

    # -- the two ledgers, judged independently and unalike -----------------

    def test_weight_over_draw_is_blocked(self, mock_db):
        """Weight is the trustworthy ledger, so it hard-blocks."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=500, bal_weight=50)
        )
        response = self._create(quantity=10.0, weight=100.0)
        assert response.status_code == 400
        assert "balance weight" in response.json()["detail"].lower()

    def test_quantity_over_draw_only_warns(self, mock_db):
        """581 br-87 lines carry junk actual_qty — warn, don't reject the draw."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=5, bal_weight=1000)
        )
        response = self._create(quantity=10.0, weight=100.0)
        assert response.status_code == 200
        warnings = response.json()["warnings"]
        assert warnings and "quantity" in warnings[0].lower()

    def test_both_ledgers_over_drawn_still_blocks_on_weight(self, mock_db):
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=1, bal_weight=1)
        )
        response = self._create(quantity=10.0, weight=100.0)
        assert response.status_code == 400
        assert "balance weight" in response.json()["detail"].lower()

    def test_warning_names_the_lines_own_unit_not_kg(self, mock_db):
        """sls weights are quintal — a hardcoded 'kg' would misreport them."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=5, bal_weight=1000, uom="DRUM")
        )
        response = self._create(quantity=10.0, weight=100.0)
        assert "DRUM" in response.json()["warnings"][0]

    # -- the boundaries ----------------------------------------------------

    def test_exact_balance_draw_passes(self, mock_db):
        """FLOAT ledgers fed from DECIMAL sources: full-balance must clear."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=10.0, bal_weight=100.0)
        )
        response = self._create(quantity=10.0, weight=100.0)
        assert response.status_code == 200
        assert response.json()["warnings"] == []

    def test_negative_correction_on_a_negative_line_passes(self, mock_db):
        """-5 > -49 is True. Guarding both directions would block the repair."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=-49.0, bal_weight=-500.0)
        )
        response = self._create(quantity=-5.0, weight=-50.0)
        assert response.status_code == 200
        assert response.json()["warnings"] == []

    def test_a_clean_line_is_priced_and_written(self, mock_db):
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=500, bal_weight=5000, rate=1500.0)
        )
        response = self._create(quantity=10.0, weight=200.0)
        assert response.status_code == 200
        assert response.json()["issue_value"] == pytest.approx(3000.0)

    # -- edits -------------------------------------------------------------

    def test_re_saving_identical_values_passes(self, mock_db):
        """The view already netted this row off; without the add-back a no-op
        edit would read as a second draw against its own quantity."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=0.0),
            existing=_existing_row(quantity=10.0, weight=100.0),
            prior=SimpleNamespace(quantity=10.0, weight=100.0),
        )
        response = client.put(
            "/api/juteIssue/update_issue/1?co_id=2", json={"quantity": 10.0, "weight": 100.0}
        )
        assert response.status_code == 200
        assert response.json()["warnings"] == []

    def test_a_quantity_only_edit_is_still_guarded(self, mock_db):
        """Regression: the guard used to hang off `weight is not None` alone,
        so the edit most likely to over-draw slipped past it entirely."""
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=0.0),
            existing=_existing_row(quantity=10.0, weight=100.0),
            prior=SimpleNamespace(quantity=10.0, weight=100.0),
        )
        response = client.put("/api/juteIssue/update_issue/1?co_id=2", json={"quantity": 999.0})
        assert response.status_code == 200
        executed = " ".join(str(c[0][0]) for c in mock_db.execute.call_args_list)
        assert "vw_jute_stock_outstanding" in executed, "stock guard never ran"
        assert response.json()["warnings"], "999 against a balance of 10 should warn"

    def test_an_edit_over_drawing_weight_is_blocked(self, mock_db):
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=0.0),
            existing=_existing_row(quantity=10.0, weight=100.0),
            prior=SimpleNamespace(quantity=10.0, weight=100.0),
        )
        response = client.put("/api/juteIssue/update_issue/1?co_id=2", json={"weight": 500.0})
        assert response.status_code == 400
        assert "balance weight" in response.json()["detail"].lower()


class TestGuardQueryShape:
    def test_stock_join_is_left_so_two_failures_stay_distinguishable(self):
        sql = " ".join(str(get_jute_mr_line_for_issue_query()).split())
        assert "LEFT JOIN vw_jute_stock_outstanding v" in sql
        assert "bm.co_id = :co_id" in sql

    def test_the_status_filter_is_on_the_join_not_the_where(self):
        """In the WHERE it would drop the row entirely, degrading the LEFT JOIN
        into an inner one and collapsing "not approved" back into "invalid ID"."""
        sql = " ".join(str(get_jute_mr_line_for_issue_query()).split())
        join_clause, _, where_clause = sql.partition("WHERE")
        expected = "v.mr_status_id IN ({})".format(
            ", ".join(str(s) for s in JUTE_MR_ISSUABLE_STATUSES)
        )
        assert expected in join_clause, "the guard must re-narrow the broader view"
        assert "mr_status_id" not in where_clause

    def test_item_group_is_joined_for_the_waste_flag_only(self):
        """Owner ruling 2026-09-25: the guard learns whether the line is
        purchased waste from the item group's item_type_id — LEFT JOINed, as a
        flag column, never as a WHERE filter that would turn a group-less line
        into 'Invalid MR line item ID'."""
        sql = " ".join(str(get_jute_mr_line_for_issue_query()).split())
        join_clause, _, where_clause = sql.partition("WHERE")
        assert "LEFT JOIN item_mst im ON im.item_id = jml.actual_item_id" in join_clause
        assert "LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id" in join_clause
        assert "CASE WHEN ig.item_type_id = 3 THEN 1 ELSE 0 END AS is_waste" in sql
        assert "jml.rate," in sql
        assert "item_type_id" not in where_clause

    def test_add_back_matches_the_mr_views_own_predicate(self):
        """The MR view's issued-total subquery uses a bare `status_id <> 4`;
        crediting back on the waste helper's wider predicate would over-credit
        a NULL-status row the view never subtracted."""
        import inspect

        from src.juteProcurement import issue as issue_module

        src = inspect.getsource(issue_module._resolve_mr_stock_for_issue)
        # comments discuss the predicate they must NOT use — judge the code only
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        assert "AND status_id <> 4" in code
        assert "status_id IS NULL" not in code


class TestPickerStaysOnIssuableStock:
    """The view is widened to NOT IN (4,6,21); the picker must not follow it all
    the way — it stops at JUTE_MR_ISSUABLE_STATUSES."""

    def test_issuable_tuple_covers_open_and_never_the_dead_statuses(self):
        """The floor issues against an MR the moment it is OPEN, but a rejected,
        cancelled or still-draft MR is never stock."""
        assert 1 in JUTE_MR_ISSUABLE_STATUSES, "OPEN MRs must be issuable"
        assert set(JUTE_MR_APPROVED_STATUSES) <= set(JUTE_MR_ISSUABLE_STATUSES)
        assert not set(JUTE_MR_ISSUABLE_STATUSES) & {4, 6, 21}

    def test_picker_hides_lines_whose_balance_rounds_to_zero(self):
        """bal_qty is a DOUBLE computed in the view, so a fully-issued line lands
        on residue (1.43e-06), not 0 — a bare `> 0` showed 11 exhausted sls lines
        in the picker rendering as "0.00"."""
        for builder in (
            get_jute_stock_outstanding_query,
            get_jute_stock_outstanding_by_item_query,
        ):
            sql = " ".join(str(builder()).split())
            assert "ROUND(vso.bal_qty, 2) > 0" in sql, builder.__name__
            assert "vso.bal_qty > 0" not in sql, builder.__name__

    def test_waste_picker_hides_the_same_residue(self):
        """Waste is kg-only, so its ledger is bal_weight — same float trap."""
        sql = " ".join(str(get_jute_waste_stock_outstanding_query()).split())
        assert "ROUND(v.bal_weight, 2) > 0" in sql
        assert "v.bal_weight > 0" not in sql

    def test_both_picker_queries_filter_on_the_issuable_tuple(self):
        expected = "vso.mr_status_id IN ({})".format(
            ", ".join(str(s) for s in JUTE_MR_ISSUABLE_STATUSES)
        )
        for builder in (
            get_jute_stock_outstanding_query,
            get_jute_stock_outstanding_by_item_query,
        ):
            assert expected in " ".join(str(builder()).split()), builder.__name__

    def test_both_picker_queries_leave_purchased_waste_to_the_grouped_row(self):
        """Owner ruling 2026-09-25 (D4): a type-3 (jute waste) MR line is
        offered ONLY inside its item's grouped waste row, never per MR line."""
        for builder in (
            get_jute_stock_outstanding_query,
            get_jute_stock_outstanding_by_item_query,
        ):
            sql = " ".join(str(builder()).split())
            assert "AND COALESCE(ig.item_type_id, 0) <> 3" in sql, builder.__name__


class TestPurchasedWasteThroughTheMrGuard:
    """A purchased-waste issue row is keyed by jute_mr_li_id, so an edit takes
    the MR branch of update_issue and the FIFO allocator prices its MR slices
    through _resolve_mr_stock_for_issue with quantity 0 (kg-only stock)."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {
            "user_id": 1,
            "sub": "test_user",
            "exp": 9999999999,
        }
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_db(self):
        return self._mock_session

    def test_quantity_zero_skips_the_bale_warning_and_keeps_whole_kg(self, mock_db):
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=300.0, rate=900.0, uom="KG")
        )
        rate, warnings, weight = _resolve_mr_stock_for_issue(
            mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=143.5
        )
        assert rate == 900.0
        assert warnings == []  # bal_qty 0 would warn for any positive quantity
        assert weight == 144  # round_kg, half-up

    def test_quantity_zero_still_blocks_a_weight_over_draw(self, mock_db):
        from fastapi import HTTPException
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=100.0, uom="KG")
        )
        with pytest.raises(HTTPException) as exc:
            _resolve_mr_stock_for_issue(
                mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=101
            )
        assert exc.value.status_code == 400

    def test_editing_a_purchased_waste_row_goes_through_the_mr_guard(self, mock_db):
        """existing.waste_entry_id is NULL and jute_mr_li_id set -> MR branch:
        balance read from vw_jute_stock_outstanding for that line, value
        repriced from the MR line's actual_rate.

        The line is booked the way sls books it — 108 BALES against 10,042 kg
        — so its bale balance is already -392 after one 500 kg slice; the
        edit must not warn about it (kg-only stock has no bale ledger)."""
        existing = _existing_row(quantity=100.0, weight=100.0)
        existing.jute_mr_li_id = 777
        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(
                bal_qty=-392.0, bal_weight=200.0, rate=900.0, uom="BALE", is_waste=1
            ),
            existing=existing,
            prior=SimpleNamespace(quantity=100.0, weight=100.0),
        )
        response = client.put(
            "/api/juteIssue/update_issue/1?co_id=2", json={"quantity": 120.0, "weight": 120.0}
        )
        assert response.status_code == 200, response.text
        assert response.json()["warnings"] == []
        guard_calls = [
            c for c in mock_db.execute.call_args_list if "FROM jute_mr_li jml" in str(c[0][0])
        ]
        assert [c[0][1] for c in guard_calls] == [{"jute_mr_li_id": 777, "co_id": 2}]
        update = [
            c for c in mock_db.execute.call_args_list if "UPDATE jute_issue SET" in str(c[0][0])
        ][0][0][1]
        assert update["issue_value"] == pytest.approx(1.2 * 900.0)
        assert update["weight"] == 120
        assert "jute_waste_rate_mst" not in " ".join(
            str(c[0][0]) for c in mock_db.execute.call_args_list
        )

    def test_a_waste_line_ignores_its_bale_ledger(self, mock_db):
        """sls line 45659: 108 bales / 10,042 kg, one 500 kg slice already out,
        so the view's bal_qty reads -392. A further 500 kg (quantity 500 kg,
        as the grouped picker fills it) must pass without the
        'exceeds available balance quantity (108 BALE)' warning."""
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=-392.0, bal_weight=9542.0, rate=13500.0,
                                 uom="BALE", is_waste=1)
        )
        rate, warnings, weight = _resolve_mr_stock_for_issue(
            mock_db, 45659, co_id=2, branch_id=BRANCH, quantity=500, weight=500
        )
        assert (rate, warnings, weight) == (13500.0, [], 500)

    def test_a_raw_jute_line_still_warns_on_its_bale_ledger(self, mock_db):
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=-392.0, bal_weight=9542.0, rate=13500.0,
                                 uom="BALE", is_waste=0)
        )
        _, warnings, _ = _resolve_mr_stock_for_issue(
            mock_db, 45659, co_id=2, branch_id=BRANCH, quantity=500, weight=500
        )
        assert warnings and "BALE" in warnings[0]

    @pytest.mark.parametrize("bal", [2.9, 0.9, 207.6])
    def test_a_draw_that_closes_a_waste_line_keeps_the_exact_fraction(self, mock_db, bal):
        """The 2021-2025 sls stubs (2.9 kg, 0.9 kg, ...) sit at the head of
        FIFO. Rounding 2.9 to 3 refuses the very line that holds it; rounding
        0.9 to 1 does the same — so a closing draw is exact."""
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=bal, rate=900.0, uom="KG", is_waste=1)
        )
        _, warnings, weight = _resolve_mr_stock_for_issue(
            mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=bal
        )
        assert weight == bal and warnings == []

    def test_a_partial_draw_on_a_waste_line_is_still_whole_kg(self, mock_db):
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=300.0, rate=900.0, uom="KG", is_waste=1)
        )
        _, _, weight = _resolve_mr_stock_for_issue(
            mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=143.5
        )
        assert weight == 144

    def test_a_raw_jute_line_with_quantity_zero_still_rounds_its_closing_draw(self, mock_db):
        """The exact-closing rule is for kg-only waste; a type-2 line keeps the
        whole-kg rule (2.9 -> 3 > 2.9 -> refused), as before."""
        from fastapi import HTTPException
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=2.9, rate=900.0, uom="KG", is_waste=0)
        )
        with pytest.raises(HTTPException):
            _resolve_mr_stock_for_issue(
                mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=2.9
            )

    def test_a_waste_line_without_actual_rate_is_priced_at_its_qc_rate(self, mock_db):
        """314 of the 330 open sls waste lines have no actual_rate (the QC
        insert writes `rate` only) — and FIFO drains exactly those first."""
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        for actual_rate in (None, 0):
            mock_db.execute.side_effect = _router(
                stock_row=_stock_row(bal_qty=0.0, bal_weight=500.0, rate=actual_rate,
                                     line_rate=13500.0, uom="KG", is_waste=1)
            )
            rate, _, _ = _resolve_mr_stock_for_issue(
                mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=100
            )
            assert rate == 13500.0

    def test_a_waste_line_with_an_actual_rate_keeps_it(self, mock_db):
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=0.0, bal_weight=500.0, rate=14000.0,
                                 line_rate=13500.0, uom="KG", is_waste=1)
        )
        rate, _, _ = _resolve_mr_stock_for_issue(
            mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=0, weight=100
        )
        assert rate == 14000.0

    def test_a_raw_jute_line_without_actual_rate_is_not_repriced(self, mock_db):
        """Explicit decision: the fallback is for purchased waste only. A
        type-2 line with no actual_rate still issues at 0, as it always has."""
        from src.juteProcurement.issue import _resolve_mr_stock_for_issue

        mock_db.execute.side_effect = _router(
            stock_row=_stock_row(bal_qty=10.0, bal_weight=500.0, rate=None,
                                 line_rate=13500.0, uom="BALE", is_waste=0)
        )
        rate, _, _ = _resolve_mr_stock_for_issue(
            mock_db, 24143, co_id=2, branch_id=BRANCH, quantity=1, weight=100
        )
        assert rate == 0.0
