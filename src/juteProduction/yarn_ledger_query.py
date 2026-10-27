"""Purchased Yarn Ledger query builders (jute production reports).

Phase 5 of docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-
design.md §3.5. Every builder is arg-less and returns a text() statement; the
router supplies the binds. Three bind shapes only:

* window  — :co_id, :branch_id, :date_from, :date_to   (documents IN the month)
* before  — :co_id, :branch_id, :before                 (state strictly BEFORE
            a date; the router passes from_date for openings, so the opening
            is the last count BEFORE the month)
* window-before — :co_id, :branch_id, :date_from, :before (closing counts:
            the last count date in [date_from, before); the router passes
            to_date + 1 day, so "<= to_date" is expressed as "< before")

All figures are KILOGRAMS. Every row carries the item labels (item_code,
item_name, yarn_count) so build_ledger needs no second lookup; yarn_count is
always a scalar subquery because jute_yarn_mst.item_id is NON-UNIQUE and a
JOIN would fan the rows out.

Document eligibility mirrors the yarn tables' own view
(vw_yarn_godown_outstanding): purchases count when APPROVED
(YARN_DOC_STATUS_APPROVED); issues and returns count unless rejected,
cancelled or still draft (YARN_DOC_STOCK_EXCLUDED_STATUSES).
"""

from sqlalchemy import text

from src.juteProcurement.constants import (
    YARN_DOC_STATUS_APPROVED,
    YARN_DOC_STOCK_EXCLUDED_STATUSES,
    YARN_ISSUE_TYPE_ISSUE,
    YARN_ISSUE_TYPE_RETURN,
)
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_LOC_FLOOR,
    WINDING_STOCK_LOC_GODOWN,
)

_EXCLUDED = ", ".join(str(s) for s in YARN_DOC_STOCK_EXCLUDED_STATUSES)

_YARN_COUNT_SUBQ = """
               (SELECT ym.jute_yarn_count
                  FROM jute_yarn_mst ym
                 WHERE ym.item_id = {item}
                 LIMIT 1) AS yarn_count"""


def get_yarn_ledger_purchase_rows_query():
    """Approved Yarn Purchase kg per yarn item in the window.

    has_yarn_master flags an item on a purchase line that has NO jute_yarn_mst
    row (ledger exception ITEM_WITHOUT_YARN_MASTER): the purchase page only
    offers items of yarn item groups, but a master row can be deleted after
    the fact, and the winding report's quality labels depend on it.
    """
    return text(
        f"""
        SELECT li.item_id,
               im.item_code,
               im.item_name,{_YARN_COUNT_SUBQ.format(item="li.item_id")},
               CASE WHEN EXISTS (SELECT 1 FROM jute_yarn_mst yx WHERE yx.item_id = li.item_id)
                    THEN 1 ELSE 0 END AS has_yarn_master,
               SUM(li.weight_kg) AS kg
        FROM jute_yarn_purchase_li li
        INNER JOIN jute_yarn_purchase h ON h.yarn_purchase_id = li.yarn_purchase_id
        LEFT JOIN item_mst im ON im.item_id = li.item_id
        WHERE h.active = 1
          AND li.active = 1
          AND h.status_id = {YARN_DOC_STATUS_APPROVED}
          AND h.co_id = :co_id
          AND h.branch_id = :branch_id
          AND h.tran_date BETWEEN :date_from AND :date_to
        GROUP BY li.item_id, im.item_code, im.item_name
        """
    )


def get_yarn_ledger_movement_rows_query():
    """Yarn Issue / Return kg per (item, department, type) in the window.

    dept_id is on the header (one document = one department, §3.2), so the
    department comes from there. LEFT JOIN dept_mst for the label only.
    """
    return text(
        f"""
        SELECT il.item_id,
               im.item_code,
               im.item_name,{_YARN_COUNT_SUBQ.format(item="il.item_id")},
               i.dept_id,
               dm.dept_desc,
               i.issue_type,
               SUM(il.weight_kg) AS kg
        FROM jute_yarn_issue_li il
        INNER JOIN jute_yarn_issue i ON i.yarn_issue_id = il.yarn_issue_id
        LEFT JOIN item_mst im ON im.item_id = il.item_id
        LEFT JOIN dept_mst dm ON dm.dept_id = i.dept_id
        WHERE i.active = 1
          AND il.active = 1
          AND i.status_id NOT IN ({_EXCLUDED})
          AND i.co_id = :co_id
          AND i.branch_id = :branch_id
          AND i.tran_date BETWEEN :date_from AND :date_to
        GROUP BY il.item_id, im.item_code, im.item_name, i.dept_id, dm.dept_desc, i.issue_type
        """
    )


def get_yarn_ledger_book_opening_query():
    """Document book per item strictly before :before — approved purchases,
    and issues / returns of every non-excluded status.

    The ledger falls back to this (opening + purchases - issues + returns
    since the beginning of the tables) only when NO godown count precedes the
    month (meta.opening_basis = 'book').
    """
    return text(
        f"""
        SELECT x.item_id,
               im.item_code,
               im.item_name,{_YARN_COUNT_SUBQ.format(item="x.item_id")},
               SUM(x.purchases_kg) AS purchases_kg,
               SUM(x.issues_kg) AS issues_kg,
               SUM(x.returns_kg) AS returns_kg
        FROM (
            SELECT li.item_id,
                   li.weight_kg AS purchases_kg,
                   0 AS issues_kg,
                   0 AS returns_kg
            FROM jute_yarn_purchase_li li
            INNER JOIN jute_yarn_purchase h ON h.yarn_purchase_id = li.yarn_purchase_id
            WHERE h.active = 1
              AND li.active = 1
              AND h.status_id = {YARN_DOC_STATUS_APPROVED}
              AND h.co_id = :co_id
              AND h.branch_id = :branch_id
              AND h.tran_date < :before
            UNION ALL
            SELECT il.item_id,
                   0 AS purchases_kg,
                   CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_ISSUE}' THEN il.weight_kg ELSE 0 END AS issues_kg,
                   CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_RETURN}' THEN il.weight_kg ELSE 0 END AS returns_kg
            FROM jute_yarn_issue_li il
            INNER JOIN jute_yarn_issue i ON i.yarn_issue_id = il.yarn_issue_id
            WHERE i.active = 1
              AND il.active = 1
              AND i.status_id NOT IN ({_EXCLUDED})
              AND i.co_id = :co_id
              AND i.branch_id = :branch_id
              AND i.tran_date < :before
        ) x
        LEFT JOIN item_mst im ON im.item_id = x.item_id
        GROUP BY x.item_id, im.item_code, im.item_name
        """
    )


