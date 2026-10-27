"""jute_mr_li.mill_moisture: factory QC moisture, written by material inspection only, read by MR edit."""
import re
from unittest.mock import MagicMock

from src.juteProcurement.query import get_jute_mr_line_items_query
from src.test.test_material_inspection_dup_line import (
    COMPLETE_URL, _body, _li_inserts, _li_updates, _make_execute, _override, _teardown, client,
)


def test_mr_line_items_query_selects_stored_mill_moisture():
    sql = re.sub(r"\s+", " ", str(get_jute_mr_line_items_query()))
    assert "jmli.mill_moisture," in sql
    assert "jute_moisture_rdg" not in sql


def _post(existing_li_id, **line):
    session = MagicMock()
    exec_fn = _make_execute(existing_li_id=existing_li_id)
    session.execute.side_effect = exec_fn
    _override(session)
    try:
        resp = client.post(COMPLETE_URL, json=_body(**line))
        assert resp.status_code == 200, resp.text
        return exec_fn.calls
    finally:
        _teardown()


def test_mi_insert_writes_avg_of_readings():
    calls = _post(None, jute_po_li_id=None,
                  moisture_readings=[{"moisture_percentage": 20}, {"moisture_percentage": 21.5}])
    sql, params = _li_inserts(calls)[0]
    assert ":MILL_MOISTURE" in sql
    assert params["mill_moisture"] == 20.75


def test_mi_update_uses_typed_moisture_and_coalesces():
    calls = _post(555, actual_moisture="18")
    sql, params = _li_updates(calls)[0]
    assert "MILL_MOISTURE = COALESCE(:MILL_MOISTURE, MILL_MOISTURE)" in sql
    assert params["mill_moisture"] == 18.0


def test_mi_resave_without_moisture_keeps_stored_value():
    _, params = _li_updates(_post(555))[0]
    assert params["mill_moisture"] is None  # COALESCE keeps the stored figure
