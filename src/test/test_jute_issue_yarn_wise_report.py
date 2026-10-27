"""
Jute Issue — Yarn Quality Wise report (TKT-000214): GET /api/juteReports/issue-yarn-wise.

"Total jute issue in kg or MT for a particular date and the month-to-date
figure, against the yarn qualities."

What is locked down here:

1. The report reads the SAME issue legs as the Jute Stock report and the
   winding DPR (`_issue_rows`), so its grand total cannot drift from theirs —
   checked structurally and by executing both on one in-memory ledger.
2. Splitting `_issue_cte` into `_issue_rows` + a GROUP BY must not change the
   SQL the existing balance reports run.
3. Day vs month-to-date bucketing, the '(unassigned)' bucket, and which issues
   count (rejected out, Draft in, waste re-issues in).
4. Raw jute vs wastage: every figure carries its wastage part (a process-waste
   re-issue, or an MR line of a type-3 item — purchased waste), so the page can
   exclude wastage; raw + wastage is always the total.
5. The endpoint derives the month start itself and never lets a malformed date
   reach the database or SQL text reach the client.

No real DB: SQL is executed on SQLite, the handler is called directly.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mysql

from src.juteProcurement.constants import (
    JUTE_ISSUE_EXCLUDED_STATUS,
    JUTE_MR_LI_EXCLUDED_STATUSES,
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    JUTE_WASTE_ITEM_TYPE_ID,
)
from src.juteProcurement.reportQueries import (
    _issue_cte,
    _issue_rows,
    get_jute_issue_yarn_wise_query,
)
from src.juteProcurement.reports import get_jute_issue_yarn_wise_report

MTD = "{d} BETWEEN :month_start AND :report_date"
BINDS = {"branch_id": 29, "month_start": "2026-10-01", "report_date": "2026-10-04"}


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------


def test_query_compiles_as_mysql_with_only_the_router_params():
    compiled = get_jute_issue_yarn_wise_query().compile(dialect=mysql.dialect())
    assert set(compiled.params) == {"branch_id", "month_start", "report_date"}


def test_report_is_built_on_the_shared_issue_legs():
    """Same eligibility as the stock report — not a second copy of the rules."""
    sql = str(get_jute_issue_yarn_wise_query())
    assert _issue_rows(MTD, with_dims=True) in sql

    mr_rule = f"jm.status_id NOT IN ({', '.join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)})"
    li_rule = f"mrli.status NOT IN ({', '.join(repr(s) for s in JUTE_MR_LI_EXCLUDED_STATUSES)})"
    assert mr_rule in sql and li_rule in sql and "mrli.active = 1" in sql
    # Rejected issues are out on both legs; Draft/Open/Approved all count.
    assert sql.count(f"ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}") == 2
    assert "ji.status_id IN (1, 3)" not in sql
    # Waste re-issues are part of "total jute issue".
    assert "ji.waste_entry_id IS NOT NULL" in sql


def test_missing_yarn_quality_is_a_named_bucket_not_a_dropped_row():
    sql = str(get_jute_issue_yarn_wise_query())
    assert "LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = src.yarn_type_id" in sql
    assert "COALESCE(yim.item_name, jym.jute_yarn_name, '(unassigned)') AS yarn_type_name" in sql


def test_existing_issue_cte_sql_is_untouched_by_the_split():
    """The balance reports must not start selecting the yarn/date columns."""
    for basis in ("actual", "accounts"):
        cte = _issue_cte("{d} <= :report_date", basis)
        assert "yarn_type_id" not in cte
        assert "AS issue_date" not in cte
        assert "is_waste" not in cte
        assert _issue_rows("{d} <= :report_date", basis) in cte
        assert " ".join(cte.split()).endswith(") src GROUP BY src.item_id")


def test_dims_carry_the_date_the_period_filter_tests():
    """accounts basis shifts an issue to its MR date — the emitted issue_date
    must be that shifted date, or day and MTD would bucket differently."""
    rows = _issue_rows("{d} <= :report_date", "accounts", with_dims=True)
    mr_arm, waste_arm = rows.split("UNION ALL")
    assert "GREATEST(ji.issue_date, jm.jute_mr_date) AS issue_date" in mr_arm
    assert "GREATEST(ji.issue_date, jm.jute_mr_date) <= :report_date" in mr_arm
    assert "ji.issue_date AS issue_date" in waste_arm
    assert rows.count("ji.yarn_type_id AS yarn_type_id") == 2


def test_wastage_is_the_waste_leg_plus_mr_lines_of_a_waste_item():
    """Same line as the Jute Stock report's is_waste and the wastage report's
    'raw jute issued': the item GROUP's type, never a name match — and a
    re-issue of process waste is wastage whatever it is called."""
    assert JUTE_WASTE_ITEM_TYPE_ID == 3
    mr_arm, waste_arm = _issue_rows(MTD, with_dims=True).split("UNION ALL")
    assert f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste" in mr_arm
    assert "1 AS is_waste" in waste_arm
    assert "item_grp_name" not in mr_arm and "LIKE" not in mr_arm

    sql = str(get_jute_issue_yarn_wise_query())
    assert (
        "SUM(CASE WHEN src.is_waste = 1 AND src.issue_date = :report_date THEN src.wt ELSE 0 END), 3) AS day_waste_kg"
        in sql
    )
    assert "SUM(CASE WHEN src.is_waste = 1 THEN src.wt ELSE 0 END), 3) AS mtd_waste_kg" in sql
    # The totals stay un-filtered: wastage is reported beside them, not taken out.
    assert "ROUND(SUM(src.wt), 3) AS mtd_issue_kg" in sql


# ---------------------------------------------------------------------------
# Executed on an in-memory ledger
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger():
    """Branch 29, report date 2026-10-04 (month start 2026-10-01).

    Yarn 28 '13-SKWP' (named through item_mst) and yarn 29 '16-SKWP' (name only
    on jute_yarn_mst). MR 1 is approved; MR 2 is a Draft MR (not stock-eligible).
    Item 30 CADDIES sits in a type-3 (jute waste) group: line 13 is PURCHASED
    waste on MR 1, waste entry 101 is process waste booked on the floor.
    """
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE jute_mr (jute_mr_id INT, branch_id INT, out_date TEXT, "
                       "jute_mr_date TEXT, jute_gate_entry_date TEXT, status_id INT)"))
        c.execute(text("CREATE TABLE jute_mr_li (jute_mr_li_id INT, jute_mr_id INT, actual_item_id INT, "
                       "actual_qty REAL, actual_weight REAL, accepted_weight REAL, rate REAL, "
                       "total_price REAL, active INT, status TEXT)"))
        c.execute(text("CREATE TABLE item_mst (item_id INT, item_grp_id INT, item_name TEXT)"))
        c.execute(text("CREATE TABLE item_grp_mst (item_grp_id INT, item_type_id INT)"))
        c.execute(text("CREATE TABLE jute_prod_waste_entry (waste_entry_id INT, item_id INT, "
                       "waste_qty REAL, dept_id INT, branch_id INT, tran_date TEXT, active INT)"))
        c.execute(text("CREATE TABLE jute_yarn_mst (jute_yarn_id INT, jute_yarn_name TEXT, item_id INT)"))
        c.execute(text("CREATE TABLE jute_issue (jute_issue_id INT, branch_id INT, issue_date TEXT, "
                       "jute_mr_li_id INT, waste_entry_id INT, item_id INT, quantity REAL, "
                       "weight REAL, issue_value REAL, status_id INT, yarn_type_id INT)"))
        c.execute(text("INSERT INTO item_grp_mst VALUES (11, 2), (42, 3), (60, 5)"))
        c.execute(text("INSERT INTO item_mst VALUES (10, 11, 'TD-5'), (30, 42, 'CADDIES'), "
                       "(335563, 60, '13-SKWP')"))
        c.execute(text("INSERT INTO jute_yarn_mst VALUES (28, 'old 13 name', 335563), (29, '16-SKWP', NULL)"))
        c.execute(text("INSERT INTO jute_mr VALUES (1, 29, '2026-09-20', '2026-09-20', '2026-09-20', 3), "
                       "(2, 29, '2026-10-01', '2026-10-01', '2026-10-01', 21)"))
        c.execute(text("INSERT INTO jute_mr_li VALUES (11, 1, 10, 200, 30000, 30000, 5000, 1500000, 1, NULL), "
                       "(12, 1, 10, 50, 7000, 7000, 5000, 350000, 1, '6'), "
                       "(13, 1, 30, 40, 4000, 4000, 1500, 60000, 1, NULL), "
                       "(21, 2, 10, 50, 7000, 7000, 5000, 350000, 1, NULL)"))
        c.execute(text("INSERT INTO jute_prod_waste_entry VALUES (101, 30, 300, 7, 29, '2026-10-02', 1)"))
        issues = [
            # id, br, date, mr_li, waste, item, qty, kg, value, status, yarn
            (1, 29, "2026-10-04", 11, None, 10, 10, 1500, 75000, 21, 28),    # day, Draft -> counts
            (2, 29, "2026-10-04", 11, None, 10, 4, 600, 30000, 3, 29),       # day, other yarn
            (3, 29, "2026-10-01", 11, None, 10, 20, 3000, 150000, 1, 28),    # earlier this month
            (4, 29, "2026-10-03", None, 101, 30, 250, 250, 0, 21, 28),       # waste re-issue, this month
            (5, 29, "2026-10-04", 11, None, 10, 1, 150, 7500, 4, 28),        # REJECTED -> out
            (6, 29, "2026-09-30", 11, None, 10, 5, 750, 37500, 3, 28),       # last month -> out
            (7, 29, "2026-10-05", 11, None, 10, 5, 750, 37500, 3, 28),       # after the report date -> out
            (8, 29, "2026-10-02", 11, None, 10, 2, 300, 15000, 3, None),     # no yarn quality
            (9, 29, "2026-10-04", 21, None, 10, 3, 450, 22500, 3, 28),       # Draft MR -> not eligible
            (10, 29, "2026-10-04", 12, None, 10, 3, 450, 22500, 3, 28),      # cancelled MR line -> out
            (11, 87, "2026-10-04", 11, None, 10, 9, 999, 49950, 3, 28),      # another branch
            (12, 29, "2026-10-04", 13, None, 30, 120, 120, 1800, 3, 29),     # purchased waste: MR line of a type-3 item
        ]
        for row in issues:
            c.execute(
                text("INSERT INTO jute_issue VALUES (:a, :b, :c, :d, :e, :f, :g, :h, :i, :j, :k)"),
                dict(zip("abcdefghijk", row)),
            )
    return eng


def _report(ledger, **binds):
    with ledger.connect() as c:
        return [dict(r._mapping) for r in c.execute(get_jute_issue_yarn_wise_query(), {**BINDS, **binds})]


def test_executed_day_and_month_to_date_per_yarn_quality(ledger):
    assert _report(ledger) == [
        {"yarn_type_id": None, "yarn_type_name": "(unassigned)", "day_issue_kg": 0.0, "mtd_issue_kg": 300.0,
         "day_waste_kg": 0.0, "mtd_waste_kg": 0.0},
        # 13-SKWP: day = 1,500 (Draft counts; rejected / Draft-MR / cancelled-line rows do not);
        #          MTD = 1,500 + 3,000 on the 1st + 250 waste re-issue on the 3rd.
        #          Wastage: the 250 kg re-issue of process waste — in the month, not on the day.
        {"yarn_type_id": 28, "yarn_type_name": "13-SKWP", "day_issue_kg": 1500.0, "mtd_issue_kg": 4750.0,
         "day_waste_kg": 0.0, "mtd_waste_kg": 250.0},
        # 16-SKWP: 600 raw jute + 120 purchased waste (an MR line of a type-3 item), both on the day.
        {"yarn_type_id": 29, "yarn_type_name": "16-SKWP", "day_issue_kg": 720.0, "mtd_issue_kg": 720.0,
         "day_waste_kg": 120.0, "mtd_waste_kg": 120.0},
    ]


def test_executed_raw_jute_is_the_total_less_wastage_for_every_quality(ledger):
    rows = _report(ledger)
    raw = {r["yarn_type_id"]: (r["day_issue_kg"] - r["day_waste_kg"], r["mtd_issue_kg"] - r["mtd_waste_kg"]) for r in rows}
    assert raw == {None: (0.0, 300.0), 28: (1500.0, 4500.0), 29: (600.0, 600.0)}
    for r in rows:
        assert 0 <= r["day_waste_kg"] <= r["day_issue_kg"]
        assert 0 <= r["mtd_waste_kg"] <= r["mtd_issue_kg"]
        assert r["day_waste_kg"] <= r["mtd_waste_kg"]


def test_executed_wastage_only_counts_issues_that_are_in_the_total(ledger):
    """A rejected or out-of-window waste issue is in neither figure."""
    with ledger.begin() as c:
        c.execute(text("INSERT INTO jute_issue VALUES (20, 29, '2026-10-04', NULL, 101, 30, 10, 10, 0, 4, 28)"))    # rejected
        c.execute(text("INSERT INTO jute_issue VALUES (21, 29, '2026-09-30', NULL, 101, 30, 10, 10, 0, 3, 28)"))    # last month
    rows = {r["yarn_type_id"]: r for r in _report(ledger)}
    assert (rows[28]["day_waste_kg"], rows[28]["mtd_waste_kg"]) == (0.0, 250.0)
    assert rows[28]["mtd_issue_kg"] == 4750.0


def test_executed_yarn_name_prefers_the_item_master_over_the_yarn_master(ledger):
    names = {r["yarn_type_id"]: r["yarn_type_name"] for r in _report(ledger)}
    assert names[28] == "13-SKWP"      # item_mst.item_name wins over 'old 13 name'
    assert names[29] == "16-SKWP"      # no item link -> jute_yarn_mst.jute_yarn_name


def test_executed_quality_with_no_issue_on_the_day_still_shows_its_month_figure(ledger):
    rows = {r["yarn_type_id"]: r for r in _report(ledger, report_date="2026-10-02")}
    # On the 2nd: only the 1st (3,000 kg, yarn 28) and the 2nd (300 kg, no yarn) exist so far.
    assert rows[28] == {"yarn_type_id": 28, "yarn_type_name": "13-SKWP", "day_issue_kg": 0.0, "mtd_issue_kg": 3000.0,
                        "day_waste_kg": 0.0, "mtd_waste_kg": 0.0}
    assert rows[None]["day_issue_kg"] == 300.0
    assert 29 not in rows, "a quality with nothing issued in the month so far has no row"


def test_executed_first_of_month_has_day_equal_to_month_to_date(ledger):
    rows = _report(ledger, report_date="2026-10-01")
    assert rows == [
        {"yarn_type_id": 28, "yarn_type_name": "13-SKWP", "day_issue_kg": 3000.0, "mtd_issue_kg": 3000.0,
         "day_waste_kg": 0.0, "mtd_waste_kg": 0.0},
    ]


def test_executed_month_window_starts_where_the_router_says(ledger):
    """A wider window pulls in the 30 Sep issue — month_start is honoured, not assumed."""
    rows = {r["yarn_type_id"]: r for r in _report(ledger, month_start="2026-09-01")}
    assert rows[28]["mtd_issue_kg"] == 4750.0 + 750.0
    assert rows[28]["day_issue_kg"] == 1500.0


def test_executed_grand_total_equals_the_stock_reports_issue_legs(ledger):
    """The yarn split and the item split are two groupings of one set of rows."""
    with ledger.connect() as c:
        by_item_mtd = c.execute(text(f"SELECT SUM(wt) FROM ({_issue_cte(MTD)}) x"), BINDS).scalar()
        by_item_day = c.execute(
            text(f"SELECT SUM(wt) FROM ({_issue_cte('{d} = :report_date')}) x"), BINDS
        ).scalar()
    rows = _report(ledger)
    assert sum(r["mtd_issue_kg"] for r in rows) == pytest.approx(by_item_mtd) == pytest.approx(5770.0)
    assert sum(r["day_issue_kg"] for r in rows) == pytest.approx(by_item_day) == pytest.approx(2220.0)
    # ...and the wastage part is the type-3 item's share of those same legs
    # (item 30 CADDIES: 250 process waste + 120 purchased waste).
    with ledger.connect() as c:
        waste_item_mtd = c.execute(text(f"SELECT wt FROM ({_issue_cte(MTD)}) x WHERE item_id = 30"), BINDS).scalar()
    assert sum(r["mtd_waste_kg"] for r in rows) == pytest.approx(waste_item_mtd) == pytest.approx(370.0)


def test_executed_other_branch_sees_only_its_own_issues(ledger):
    assert _report(ledger, branch_id=87) == [
        {"yarn_type_id": 28, "yarn_type_name": "13-SKWP", "day_issue_kg": 999.0, "mtd_issue_kg": 999.0,
         "day_waste_kg": 0.0, "mtd_waste_kg": 0.0},
    ]
    assert _report(ledger, branch_id=4) == []


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

SQLA_STYLE_ERROR = (
    '(pymysql.err.OperationalError) (1525, "Incorrect DATE value: \'x\'")\n'
    "[SQL: SELECT src.yarn_type_id AS yarn_type_id ...]"
)


def _request(**params):
    return SimpleNamespace(query_params=params)


def _explode(*_args, **_kwargs):
    raise RuntimeError(SQLA_STYLE_ERROR)


class _RecordingDb:
    """Stands in for the tenant session: records the binds, returns canned rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.binds = None

    def execute(self, _query, binds):
        self.binds = binds
        return SimpleNamespace(fetchall=lambda: [SimpleNamespace(_mapping=r) for r in self.rows])


