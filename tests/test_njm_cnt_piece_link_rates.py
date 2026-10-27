"""Contractor piece production must price through the branch-wise production-quality ->
wages-quality link before falling back to the process token.

Aug-2026 on sls branch 87: finishing rows tagged `hemming` carried two qualities — 8 (HEMMING
26.5", linked to card 7801, contract rate 3.50) and 18 (HEMMING JOINTS, linked to card 7808,
contract rate 12.75). The contractor arm priced every `hemming` row on 7801, so hemming joints
were paid 3.50 instead of 12.75. The main-wages FNG arm already resolves the link first.
"""
import re

from src.hrms.njmWagesData import (
    CNT_PIECE_PROCESS_QCODE,
    LINK_DEPT_NEEDLE,
    _CNT_PIECE_INSERT,
    _MAIN_FNG_INSERT,
)


def _sql(stmt) -> str:
    return re.sub(r"\s+", " ", str(stmt))


def test_contractor_piece_arm_prices_through_the_quality_link_first():
    sql = _sql(_CNT_PIECE_INSERT)
    assert "link_prodquality_wagequality_mst" in sql
    assert "lk.prod_quality_id = fd.finishing_quality_id" in sql
    # the link decides the card; the process token is only the fallback
    assert "COALESCE(lk.q_code, :q_code)" in sql
    # the card is still the branch's own
    assert "q.branch_id = :branch_id" in sql


def test_contractor_piece_arm_binds_the_same_department_needle_as_the_main_arm():
    assert ":dept_needle" in _sql(_CNT_PIECE_INSERT)
    assert ":dept_needle" in _sql(_MAIN_FNG_INSERT)
    assert LINK_DEPT_NEEDLE["finishing"]


def test_finishing_needle_covers_both_finish_and_sack_departments():
    # QUALITY_TYPES maps FINISH and SACK onto "finishing". A dict-comp keyed by
    # qtype used to keep only SACK, so FINISHING(LC) work (q_code 7808 @ 12.75)
    # was mispriced at the 7801 fallback (3.50). The needle must match both, and
    # it is matched with REGEXP (alternation), not LIKE.
    needle = LINK_DEPT_NEEDLE["finishing"]
    assert re.search(needle, "FINISHING(LC)")
    assert re.search(needle, "SACK SEWING")
    for stmt in (_CNT_PIECE_INSERT, _MAIN_FNG_INSERT):
        assert "REGEXP :dept_needle" in _sql(stmt)
        assert "LIKE :dept_needle" not in _sql(stmt)


def test_process_token_fallback_is_unchanged():
    assert CNT_PIECE_PROCESS_QCODE == {"hemming": "7801", "herackle": "7803", "sacksewing": "7805"}
