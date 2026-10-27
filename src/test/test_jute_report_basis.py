"""
Report basis (actual|accounts) — decision D5, 2026-09-03.

Actual = today's reports (unchanged). Accounts = receipt on jute_mr_date,
approved (accepted) weight, value = accepted x rate/100, issues dated before
the MR date shifted to the MR date. Covers the Stock report (E1), the Batch
Cost report fix + basis (E2), and the MR Wise Issue Detail reconciliation
columns (E3).
"""
from src.juteProcurement import reportQueries as rq


def test_stock_accounts_basis_uses_mr_date_and_accepted_weight():
    sql = str(rq.get_jute_stock_report_query(basis="accounts"))
    assert "GREATEST(ji.issue_date, jm.jute_mr_date)" in sql
    assert "COALESCE(mrli.accepted_weight, mrli.actual_weight, 0)" in sql
    assert "(COALESCE(mrli.accepted_weight, mrli.actual_weight, 0) / 100) * COALESCE(mrli.rate, 0)" in sql


def test_stock_accounts_basis_never_reads_null_accepted_weight_as_zero():
    """TKT-000155: accepted_weight is NULL on every pre-FY26-27 sls line. Reading
    it as 0 booked no receipt while the issue side still drew the full weight,
    so the Accounts stock report went negative on 120+ items."""
    sql = str(rq.get_jute_stock_report_query(basis="accounts"))
    assert "COALESCE(mrli.accepted_weight, 0)" not in sql


def test_stock_accounts_basis_books_issues_at_approved_weight():
    """TKT-000155: issues are drawn at net weight but the books hold approved
    weight, so a fully issued line with a claim closed negative by the claim.
    Accounts basis scales the MR-backed issue by approved/net; the waste arm
    (no MR link) is untouched."""
    sql = str(rq.get_jute_stock_report_query(basis="accounts"))
    scaled = (
        "COALESCE(ji.weight, 0)"
        " * COALESCE(COALESCE(mrli.accepted_weight, mrli.actual_weight, 0)"
        " / NULLIF(mrli.actual_weight, 0), 1) AS wt"
    )
    # once per period CTE: before / on / mtd
    assert sql.count(scaled) == 3


def test_stock_actual_basis_unchanged():
    sql = str(rq.get_jute_stock_report_query())
    assert "GREATEST(" not in sql and "COALESCE(mrli.actual_weight, 0)" in sql
    assert "NULLIF(mrli.actual_weight, 0)" not in sql


def test_batch_cost_counts_draft_issues_and_weighted_rate():
    sql = str(rq.get_batch_cost_report_query())
    flat = " ".join(sql.split())
    day = "ji.d = :report_date"
    assert "ji.status_id IN (1, 3)" not in sql
    # Day rate: weighted over the report date's issue lines (TKT-000213 moved it
    # into a day bucket of the month's rows; the formula itself is unchanged).
    assert (
        f"SUM(CASE WHEN {day} THEN COALESCE(ji.issue_value, 0) END)"
        f" / SUM(CASE WHEN {day} THEN ji.weight END) * 100"
    ) in flat
    assert "AVG(" not in sql
    assert "'(unassigned)'" in sql


def test_batch_cost_accounts_basis_shifts_issue_date():
    sql = str(rq.get_batch_cost_report_query(basis="accounts"))
    assert "GREATEST(ji.issue_date, jm.jute_mr_date)" in sql


def test_mr_issue_detail_has_reconciliation_columns():
    sql = str(rq.get_jute_mr_issue_detail_query(has_mr_filter=True, has_date_filter=False))
    for col in ("accepted_weight", "unit_weight", "total_issued_weight", "expected_issued_weight", "weight_diff"):
        assert f"AS {col}" in sql
