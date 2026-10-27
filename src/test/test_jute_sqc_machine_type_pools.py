"""Per-report SQC machine-type pools (dept-based machine dropdowns, 2026-08-15).

Covers the shared expanding-IN builder (get_machines_by_type_names_query), the
per-report name-list constants (union of dev3 + sls vocabularies), and the finishing
per-process map's tuple form (rolling carries the sls 'Rolling Machine' variant).
"""

from src.juteProduction.constants import FINISHING_MACHINE_TYPE_NAMES
from src.juteSQC.constants import (
    CARD_MACHINE_TYPE_NAMES,
    DRAWHEAD_MACHINE_TYPE_NAMES,
    FIN_DRAW_MACHINE_TYPE_NAMES,
)
from src.juteProduction.finishing_query import get_finishing_machines_query
from src.juteSQC.query import get_machines_by_type_names_query


class TestMachinesByTypeNamesQuery:
    def test_filters_on_expanding_type_names(self):
        q = get_machines_by_type_names_query()
        # str() renders the expanding bind as IN ([POSTCOMPILE_type_names])
        assert "mt.machine_type_name IN (__[POSTCOMPILE_type_names])" in str(q)
        assert q._bindparams["type_names"].expanding is True

    def test_scopes_active_and_branch(self):
        sql = str(get_machines_by_type_names_query())
        assert "m.active = 1" in sql
        assert "mt.active = 1" in sql
        assert ":branch_id IS NULL OR d.branch_id = :branch_id" in sql


class TestPerReportPools:
    def test_card_pool_unions_both_tenant_vocabularies(self):
        # sls names + dev3 equivalents; breaker + inter card share one pool.
        assert set(CARD_MACHINE_TYPE_NAMES) == {
            "R/G",
            "Teaser",
            "Breaker",
            "Breaker Card",
            "INTER CARD",
        }

    def test_fin_draw_pool(self):
        assert FIN_DRAW_MACHINE_TYPE_NAMES == ("Drawing",)

    def test_drawhead_pool_unions_both_tenant_vocabularies(self):
        assert set(DRAWHEAD_MACHINE_TYPE_NAMES) == {"Carding Machine", "finishercard"}


class TestFinishingMachineTypes:
    def test_all_values_are_tuples(self):
        assert all(isinstance(v, tuple) for v in FINISHING_MACHINE_TYPE_NAMES.values())

    def test_rolling_carries_sls_variant(self):
        assert set(FINISHING_MACHINE_TYPE_NAMES["rolling"]) == {
            "Rolling",
            "Rolling Machine",
        }

    def test_finishing_query_uses_expanding_machine_types(self):
        q = get_finishing_machines_query()
        assert "mt.machine_type_name IN (__[POSTCOMPILE_machine_types])" in str(q)
        assert q._bindparams["machine_types"].expanding is True
