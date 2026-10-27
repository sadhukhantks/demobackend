"""
Regression tests for jute PO list search (2026-07-09).

Bug: searching by the formatted PO number (e.g. LCPL/JPO/26-27/00006) returned
no rows because the search clause only matched the raw integer sequence
(jp.po_no). The fix adds the formatted CONCAT expression to the search clause
in both the data and count queries.
"""
from src.juteProcurement.query import (
    get_jute_po_table_query,
    get_jute_po_table_count_query,
)


def _sql(query) -> str:
    return str(query)


def test_data_query_search_matches_formatted_po_number():
    sql = _sql(get_jute_po_table_query(co_id=1, search="%JPO/26-27/00006%"))
    # Formatted PO number pieces must appear inside the search OR block.
    assert "CAST(jp.po_no AS CHAR) LIKE :search" in sql
    assert "'JPO/'" in sql
    assert "LPAD(jp.po_no, 5, '0')" in sql


def test_count_query_search_matches_formatted_po_number():
    sql = _sql(get_jute_po_table_count_query(co_id=1, search="%JPO/26-27/00006%"))
    assert "CAST(jp.po_no AS CHAR) LIKE :search" in sql
    assert "LPAD(jp.po_no, 5, '0')" in sql
    # Count query must join co_mst so the formatted expr's cm.co_prefix resolves.
    assert "co_mst cm" in sql


def test_no_search_clause_when_search_absent():
    sql = _sql(get_jute_po_table_query(co_id=1, search=None))
    assert ":search" not in sql


def test_count_query_skips_name_joins_when_no_search():
    """
    Perf regression (2026-08-21): the count query unconditionally joined a
    GROUP BY derived table over jute_supp_party_map plus party_mst, which MySQL
    merged into the outer join and fanned 3.3k POs x ~700 map rows => 9.4s on
    sls. Those joins only feed the search clause, so they must be absent when
    no search term is given.
    """
    sql = _sql(get_jute_po_table_count_query(co_id=1, search=None))
    assert "jute_supp_party_map" not in sql
    assert "party_mst" not in sql
    assert "co_mst" not in sql
    assert "jute_po jp" in sql


def test_count_query_party_join_mirrors_data_query():
    """Count must join party the same way the data query does (jp.party_id),
    otherwise the total disagrees with the rows for party-name searches."""
    count_sql = _sql(get_jute_po_table_count_query(co_id=1, search="%acme%"))
    data_sql = _sql(get_jute_po_table_query(co_id=1, search="%acme%"))
    assert "party_mst pm ON pm.party_id = jp.party_id" in count_sql
    assert "party_mst pm ON pm.party_id = jp.party_id" in data_sql
