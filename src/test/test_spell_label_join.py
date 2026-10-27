"""Every grid that labels a row with its spell must join spell_mst on the PK.

``spell_mst`` fans out per branch through ``shift_mst`` — one ``spell_code``
exists once per shift (sls 'A1' = spell 91 / 97 / 102, dev3 has the same fanout
on 3 of its 10 active spells). Several production queries collapsed the label
join to ``SELECT spell_code, MIN(spell_id) ... GROUP BY spell_code``, which
matches ONLY the lowest spell_id of each code: every other branch's rows came
back with a blank spell_code (2107 of 2669 sls doff rows, all 95 beaming daily
rows), and in the winding reconciliation the derived ``shift`` went NULL, so
those kg fell out of the A/B/C roll-ups of winding_spell_report /
winding_quality_wise (TKT-000087).

The join is on a primary key, so it can never fan a row out — there is no reason
to dedupe. This guards the whole family against the pattern coming back.
"""

import pytest

from src.juteProduction.beaming_query import get_beaming_entries_by_date_query
from src.juteProduction.finishing_query import get_finishing_entries_by_date_query
from src.juteProduction.spinning_query import get_doff_entries_by_date_query
from src.juteProduction.winding_query import (
    get_doff_by_date_query,
    get_jugar_by_date_query,
    get_quality_by_date_query,
    get_winding_reconciliation_query,
)
from src.juteSQC.query import get_beam_mr_by_date_query


@pytest.mark.parametrize(
    "builder",
    [
        get_doff_by_date_query,
        get_jugar_by_date_query,
        get_quality_by_date_query,
        get_winding_reconciliation_query,
        get_beaming_entries_by_date_query,
        get_finishing_entries_by_date_query,
        get_doff_entries_by_date_query,
        get_beam_mr_by_date_query,
    ],
)
def test_spell_label_joins_on_primary_key(builder):
    sql = str(builder())
    assert "GROUP BY spell_code" not in sql
    assert "LEFT JOIN spell_mst sp ON sp.spell_id =" in sql


def test_slice_driver_fans_idle_frames_over_their_own_branch_spells():
    """The spinning day-slice seeds idle mapped frames from spg_quality_helper.

    The helper has no spell grain, so each row fans across a spell set. Fanning
    across ``MIN(spell_id) per spell_code`` gave every branch the SAME (lowest)
    branch's spell ids, so a caller filtering :spell_id on its own branch saw
    nothing (sls branch 87: 0 idle rows) or another branch's frames (dev3
    branch 2 picked up branch 12's 9 machines). The helper carries branch_id —
    fan over that branch's own spells via shift_mst.
    """
    from src.juteProduction.spinning_query import _SLICE_DRIVER_SQL

    assert "MIN(spell_id)" not in _SLICE_DRIVER_SQL
    assert "spx ON 1 = 1" not in _SLICE_DRIVER_SQL
    assert "spx ON spx.branch_id = h.branch_id" in _SLICE_DRIVER_SQL