def _godown_count_sql(last_date_extra: str) -> str:
    """Counted purchased yarn in the GODOWN, per item, on the last count date
    that satisfies ``last_date_extra`` (an extra predicate on the MAX subquery,
    already prefixed with AND).

    kg = SUM(no_of_samples * wt_per_sample) over the PURCHASE-bucket GODOWN
    lines of that date. tran_date is returned so the router can report the
    count date without a second query. item_id may be NULL for an unmastered
    line — it is kept (as its own item) so the godown total stays honest.
    """
    return f"""
        SELECT ws.item_id,
               im.item_code,
               im.item_name,{_YARN_COUNT_SUBQ.format(item="ws.item_id")},
               ws.tran_date,
               SUM(ws.no_of_samples * ws.wt_per_sample) AS kg
        FROM jute_prod_winding_stock ws
        LEFT JOIN item_mst im ON im.item_id = ws.item_id
        WHERE ws.co_id = :co_id
          AND ws.branch_id = :branch_id
          AND ws.active = 1
          AND ws.bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'
          AND ws.stock_loc = '{WINDING_STOCK_LOC_GODOWN}'
          AND ws.tran_date = (
              SELECT MAX(x.tran_date)
              FROM jute_prod_winding_stock x
              WHERE x.co_id = :co_id
                AND x.branch_id = :branch_id
                AND x.active = 1
                AND x.bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'
                AND x.stock_loc = '{WINDING_STOCK_LOC_GODOWN}'
                AND x.tran_date < :before{last_date_extra}
          )
        GROUP BY ws.item_id, im.item_code, im.item_name, ws.tran_date
        """


def _floor_count_sql(last_date_extra: str) -> str:
    """Counted purchased yarn on department FLOORS, per (item, department),
    each department on ITS OWN last count date satisfying ``last_date_extra``.

    Departments are counted on different days, so the last date is resolved
    per dept_id in the derived table and joined NULL-safely (<=>): FLOOR rows
    saved before the department column existed carry dept_id NULL and form
    their own "no department" group rather than vanishing.
    """
    return f"""
        SELECT ws.item_id,
               im.item_code,
               im.item_name,{_YARN_COUNT_SUBQ.format(item="ws.item_id")},
               ws.dept_id,
               dm.dept_desc,
               ws.tran_date,
               SUM(ws.no_of_samples * ws.wt_per_sample) AS kg
        FROM jute_prod_winding_stock ws
        INNER JOIN (
            SELECT dept_id, MAX(tran_date) AS last_date
            FROM jute_prod_winding_stock
            WHERE co_id = :co_id
              AND branch_id = :branch_id
              AND active = 1
              AND bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'
              AND stock_loc = '{WINDING_STOCK_LOC_FLOOR}'
              AND tran_date < :before{last_date_extra}
            GROUP BY dept_id
        ) ld ON ld.dept_id <=> ws.dept_id AND ld.last_date = ws.tran_date
        LEFT JOIN item_mst im ON im.item_id = ws.item_id
        LEFT JOIN dept_mst dm ON dm.dept_id = ws.dept_id
        WHERE ws.co_id = :co_id
          AND ws.branch_id = :branch_id
          AND ws.active = 1
          AND ws.bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'
          AND ws.stock_loc = '{WINDING_STOCK_LOC_FLOOR}'
        GROUP BY ws.item_id, im.item_code, im.item_name, ws.dept_id, dm.dept_desc, ws.tran_date
        """


# The closing lookups add a LOWER bound: a count is a closing figure only when
# it was taken IN the period. Without it, the newest count before the month
# (a pre-phase-4 FLOOR row, or a department last counted months ago) would
# stand in as the month's closing and hide the missing count.
_GODOWN_IN_PERIOD = "\n                AND x.tran_date >= :date_from"
_FLOOR_IN_PERIOD = "\n              AND tran_date >= :date_from"


def get_yarn_ledger_godown_count_query():
    """Godown OPENING count: the last GODOWN count date strictly before
    :before (empty when none exists). Binds: before."""
    return text(_godown_count_sql(""))


def get_yarn_ledger_godown_closing_count_query():
    """Godown CLOSING count: the last GODOWN count date in [:date_from,
    :before) — empty when the period holds no count. Binds: window-before."""
    return text(_godown_count_sql(_GODOWN_IN_PERIOD))


def get_yarn_ledger_floor_count_query():
    """Floor OPENING counts: each department's last FLOOR count date strictly
    before :before. Binds: before."""
    return text(_floor_count_sql(""))


def get_yarn_ledger_floor_closing_count_query():
    """Floor CLOSING counts: each department's last FLOOR count date in
    [:date_from, :before) — a department not counted in the period is absent
    (the ledger then raises DEPT_NO_FLOOR_COUNT). Binds: window-before."""
    return text(_floor_count_sql(_FLOOR_IN_PERIOD))
