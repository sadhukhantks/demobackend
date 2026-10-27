"""
Batch Cost Report — month-to-date columns (TKT-000213, owner rulings 2026-10-05).

"MTD figures required for actual weight and rate."

What is locked down here:

1. Day columns keep their meaning: only the report date's issues and plan.
2. MTD = 1st of the report date's month through the report date, inclusive,
   with the day's eligibility (rejected out, Draft in, item fallback to the MR
   line's actual item, branch-scoped).
3. MTD planned is computed per day (that day's assignment % x that day's yarn
   total) and then summed; MTD rate is MTD value / MTD weight x 100 — weighted,
   never an average of daily rates.
4. Rows: every yarn type x quality issued or planned on any day of the month;
   MTD weight totals equal the month's eligible issue.
5. Accounts basis: the month range applies to the shifted (MR) date, and the
   value comes from the MR line's rate.
6. The endpoint derives month_start itself.

No real DB: the query runs on SQLite, the handler is called directly.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import mysql

from src.juteProcurement.reportQueries import get_batch_cost_report_query
from src.juteProcurement.reports import get_batch_cost_report

BRANCH = 29
REPORT_DATE = "2026-10-04"
MONTH_START = "2026-10-01"

# Yarn types: 1 = HESSIAN WARP (named via its item), 2 = SACKING WEFT (own name).
# Qualities: 101 TD4, 102 TD5, 103 MESTA.
#
# MR lines (jute_mr_li_id, jute_mr_id, actual_item_id, rate) and MR dates:
MR_LINES = [
    (1, 1, 101, 5000.0),  # MR 1, dated 2026-09-20
    (2, 1, 102, 5000.0),
    (3, 2, 103, 4000.0),  # MR 2, dated 2026-09-25
    (4, 3, 101, 5200.0),  # MR 3, dated 2026-10-02 -> shifts a 09-29 issue INTO Oct
    (5, 4, 102, 5000.0),  # MR 4, dated 2026-10-06 -> shifts a 10-04 issue OUT
]
MRS = [(1, "2026-09-20"), (2, "2026-09-25"), (3, "2026-10-02"), (4, "2026-10-06")]

# Issues: (id, branch, date, status, yarn, item_id, mr_li, weight kg, issue_value)
ISSUES = [
    # yarn 1 -------------------------------------------------------------
    (1, 29, "2026-10-01", 1, 1, 101, 1, 600.0, 30000.0),
    (2, 29, "2026-10-01", 1, 1, 102, 2, 400.0, 0.0),  # value never stamped
    (3, 29, "2026-10-02", 1, 1, 101, 1, 300.0, 15600.0),
    (4, 29, "2026-10-02", 1, 1, None, 3, 700.0, 28000.0),  # item from MR line -> 103
    (5, 29, "2026-10-03", 21, 1, 101, 1, 100.0, 5000.0),  # Draft still counts
    (6, 29, "2026-10-04", 1, 1, 101, 1, 500.0, 26000.0),
    (7, 29, "2026-10-04", 1, 1, 102, 2, 300.0, 15000.0),
    # never counted --------------------------------------------------------
    (8, 29, "2026-10-04", 4, 1, 101, 1, 999.0, 1.0),  # rejected
    (9, 29, "2026-09-30", 1, 1, 101, 1, 777.0, 1.0),  # previous month
    (10, 29, "2026-10-05", 1, 1, 101, 1, 888.0, 1.0),  # after the report date
    (11, 30, "2026-10-04", 1, 1, 101, 1, 555.0, 1.0),  # other branch
    # no yarn quality -> '(unassigned)' ----------------------------------
    (12, 29, "2026-10-02", 1, None, 101, 1, 50.0, 2500.0),
    # yarn 2: the two accounts-basis date shifts -----------------------------
    (13, 29, "2026-09-29", 1, 2, 101, 4, 200.0, 0.0),  # accounts: -> 10-02 (in)
    (14, 29, "2026-10-04", 1, 2, 102, 5, 100.0, 5000.0),  # accounts: -> 10-06 (out)
]

# Batch plans: A = 101 60% / 102 40%; B = 101 50% / 103 50%.
PLAN_LINES = [(1, 101, 60.0), (1, 102, 40.0), (2, 101, 50.0), (2, 103, 50.0)]
# Assignments: (branch, date, yarn, plan, status)
ASSIGNS = [
    (29, "2026-10-01", 1, 1, 3),
    (29, "2026-10-02", 1, 2, 3),
    (29, "2026-10-03", 1, 1, 21),  # Draft assignment: not a plan yet
    (29, "2026-10-04", 1, 1, 3),
    (30, "2026-10-04", 1, 2, 3),  # other branch
]


def _greatest(*args):
    """MySQL 8 GREATEST: NULL if any argument is NULL."""
    return None if any(a is None for a in args) else max(args)


def _engine(plan_lines=PLAN_LINES, assigns=ASSIGNS, issues=ISSUES):
    eng = create_engine("sqlite://")

    @event.listens_for(eng, "connect")
    def _fns(dbapi_conn, _record):
        dbapi_conn.create_function("GREATEST", -1, _greatest)

    with eng.begin() as c:
        for ddl in (
            "CREATE TABLE jute_issue (jute_issue_id INT, branch_id INT, issue_date TEXT,"
            " status_id INT, yarn_type_id INT, item_id INT, jute_mr_li_id INT,"
            " weight REAL, issue_value REAL)",
            "CREATE TABLE jute_mr_li (jute_mr_li_id INT, jute_mr_id INT,"
            " actual_item_id INT, rate REAL)",
            "CREATE TABLE jute_mr (jute_mr_id INT, jute_mr_date TEXT)",
            "CREATE TABLE jute_batch_daily_assign (branch_id INT, assign_date TEXT,"
            " jute_yarn_id INT, batch_plan_id INT, status_id INT)",
            "CREATE TABLE jute_batch_plan_li (batch_plan_id INT, jute_quality_id INT,"
            " percentage REAL)",
            "CREATE TABLE jute_yarn_mst (jute_yarn_id INT, item_id INT, jute_yarn_name TEXT)",
            "CREATE TABLE item_mst (item_id INT, item_name TEXT, item_grp_id INT)",
            "CREATE TABLE item_grp_mst (item_grp_id INT, item_grp_name TEXT, item_type_id INT)",
        ):
            c.execute(text(ddl))
        c.execute(text("INSERT INTO jute_issue VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i)"),
                  [dict(zip("abcdefghi", r)) for r in issues])
        c.execute(text("INSERT INTO jute_mr_li VALUES (:a,:b,:c,:d)"),
                  [dict(zip("abcd", r)) for r in MR_LINES])
        c.execute(text("INSERT INTO jute_mr VALUES (:a,:b)"), [dict(zip("ab", r)) for r in MRS])
        c.execute(text("INSERT INTO jute_batch_plan_li VALUES (:a,:b,:c)"),
                  [dict(zip("abc", r)) for r in plan_lines])
        c.execute(text("INSERT INTO jute_batch_daily_assign VALUES (:a,:b,:c,:d,:e)"),
                  [dict(zip("abcde", r)) for r in assigns])
        c.execute(text("INSERT INTO jute_yarn_mst VALUES (1, 901, 'warp'), (2, NULL, 'SACKING WEFT')"))
        c.execute(text(
            "INSERT INTO item_mst VALUES (901, 'HESSIAN WARP', 9), (101, 'TD4', 1),"
            " (102, 'TD5', 1), (103, 'MESTA', 2), (104, 'THREAD WASTE', 3)"
        ))
        c.execute(text("INSERT INTO item_grp_mst VALUES (1, 'JUTE', 2), (2, 'MESTA', 2), (3, 'WASTE', 3)"))
    return eng


def _run(basis="actual", report_date=REPORT_DATE, month_start=MONTH_START, **fixture):
    with _engine(**fixture).connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            get_batch_cost_report_query(basis=basis),
            {"branch_id": BRANCH, "month_start": month_start, "report_date": report_date},
        )]
    return rows, {(r["yarn_type_id"], r["item_id"]): r for r in rows}


def _check(row, **expected):
    for field, value in expected.items():
        assert row[field] == pytest.approx(value, abs=0.005), field


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", ["actual", "accounts"])
def test_query_binds_only_the_router_params(basis):
    compiled = get_batch_cost_report_query(basis=basis).compile(dialect=mysql.dialect())
    assert set(compiled.params) == {"branch_id", "month_start", "report_date"}


def test_accounts_month_range_tests_the_shifted_date():
    sql = " ".join(str(get_batch_cost_report_query(basis="accounts")).split())
    shifted = "COALESCE(GREATEST(ji.issue_date, jm.jute_mr_date), ji.issue_date)"
    assert f"{shifted} BETWEEN :month_start AND :report_date" in sql


# ---------------------------------------------------------------------------
# Actual basis
# ---------------------------------------------------------------------------


def test_day_columns_cover_only_the_report_date():
    _, rows = _run()
    # 10-04: yarn 1 issued 800 kg under plan A (60/40).
    _check(rows[(1, 101)], planned_weight=480, actual_weight=500, actual_rate=5200,
           issue_value=26000, variance=20)
    _check(rows[(1, 102)], planned_weight=320, actual_weight=300, actual_rate=5000,
           issue_value=15000, variance=-20)
    _check(rows[(2, 102)], planned_weight=0, actual_weight=100, actual_rate=5000,
           issue_value=5000, variance=100)


def test_mtd_actual_weight_value_and_weighted_rate():
    _, rows = _run()
    # 600 + 300 + 100 (Draft) + 500; the rejected / Sept / 10-05 / br 30 lines are out.
    _check(rows[(1, 101)], mtd_actual_weight=1500, mtd_issue_value=76600,
           mtd_actual_rate=5106.67)
    # Weighted, not the mean of the daily rates (5000, 5200, 5000, 5200 -> 5100).
    assert rows[(1, 101)]["mtd_actual_rate"] != pytest.approx(5100, abs=0.5)
    # An unstamped (0-value) line still adds weight: 15000 / 700 kg.
    _check(rows[(1, 102)], mtd_actual_weight=700, mtd_issue_value=15000,
           mtd_actual_rate=2142.86)
    # Item NULL on the issue -> the MR line's actual item (103).
    _check(rows[(1, 103)], mtd_actual_weight=700, mtd_issue_value=28000,
           mtd_actual_rate=4000)


def test_mtd_planned_is_summed_per_day():
    _, rows = _run()
    # 101: 10-01 60% x 1000 + 10-02 50% x 1000 + 10-03 Draft (none) + 10-04 60% x 800
    _check(rows[(1, 101)], mtd_planned_weight=600 + 500 + 480, mtd_variance=1500 - 1580)
    # 102: plan A days only (10-01, 10-04)
    _check(rows[(1, 102)], mtd_planned_weight=400 + 320, mtd_variance=-20)
    # 103: plan B on 10-02 only
    _check(rows[(1, 103)], mtd_planned_weight=500, mtd_variance=200)


def test_rows_cover_the_whole_month_with_zero_day_columns():
    rows, by_key = _run()
    assert set(by_key) == {(1, 101), (1, 102), (1, 103), (None, 101), (2, 102)}
    assert len(rows) == len(by_key), "one row per yarn type x quality"
    # Nothing moved for 103 or the unassigned bucket on the report date.
    for k in ((1, 103), (None, 101)):
        _check(by_key[k], planned_weight=0, actual_weight=0, actual_rate=0,
               issue_value=0, variance=0)
    _check(by_key[(None, 101)], mtd_actual_weight=50, mtd_issue_value=2500)
    assert by_key[(None, 101)]["yarn_type_name"] == "(unassigned)"
    assert by_key[(1, 101)]["yarn_type_name"] == "HESSIAN WARP"
    assert by_key[(2, 102)]["yarn_type_name"] == "SACKING WEFT"
    assert by_key[(1, 103)]["item_name"] == "MESTA"


def test_mtd_weight_total_equals_the_months_issue():
    rows, _ = _run()
    eligible = sum(
        w for (_i, br, d, st, _y, _it, _l, w, _v) in ISSUES
        if br == BRANCH and st != 4 and MONTH_START <= d <= REPORT_DATE
    )
    assert sum(r["mtd_actual_weight"] for r in rows) == pytest.approx(eligible) == 3050
    assert sum(r["actual_weight"] for r in rows) == pytest.approx(900)


def test_first_of_month_mtd_equals_day():
    rows, _ = _run(report_date="2026-10-01")
    assert rows
    for r in rows:
        for f in ("planned_weight", "actual_weight", "actual_rate", "issue_value", "variance"):
            assert r[f"mtd_{f}"] == pytest.approx(r[f]), f


def test_quality_listed_twice_in_a_plan_is_one_row():
    """sls plan 1243 lists quality 57 twice (18% + 5%). Each line's share is
    planned, on one row — never two rows with the same key (DataGrid ids)."""
    plan_c = [(3, 101, 18.0), (3, 101, 5.0), (3, 102, 77.0)]
    assigns = [(29, REPORT_DATE, 1, 3, 3)]
    rows, by_key = _run(plan_lines=PLAN_LINES + plan_c, assigns=assigns)
    assert len(rows) == len(by_key)
    _check(by_key[(1, 101)], planned_weight=0.23 * 800, mtd_planned_weight=0.23 * 800)


# ---------------------------------------------------------------------------
# Accounts basis
# ---------------------------------------------------------------------------


def test_accounts_basis_shifts_issues_by_mr_date_and_values_at_mr_rate():
    rows, by_key = _run(basis="accounts")
    # 09-29 issue against an MR dated 10-02 moves into October ...
    _check(by_key[(2, 101)], actual_weight=0, mtd_actual_weight=200,
           mtd_issue_value=10400, mtd_actual_rate=5200)
    # ... and a 10-04 issue against an MR dated 10-06 moves out of the range.
    assert (2, 102) not in by_key
    # Value = weight/100 x MR-line rate; MTD rate = MTD value / MTD weight.
    _check(by_key[(1, 101)], actual_weight=500, issue_value=25000,
           mtd_actual_weight=1500, mtd_issue_value=75000, mtd_actual_rate=5000)
    _check(by_key[(1, 102)], mtd_actual_weight=700, mtd_issue_value=35000,
           mtd_actual_rate=5000)
    # The plan base (yarn 1's daily totals) is unchanged by the yarn-2 shifts.
    _check(by_key[(1, 101)], mtd_planned_weight=1580)
    assert sum(r["mtd_actual_weight"] for r in rows) == pytest.approx(1500 + 700 + 700 + 50 + 200)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class _CapturingDb:
    def __init__(self):
        self.params = None

    def execute(self, _query, params):
        self.params = params
        return SimpleNamespace(fetchall=lambda: [])


@pytest.mark.parametrize(
    "q_date,month_start,report_date",
    [
        ("2026-10-04", "2026-10-01", "2026-10-04"),
        ("2026-10-01", "2026-10-01", "2026-10-01"),
        ("2028-02-29", "2028-02-01", "2028-02-29"),
        ("2026-9-5", "2026-09-01", "2026-09-05"),  # strptime accepts it; normalised
    ],
)
def test_endpoint_derives_month_start(q_date, month_start, report_date):
    db = _CapturingDb()
    out = get_batch_cost_report(
        SimpleNamespace(query_params={"branch_id": "29", "date": q_date, "basis": "accounts"}),
        db=db, token_data={},
    )
    assert db.params == {"branch_id": 29, "month_start": month_start, "report_date": report_date}
    plan_status = out.pop("plan_status")  # TKT-000206, see test_jute_batch_cost_plan_coverage
    assert out == {"data": [], "month_start": month_start, "report_date": report_date}
    assert plan_status["day_has_issue"] is False


# ---------------------------------------------------------------------------
# Jute waste re-issued into a batch (owner ruling 2026-10-08, option 3)
# ---------------------------------------------------------------------------

WASTE_ISSUE = (15, 29, "2026-10-04", 1, 1, 104, None, 200.0, 0.0)  # thread waste, yarn 1


def test_waste_rows_are_flagged_and_jute_rows_are_not():
    rows, by = _run(issues=ISSUES + [WASTE_ISSUE])
    assert by[(1, 104)]["is_waste"] == 1
    _check(by[(1, 104)], actual_weight=200.0, mtd_actual_weight=200.0, issue_value=0.0)
    assert all(r["is_waste"] == 0 for r in rows if r["item_id"] != 104)


def test_waste_stays_in_the_plan_base():
    """Batch plans list waste qualities too, so a plan's % is a share of jute + waste:
    yarn 1 on 10-04 issues 800 kg jute + 200 kg waste -> TD4 planned 60% x 1000."""
    _, by = _run(issues=ISSUES + [WASTE_ISSUE])
    _check(by[(1, 101)], planned_weight=600.0)


def test_planned_only_waste_quality_is_flagged():
    _, by = _run(plan_lines=PLAN_LINES + [(1, 104, 5.0)])
    assert by[(1, 104)]["is_waste"] == 1
