"""
Regression: Bill Pass must ignore soft-deleted inward lines (proc_inward_dtl.active = 0).

The SR screen (get_inward_dtl_for_sr_query) and the accounting posting
(posting_service) both filter `pid.active = 1`; the three bill-pass readers did
not, so a soft-deleted line — and its still-active proc_gst row — inflated
sr_taxable / sr_tax / sr_total and the line grid. Real case (sls): 209 inactive
lines on 75 approved SRs, 169 of them carrying active GST, visible in Bill Pass
but absent from the SR.
"""

import re

from src.procurement.query import (
    get_bill_pass_list_query,
    get_bill_pass_by_id_query,
    get_bill_pass_sr_lines_query,
)


def _sr_totals_block(sql: str) -> str:
    """The `FROM proc_inward_dtl pid ... GROUP BY pid.inward_id` sub-select."""
    m = re.search(r"FROM proc_inward_dtl pid(.*?)GROUP BY pid\.inward_id", sql, re.S)
    assert m, "sr_totals sub-select not found"
    return m.group(1)


def test_list_query_sr_totals_only_active_lines():
    block = _sr_totals_block(str(get_bill_pass_list_query()))
    assert re.search(r"pid\.active\s*=\s*1", block)


def test_by_id_query_sr_totals_only_active_lines():
    block = _sr_totals_block(str(get_bill_pass_by_id_query()))
    assert re.search(r"pid\.active\s*=\s*1", block)


def test_sr_lines_query_only_active_lines():
    sql = str(get_bill_pass_sr_lines_query())
    where = sql[sql.index("WHERE pid.inward_id"):]
    assert re.search(r"pid\.active\s*=\s*1", where)
