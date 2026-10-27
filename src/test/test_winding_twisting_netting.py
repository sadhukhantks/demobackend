"""TKT-000127 — twisting (ply) output netted out of the winding/weaving difference.

The mill started twisting on 2026-08-21 (sls branch 29) and keys 3-ply output
into the WINDING doff entry, because the ERP has no twisting module. That single
entry does two wrong things to the difference report at once:

* it ADDS ply tonnage to book stock (the doff is winding production), while
* the single yarn twisting consumed is never subtracted, and the ply itself is
  absent from the physical count (jute_prod_winding_stock),

so the difference drifts by roughly TWICE the twisting tonnage. Golden case,
verified against sls on 2026-08-27: branch 29, 2026-08-26, item 335869
'3PLY-YARN' (item group 2268 'YARN', item_type_id 4) carries 1,332 kg of raw
doffs — exactly the 14,269 - 12,937 = 1,332 the reporter saw.

Interim model (option B): flag the ply item on the yarn master (is_twisted) and
SUBTRACT its output as a proxy for the single yarn it ate. Twisting wastage
(input minus output) knowingly stays inside the difference until a real
twisting-INPUT entry exists.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.winding_reports import (
    DIFFERENCE_NOTE,
    build_reconciliation,
    split_twisting_mt,
)
from src.juteProduction.winding_stock_query import (
    get_sale_yarn_mt_query,
    get_twisted_yarn_item_ids_query,
)
from src.main import app


client = TestClient(app)

# --- golden case, sls branch 29 / 2026-08-26 --------------------------------
PLY_ITEM_ID = 335869          # 3PLY-YARN
TWISTING_KG = 1332.0
WINDING_TOTAL_KG = 14269.0    # everything the winding entry booked that day
WINDING_NET_KG = 12937.0      # what is really winding production


def _wind_row(item_id, kg):
    """A row as get_winding_reconciliation_query / get_dpr_winding_mtd_query
    return it — the two share the item_id + production_kg shape the split reads."""
    return {"item_id": item_id, "production_kg": kg}


GOLDEN_WIND_ROWS = [
    _wind_row(335564, 5652.0),          # 16-SKWP
    _wind_row(335563, 4520.0),          # 13-SKWP
    _wind_row(335865, 1837.0),          # 16(96)-SKWP
    _wind_row(335676, 786.0),           # 9.5(TWINE)-HSWP
    _wind_row(335869, 1000.0),          # 3PLY-YARN, two spells
    _wind_row(335869, 332.0),           # 3PLY-YARN
    _wind_row(335670, 142.0),           # padding to the reporter's 14,269
]


# ----------------------------------------------------------------- the split


def test_golden_case_splits_ply_out_of_winding():
    """1,332 kg of 3PLY leaves winding and lands on its own line."""
    assert round(sum(r["production_kg"] for r in GOLDEN_WIND_ROWS), 3) == WINDING_TOTAL_KG

    winding_mt, twisting_mt = split_twisting_mt(GOLDEN_WIND_ROWS, {PLY_ITEM_ID})

    assert twisting_mt == round(TWISTING_KG / 1000.0, 3) == 1.332
    assert winding_mt == round(WINDING_NET_KG / 1000.0, 3) == 12.937


def test_unflagged_ply_stays_in_winding():
    """No flag, no split — the report reads exactly as it did before TKT-127,
    which is why the note tells the mill the flag must be set on every ply item."""
    winding_mt, twisting_mt = split_twisting_mt(GOLDEN_WIND_ROWS, set())
    assert twisting_mt == 0.0
    assert winding_mt == round(WINDING_TOTAL_KG / 1000.0, 3)


def test_split_tolerates_null_production_and_null_item():
    rows = [_wind_row(PLY_ITEM_ID, None), _wind_row(None, 500.0)]
    assert split_twisting_mt(rows, {PLY_ITEM_ID}) == (0.5, 0.0)


# -------------------------------------------------------- the netting maths


def test_golden_case_should_be_drops_by_twice_the_twisting():
    """The bug and its size in one assertion: against the pre-fix model the new
    should-be closing stock is lower by 2 x 1.332 — once because ply left
    winding, once because its single-yarn input is now proxied out."""
    common = dict(
        opening_mt=40.0, weaving_mt=10.0, purchase_mt=0.0,
        sale_mt=0.0, actual_mt=42.0,
    )
    before = build_reconciliation(winding_mt=14.269, **common)
    after = build_reconciliation(winding_mt=12.937, twisting_mt=1.332, **common)

    assert before["should_be_closing_mt"] == 44.269
    assert after["should_be_closing_mt"] == 41.605
    assert round(before["should_be_closing_mt"] - after["should_be_closing_mt"], 3) == 2.664
    # ... and the difference the reporter was chasing shrinks by the same amount.
    assert round(after["difference_mt"] - before["difference_mt"], 3) == 2.664


def test_twisting_is_disclosed_on_its_own_line():
    recon = build_reconciliation(
        opening_mt=0.0, winding_mt=12.937, twisting_mt=1.332,
        weaving_mt=0.0, purchase_mt=0.0, sale_mt=0.0, actual_mt=0.0,
    )
    assert recon["twisting_mt"] == 1.332
    assert recon["winding_mt"] == 12.937  # same shape and units as winding_mt


def test_twisting_defaults_to_zero_for_a_branch_that_does_not_twist():
    """Every pre-TKT-127 caller and every non-twisting branch must reconcile
    exactly as before: opening 18.415 + winding 37.875 = 56.290 (the mill's own
    signed-off sheet for 27-01-2026)."""
    recon = build_reconciliation(
        opening_mt=18.415, winding_mt=37.875, weaving_mt=0.0,
        purchase_mt=0.0, sale_mt=0.0, actual_mt=55.850,
    )
    assert recon["twisting_mt"] == 0.0
    assert recon["should_be_closing_mt"] == 56.290
    assert recon["difference_mt"] == -0.440


def test_percentage_denominator_is_winding_net_of_twisting():
    """pct divides by the winding_mt printed beside it, not by the pre-split
    total — otherwise the percentage silently describes a different number."""
    recon = build_reconciliation(
        opening_mt=0.0, winding_mt=12.937, twisting_mt=1.332,
        weaving_mt=0.0, purchase_mt=0.0, sale_mt=0.0, actual_mt=10.0,
    )
    assert recon["difference_pct"] == round(
        recon["difference_mt"] / 12.937 * 100, 2
    )


def test_note_explains_the_line_and_the_assumption():
    note = DIFFERENCE_NOTE.upper()
    assert "TWISTING" in note
    assert "INPUT ~= OUTPUT" in note          # the interim assumption, stated
    assert "WASTAGE" in note                  # what knowingly stays in the diff
    # phase 5: purchase reads the Yarn Purchase document, Stores is a warning
    assert "YARN PURCHASE" in note
    assert "STORES" in note


# ------------------------------------------------------------- SQL contracts


def test_twisted_item_ids_query_is_distinct():
    """jute_yarn_mst.item_id is NON-UNIQUE — without DISTINCT the caller's set
    build is fine but the scan is not, and the intent stops being readable."""
    sql = str(get_twisted_yarn_item_ids_query())
    assert "SELECT DISTINCT item_id" in sql
    assert "is_twisted = 1" in sql
    assert "item_id IS NOT NULL" in sql


def test_sale_excludes_twisted_items():
    """A ply SALE would be subtracted twice under the new model: once as the
    twisting proxy, once as sale_mt (ply is item_type_id 4 like any yarn)."""
    sql = str(get_sale_yarn_mt_query())
    assert "NOT EXISTS" in sql.upper()
    assert "jute_yarn_mst" in sql
    assert "ym.is_twisted = 1" in sql
    assert "ym.item_id = sid.item_id" in sql
    # still identified by item type, never by invoice_type (pre-existing rule)
    assert "item_type_id" in sql
    assert "invoice_type" not in sql


# -------------------------------------------------- yarn master pass-through


class TestYarnMasterIsTwisted:
    """is_twisted must round-trip through the master that owns it — the report
    has no other way to tell ply from single yarn."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_list_and_detail_queries_select_the_flag(self):
        from src.masters.yarnMaster import (
            get_yarn_list_query,
            get_yarn_list_with_search_query,
        )
        for q in (get_yarn_list_query(), get_yarn_list_with_search_query()):
            assert "ym.is_twisted" in str(q)

    def test_get_yarn_table_returns_the_flag(self):
        row = MagicMock()
        row._mapping = {
            "jute_yarn_id": 1, "item_id": PLY_ITEM_ID, "jute_yarn_name": "3PLY-YARN",
            "item_code": "3PLY-YARN", "jute_yarn_count": 3.0, "std_mr_pct": None,
            "is_twisted": 1, "item_grp_id": 2268, "item_grp_name": "YARN",
            "item_grp_code": "YARN", "jute_yarn_remarks": None, "co_id": 1,
            "updated_by": 1, "updated_date_time": None,
        }
        self._mock_session.execute.return_value.fetchall.return_value = [row]

        response = client.get("/api/yarnMaster/get_yarn_table?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"][0]["is_twisted"] == 1

    def _create(self, payload):
        added = []
        self._mock_session.add.side_effect = added.append
        self._mock_session.flush.side_effect = lambda: [
            setattr(o, "item_id", 999) for o in added if not hasattr(o, "jute_yarn_id")
        ]
        self._mock_session.refresh.side_effect = lambda o: setattr(o, "jute_yarn_id", 42)
        with patch("src.masters.yarnMaster._resolve_yarn_uom_id", return_value=163), \
                patch("src.masters.yarnMaster._check_item_uniqueness", return_value=None):
            response = client.post("/api/yarnMaster/yarn_create", json=payload)
        assert response.status_code == 200, response.text
        return added[1]  # the JuteYarnMst

    def test_create_accepts_the_flag(self):
        yarn = self._create(
            {"jute_yarn_name": "3PLY-YARN", "co_id": "1", "is_twisted": True}
        )
        assert yarn.is_twisted == 1

    def test_create_without_the_flag_defaults_to_zero(self):
        """Backward compatible: an older client that never sends the field
        creates an ordinary single yarn, not a ply one."""
        yarn = self._create({"jute_yarn_name": "16-SKWP", "co_id": "1"})
        assert yarn.is_twisted == 0

    def _edit(self, payload):
        existing_yarn = MagicMock(jute_yarn_id=1, item_id=100, co_id=1, is_twisted="UNSET")
        existing_item = MagicMock(item_id=100, updated_by=1)

        def mock_query(model):
            q = MagicMock()
            target = existing_yarn if model.__tablename__ == "jute_yarn_mst" else existing_item
            q.filter.return_value.first.return_value = target
            return q

        self._mock_session.query.side_effect = mock_query
        with patch("src.masters.yarnMaster._check_item_uniqueness", return_value=None):
            response = client.put("/api/yarnMaster/yarn_edit/1", json=payload)
        assert response.status_code == 200, response.text
        return existing_yarn

    def test_edit_sets_the_flag(self):
        yarn = self._edit(
            {"jute_yarn_name": "3PLY-YARN", "co_id": "1", "is_twisted": 1}
        )
        assert yarn.is_twisted == 1

    def test_edit_clears_the_flag_when_explicitly_false(self):
        yarn = self._edit(
            {"jute_yarn_name": "3PLY-YARN", "co_id": "1", "is_twisted": False}
        )
        assert yarn.is_twisted == 0

    def test_edit_without_the_key_leaves_the_flag_alone(self):
        """An older client saving an unrelated field must not silently un-flag a
        ply item — that would put 2x its tonnage back into the difference."""
        yarn = self._edit({"jute_yarn_name": "3PLY-YARN", "co_id": "1"})
        assert yarn.is_twisted == "UNSET"  # never assigned


def test_parse_is_twisted_reads_the_shapes_a_client_actually_sends():
    from src.masters.yarnMaster import _parse_is_twisted

    for truthy in (1, True, "1", "true", "TRUE", "yes", "Y"):
        assert _parse_is_twisted(truthy) == 1, truthy
    for falsy in (0, False, "0", "false", "no", None, ""):
        assert _parse_is_twisted(falsy) == 0, falsy
