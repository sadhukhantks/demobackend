"""Tests for the F10 balepress daily sync (src/juteProduction/services/bale_sync.py).

Pure orchestration over the bale_query builders, exercised with a MagicMock session — no
DB. Execute order inside sync_balepress_daily is fixed: (1) aggregate active PROD bales
on the key, (2) look up the active balepress daily row on the key, then ONE write:
INSERT when the row is missing and bales > 0, UPDATE when it exists and bales > 0,
soft-delete (active = 0) when it exists and bales == 0, nothing when neither.

Covered:
* insert when missing (binds: process 'balepress', prod_uom 'bale', prod_qty = count,
  prod_wt_kg = Σ net, NULL-safe machine_id kept as None)
* update when present (UPDATE targets the existing finishing_daily_id)
* soft-delete when count drops to 0 and a row exists
* no write at all when count is 0 and no row exists
* REPACK exclusion: the aggregate binds entry_type = 'PROD'
* bale_daily_key / same_bale_daily_key helpers (branch ignored, dates compared by value)
* the service never commits (transaction belongs to the caller)
"""

from datetime import date
from unittest.mock import MagicMock

from src.juteProduction.constants import (
    BALE_ENTRY_TYPE_PROD,
    BALE_PRESS_PROCESS,
    BALE_PRESS_PROD_UOM,
)
from src.juteProduction.services.bale_sync import (
    bale_daily_key,
    same_bale_daily_key,
    sync_balepress_daily,
)


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchone=_UNSET, lastrowid=_UNSET):
    res = MagicMock()
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


KEY = dict(
    co_id=1,
    branch_id=4,
    tran_date=date(2026, 9, 10),
    spell_id=91,
    machine_id=210,
    finishing_quality_id=12,
)


class TestSyncBalepressDaily:
    def test_inserts_when_missing(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_attr_row(bales=9, total_net=3390.0)),  # aggregate
            _exec_result(fetchone=None),                                   # no daily row
            _exec_result(lastrowid=777),                                   # insert
        ]

        out = sync_balepress_daily(db, user_id=1, **KEY)

        assert out["action"] == "inserted"
        assert out["bales"] == 9
        assert out["total_net"] == 3390.0
        assert out["finishing_daily_id"] == 777
        stmt = str(db.execute.call_args_list[-1].args[0])
        assert "INSERT INTO jute_prod_finishing_daily" in stmt
        binds = db.execute.call_args_list[-1].args[1]
        assert binds["process"] == BALE_PRESS_PROCESS
        assert binds["prod_uom"] == BALE_PRESS_PROD_UOM
        assert binds["prod_qty"] == 9.0
        assert binds["prod_wt_kg"] == 3390.0
        assert binds["machine_id"] == 210
        assert binds["finishing_quality_id"] == 12
        assert binds["branch_id"] == 4
        assert binds["updated_by"] == 1
        db.commit.assert_not_called()

    def test_updates_when_present(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_attr_row(bales=3, total_net=880.5)),
            _exec_result(fetchone=_attr_row(finishing_daily_id=55, prod_qty=2, prod_wt_kg=600)),
            _exec_result(),  # update
        ]

        out = sync_balepress_daily(db, user_id=7, **KEY)

        assert out["action"] == "updated"
        assert out["finishing_daily_id"] == 55
        stmt = str(db.execute.call_args_list[-1].args[0])
        assert "UPDATE jute_prod_finishing_daily" in stmt
        assert "active = 0" not in stmt
        binds = db.execute.call_args_list[-1].args[1]
        assert binds["id"] == 55
        assert binds["prod_qty"] == 3.0
        assert binds["prod_wt_kg"] == 880.5
        assert binds["updated_by"] == 7
        db.commit.assert_not_called()

    def test_soft_deletes_when_count_zero(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_attr_row(bales=0, total_net=0)),
            _exec_result(fetchone=_attr_row(finishing_daily_id=55, prod_qty=2, prod_wt_kg=600)),
            _exec_result(),  # soft delete
        ]

        out = sync_balepress_daily(db, user_id=1, **KEY)

        assert out["action"] == "deleted"
        stmt = str(db.execute.call_args_list[-1].args[0])
        assert "UPDATE jute_prod_finishing_daily" in stmt
        assert "active = 0" in stmt
        assert db.execute.call_args_list[-1].args[1]["id"] == 55
        assert db.execute.call_count == 3

    def test_no_write_when_count_zero_and_no_row(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_attr_row(bales=0, total_net=0)),
            _exec_result(fetchone=None),
        ]

        out = sync_balepress_daily(db, user_id=1, **KEY)

        assert out["action"] == "none"
        assert out["finishing_daily_id"] is None
        assert db.execute.call_count == 2

    def test_aggregate_counts_prod_only_and_null_safe_machine(self):
        """REPACK rows are excluded (Q4) and a NULL machine stays NULL in the binds."""
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_attr_row(bales=1, total_net=290.0)),
            _exec_result(fetchone=None),
            _exec_result(lastrowid=1),
        ]

        sync_balepress_daily(db, user_id=1, **dict(KEY, machine_id=None))

        agg_stmt = str(db.execute.call_args_list[0].args[0])
        agg_binds = db.execute.call_args_list[0].args[1]
        assert "entry_type = :entry_type" in agg_stmt
        assert agg_binds["entry_type"] == BALE_ENTRY_TYPE_PROD
        assert agg_binds["machine_id"] is None
        assert ":machine_id IS NULL AND machine_id IS NULL" in agg_stmt
        daily_stmt = str(db.execute.call_args_list[1].args[0])
        daily_binds = db.execute.call_args_list[1].args[1]
        assert ":machine_id IS NULL AND machine_id IS NULL" in daily_stmt
        assert daily_binds["process"] == BALE_PRESS_PROCESS
        assert daily_binds["machine_id"] is None
        assert db.execute.call_args_list[2].args[1]["machine_id"] is None


class TestKeyHelpers:
    def test_bale_daily_key_casts_and_keeps_none(self):
        k = bale_daily_key("1", None, date(2026, 9, 10), "91", None, "12")
        assert k == {
            "co_id": 1,
            "branch_id": None,
            "tran_date": date(2026, 9, 10),
            "spell_id": 91,
            "machine_id": None,
            "finishing_quality_id": 12,
        }

    def test_same_key_ignores_branch_and_compares_dates_by_value(self):
        a = bale_daily_key(1, 4, date(2026, 9, 10), 91, 210, 12)
        b = bale_daily_key(1, 9, "2026-09-10", 91, 210, 12)
        assert same_bale_daily_key(a, b)
        assert not same_bale_daily_key(a, dict(b, spell_id=92))
        assert not same_bale_daily_key(a, dict(b, machine_id=None))
        assert not same_bale_daily_key(a, dict(b, finishing_quality_id=13))
        assert not same_bale_daily_key(a, dict(b, tran_date=date(2026, 9, 11)))
