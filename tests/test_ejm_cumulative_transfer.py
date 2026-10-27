"""The cumulative INSERT names its target columns in one list and builds the
SELECT from CUMULATIVE_COLUMNS plus four literal zeros. If either side drifts
the statement still parses but posts hours into the wrong column, so pin the
two lists to each other.
"""
import re

from src.hrms.ejmWagesData import CUMULATIVE_COLUMNS, _CUMULATIVE_INSERT

SQL = str(_CUMULATIVE_INSERT)


def test_insert_columns_line_up_with_select_expressions():
    target = re.search(r"INSERT INTO cumulative_data_table\s*\((.*?)\)\s*SELECT", SQL, re.S).group(1)
    cols = [c.strip() for c in target.split(",")]
    select = re.search(r"SELECT (.*?)\s+FROM \(", SQL, re.S).group(1)
    exprs = [e.strip() for e in select.split(",")]
    assert len(cols) == len(exprs)
    # the three key columns, then every pivoted field in CUMULATIVE_COLUMNS order
    assert cols[:3] == ["date_from", "date_to", "eb_id"]
    assert cols[3:3 + len(CUMULATIVE_COLUMNS)] == [f for f, _ in CUMULATIVE_COLUMNS]
    assert exprs[3:3 + len(CUMULATIVE_COLUMNS)] == [f"g.{f}" for f, _ in CUMULATIVE_COLUMNS]
    # the legacy-zeroed balances are the only literals
    assert exprs[3 + len(CUMULATIVE_COLUMNS):] == ["0"] * 4


def test_every_field_is_pivoted_from_its_component_id():
    for field, cid in CUMULATIVE_COLUMNS:
        assert f"COMPONENT_ID = {cid} THEN k.AMOUNT ELSE 0 END) AS {field}" in SQL
    assert "WHERE g.total_earnings > 0" in SQL
