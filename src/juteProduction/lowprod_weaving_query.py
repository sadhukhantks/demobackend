"""SQL for the Weaving Low Producer report (src/juteProduction/lowprod_weaving.py).

Deliberately small: every production / standard / efficiency figure the report
shows comes from the weaving day-slice via
``weaving_lock.fetch_weaving_day_rows`` (log rows when fresh, live recompute
otherwise) — re-deriving them here would fork the formula chain. What the slice
does NOT carry is the worker behind ``eb_id``, so this module holds that lookup
plus the co/branch pair guard.
"""

from sqlalchemy import text

from src.juteProduction.reportQueries import (
    _WORKER_NAME,
    _emp_code_expr,
    _expanding,
)


def get_branch_in_company_query():
    """Guard: the requested branch really belongs to the requested company.

    The day-slice is co-scoped but branch-filtered, so a branch of ANOTHER
    company of the same tenant would silently return an empty grid under this
    co_id's header instead of an error."""
    return text(
        "SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"
    )


def get_weaving_worker_names_query():
    """eb_id -> (eb_no, worker_name) for the ebs appearing on the day-slice rows.

    Keyed on hrms_ed_personal_details (eb_id unique there — verified dev3 + sls);
    emp_code comes from the scalar subquery in reportQueries._emp_code_expr, never
    a join — hrms_ed_official_details holds one row PER branch transfer and a join
    would fan the name map out into duplicate eb_ids.

    No ``active`` filter: a worker who has since left still owns the production
    rows of the reported period and must keep their name in the report.
    """
    return _expanding(
        f"""
        SELECT pd.eb_id,
               {_emp_code_expr('pd')} AS eb_no,
               {_WORKER_NAME} AS worker_name
        FROM hrms_ed_personal_details pd
        WHERE pd.eb_id IN :eb_ids
        """,
        "eb_ids",
    )