def test_endpoint_derives_the_month_start_and_returns_the_rows():
    row = {"yarn_type_id": 28, "yarn_type_name": "13-SKWP", "day_issue_kg": 7869.0, "mtd_issue_kg": 23657.0,
           "day_waste_kg": 0.0, "mtd_waste_kg": 0.0}
    db = _RecordingDb([row])
    out = get_jute_issue_yarn_wise_report(_request(branch_id="29", date="2026-10-04"), db=db, token_data={})
    assert db.binds == {"branch_id": 29, "month_start": "2026-10-01", "report_date": "2026-10-04"}
    assert out == {"data": [row], "month_start": "2026-10-01", "report_date": "2026-10-04"}


@pytest.mark.parametrize(
    "given,month_start,report_date",
    [
        ("2026-10-01", "2026-10-01", "2026-10-01"),   # the 1st: window is that one day
        ("2026-03-31", "2026-03-01", "2026-03-31"),
        ("2024-02-29", "2024-02-01", "2024-02-29"),   # leap day
        ("2026-1-5", "2026-01-01", "2026-01-05"),     # unpadded input is normalised, not string-sliced
    ],
)
def test_endpoint_month_window(given, month_start, report_date):
    db = _RecordingDb()
    out = get_jute_issue_yarn_wise_report(_request(branch_id="1", date=given), db=db, token_data={})
    assert (db.binds["month_start"], db.binds["report_date"]) == (month_start, report_date)
    assert out["data"] == []


