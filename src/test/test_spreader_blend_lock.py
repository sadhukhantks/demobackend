"""Tests for blend-aware group lock in spreader_rules."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.juteProduction.services.spreader_rules import (
    _active_group_for_bin,
    assign_or_reuse_entry_id_grp,
    get_bin_state,
)


def _row(*vals):
    return vals


class TestActiveGroupForBin:
    def test_no_open_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert grp is None and blend is None

    def test_pure_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(6, 10, None, 1, None)
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert grp == 6
        assert blend == (10, None, 1, None)

    def test_blend_group(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(7, 10, 20, 2, 1)
        grp, blend = _active_group_for_bin(db, 1, 2)
        assert blend == (10, 20, 2, 1)


class TestAssignOrReuse:
    @patch("src.juteProduction.services.spreader_rules._next_entry_id_grp", return_value=9)
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(None, None))
    def test_empty_bin_new_group(self, mock_active, mock_next):
        grp, reused = assign_or_reuse_entry_id_grp(
            MagicMock(), 1, 2, (10, None, 1, None), datetime(2026, 8, 14, 8)
        )
        assert (grp, reused) == (9, False)

    @patch("src.juteProduction.services.spreader_rules.evaluate_4hr_window")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_same_blend_reuses_group(self, mock_active, mock_win):
        mock_win.return_value = MagicMock(allowed=True)
        grp, reused = assign_or_reuse_entry_id_grp(
            MagicMock(), 1, 2, (10, 20, 2, 1), datetime(2026, 8, 14, 8)
        )
        assert (grp, reused) == (6, True)

    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="JTD5/ATD5 (2:1)")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_different_blend_rejected_with_label(self, mock_active, mock_lbl):
        with pytest.raises(ValueError) as ei:
            assign_or_reuse_entry_id_grp(
                MagicMock(), 1, 2, (10, None, 1, None), datetime(2026, 8, 14, 8)
            )
        assert "JTD5/ATD5 (2:1)" in str(ei.value)

    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="X")
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_same_items_different_ratio_rejected(self, mock_active, mock_lbl):
        with pytest.raises(ValueError):
            assign_or_reuse_entry_id_grp(
                MagicMock(), 1, 2, (10, 20, 3, 1), datetime(2026, 8, 14, 8)
            )


class TestGetBinState:
    @patch("src.juteProduction.services.spreader_rules.blend_label_from_db", return_value="JTD5/ATD5 (2:1)")
    @patch("src.juteProduction.services.spreader_rules.evaluate_4hr_window", return_value=None)
    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(6, (10, 20, 2, 1)))
    def test_state_exposes_blend_fields(self, mock_active, mock_win, mock_lbl):
        state = get_bin_state(MagicMock(), 1, 2, datetime(2026, 8, 14, 8))
        assert state["locked_item_id"] == 10
        assert state["locked_item_id_2"] == 20
        assert state["locked_ratio_1"] == 2
        assert state["locked_ratio_2"] == 1
        assert state["locked_quality_label"] == "JTD5/ATD5 (2:1)"

    @patch("src.juteProduction.services.spreader_rules._active_group_for_bin", return_value=(None, None))
    def test_empty_bin_state(self, mock_active):
        state = get_bin_state(MagicMock(), 1, 2, datetime(2026, 8, 14, 8))
        assert state["locked_item_id"] is None
        assert state["locked_quality_label"] is None
