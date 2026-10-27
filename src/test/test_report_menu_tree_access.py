"""Report-menu tree is role-filtered.

Reports live in menu_mst as level-3 rows (module -> reports page -> report) and
are deliberately dropped from the sidebar payload (_is_sub_sub_menu), so
/api/admin/PortalData/report_menu_tree is the only place they surface — and the
only place role access can be enforced for them.

The access rule is SQL, so it is tested as SQL: get_report_menu_tree() runs
against a throwaway in-memory SQLite tree rather than being asserted about as a
string. If the grant resolution changes, this fails on rows, not on grep.
"""

import pytest
from sqlalchemy import create_engine, text

from src.common.portal.query import get_report_menu_tree

# menu_mst: 1 module -> 2 (reports page) -> 3 report rows; 90 is a non-report
# child of the same reports page (a sub-page, must never be returned).
MENUS = [
    # menu_id, name, path, parent, report, module_mst_id, active
    (1, "Jute Procurement", "jutePurchase", None, 0, 7, 1),
    (2, "Jute Report", "jutePurchase/reports", 1, 0, 7, 1),
    (10, "MR List", "jutePurchase/reports/mrList", 2, 1, 7, 1),
    (11, "Jute Stock", "jutePurchase/reports/juteStock", 2, 1, 7, 1),
    (12, "Batch Cost", "jutePurchase/reports/batchCost", 2, 1, 7, 1),
    (13, "Retired Report", "jutePurchase/reports/retired", 2, 1, 7, 0),
    (90, "Not A Report", "jutePurchase/reports/subPage", 2, 0, 7, 1),
]

# role_menu_map: role 1 = all three reports, role 2 = one report only,
# role 3 = the reports page but no report under it, role 4 = report at level 0.
ROLE_MENUS = [
    (1, 2, 4), (1, 10, 4), (1, 11, 1), (1, 12, 2),
    (2, 2, 1), (2, 11, 1),
    (3, 2, 1),
    (4, 2, 1), (4, 10, 0),
]

# user_role_map: user_id, role_id, co_id
USER_ROLES = [(100, 1, 5), (200, 2, 5), (300, 3, 5), (400, 4, 5), (500, 1, 9)]


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text(
            "CREATE TABLE menu_mst (menu_id INTEGER, menu_name TEXT, menu_path TEXT, "
            "menu_parent_id INTEGER, report INTEGER, module_mst_id INTEGER, "
            "order_by INTEGER DEFAULT 0, active INTEGER)"
        ))
        c.execute(text(
            "CREATE TABLE role_menu_map (role_id INTEGER, menu_id INTEGER, access_type_id INTEGER)"
        ))
        c.execute(text(
            "CREATE TABLE user_role_map (user_id INTEGER, role_id INTEGER, "
            "co_id INTEGER, branch_id INTEGER DEFAULT 1)"
        ))
        c.execute(text(
            "CREATE TABLE control_co_module (co_id INTEGER, module_id INTEGER, access_type INTEGER)"
        ))
        for m in MENUS:
            c.execute(
                text(
                    "INSERT INTO menu_mst (menu_id, menu_name, menu_path, menu_parent_id, "
                    "report, module_mst_id, active) VALUES (:a, :b, :c, :d, :e, :f, :g)"
                ),
                dict(zip("abcdefg", m)),
            )
        for r in ROLE_MENUS:
            c.execute(
                text("INSERT INTO role_menu_map VALUES (:a, :b, :c)"),
                dict(zip("abc", r)),
            )
        for u in USER_ROLES:
            c.execute(
                text("INSERT INTO user_role_map (user_id, role_id, co_id) VALUES (:a, :b, :c)"),
                dict(zip("abc", u)),
            )
        yield c


def _reports(conn, user_id, root_path="jutepurchase/reports"):
    rows = conn.execute(
        get_report_menu_tree(), {"root_path": root_path, "user_id": user_id}
    ).fetchall()
    return [r._mapping["menu_id"] for r in rows]


def test_user_sees_only_granted_reports(conn):
    assert _reports(conn, 100) == [10, 11, 12]
    assert _reports(conn, 200) == [11]


def test_reports_page_grant_alone_grants_no_report(conn):
    """Access to the reports page does not imply access to the reports on it."""
    assert _reports(conn, 300) == []


def test_access_type_zero_is_not_a_grant(conn):
    assert _reports(conn, 400) == []


def test_inactive_and_non_report_children_excluded(conn):
    """Role 1 holds menu 2's whole subtree; 13 is inactive and 90 is not a report."""
    got = _reports(conn, 100)
    assert 13 not in got and 90 not in got


def test_module_hidden_for_company_hides_reports(conn):
    """control_co_module.access_type = 2 blanks the module for that company."""
    conn.execute(text("INSERT INTO control_co_module VALUES (5, 7, 2)"))
    assert _reports(conn, 100) == []
    # A user of the same role in another company is unaffected.
    assert _reports(conn, 500) == [10, 11, 12]


def test_module_capped_to_read_still_lists_reports(conn):
    """access_type = 1 caps the module to Read, which is still >= view."""
    conn.execute(text("INSERT INTO control_co_module VALUES (5, 7, 1)"))
    assert _reports(conn, 100) == [10, 11, 12]


def test_unknown_user_gets_nothing(conn):
    assert _reports(conn, 999) == []


def test_unknown_root_path_gets_nothing(conn):
    assert _reports(conn, 100, root_path="nope/reports") == []