@pytest.mark.parametrize("bad_date", ["04-10-2026", "not-a-date", "2026-13-45", "2026-02-30"])
def test_endpoint_malformed_date_is_400_and_never_reaches_the_db(bad_date):
    with pytest.raises(HTTPException) as exc:
        get_jute_issue_yarn_wise_report(
            _request(branch_id="1", date=bad_date), db=SimpleNamespace(execute=_explode), token_data={}
        )
    assert exc.value.status_code == 400
    assert "YYYY-MM-DD" in exc.value.detail


@pytest.mark.parametrize(
    "params",
    [{"date": "2026-10-04"}, {"branch_id": "29"}, {"branch_id": "29", "date": ""}, {"branch_id": "x", "date": "2026-10-04"}],
)
def test_endpoint_missing_or_bad_params_are_400(params):
    with pytest.raises(HTTPException) as exc:
        get_jute_issue_yarn_wise_report(_request(**params), db=SimpleNamespace(execute=_explode), token_data={})
    assert exc.value.status_code == 400


def test_endpoint_db_errors_do_not_leak_sql_to_the_client():
    with pytest.raises(HTTPException) as exc:
        get_jute_issue_yarn_wise_report(
            _request(branch_id="29", date="2026-10-04"), db=SimpleNamespace(execute=_explode), token_data={}
        )
    assert exc.value.status_code == 500
    assert "SQL" not in str(exc.value.detail) and "SELECT" not in str(exc.value.detail)
