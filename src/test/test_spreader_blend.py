"""Tests for spreader blend helpers (dual-quality rolls)."""

from src.juteProduction.services.blend import (
    blend_label,
    canonical_blend,
    target_maturity_hours,
)


class TestCanonicalBlend:
    def test_pure_roll_passthrough(self):
        assert canonical_blend(10, None, 1, None) == (10, None, 1, None)

    def test_pure_roll_defaults_ratio_to_1(self):
        assert canonical_blend(10, None, None, None) == (10, None, 1, None)

    def test_pure_roll_forces_ratio_to_1(self):
        # a crafted payload carrying a ratio without a second item stores as pure 1
        assert canonical_blend(10, None, 3, None) == (10, None, 1, None)

    def test_dominant_first_kept(self):
        assert canonical_blend(10, 20, 2, 1) == (10, 20, 2, 1)

    def test_dominant_first_swapped(self):
        # ATD5/JTD5 (1:2) becomes JTD5/ATD5 (2:1)
        assert canonical_blend(10, 20, 1, 2) == (20, 10, 2, 1)

    def test_ratio_tie_lower_item_first(self):
        assert canonical_blend(20, 10, 1, 1) == (10, 20, 1, 1)

    def test_ratio_tie_already_ordered(self):
        assert canonical_blend(10, 20, 3, 3) == (10, 20, 3, 3)


class TestBlendLabel:
    def test_pure(self):
        assert blend_label("JTD5", None, 1, None) == "JTD5"

    def test_blend(self):
        assert blend_label("JTD5", "ATD5", 2, 1) == "JTD5/ATD5 (2:1)"


class TestTargetMaturity:
    def test_pure_uses_item_hours(self):
        assert target_maturity_hours({10: 24}, 10, None, 1, None, 48) == 24

    def test_pure_missing_uses_default(self):
        assert target_maturity_hours({}, 10, None, 1, None, 48) == 48

    def test_blend_weighted_average(self):
        # (24*2 + 36*1) / 3 = 28
        assert target_maturity_hours({10: 24, 20: 36}, 10, 20, 2, 1, 48) == 28

    def test_blend_half_rounds_up_like_sql(self):
        # (24 + 25) / 2 = 24.5 -> 25, matching MySQL ROUND(), not banker's round() = 24
        assert target_maturity_hours({10: 24, 20: 25}, 10, 20, 1, 1, 48) == 25

    def test_blend_one_missing_uses_other(self):
        assert target_maturity_hours({10: 24}, 10, 20, 2, 1, 48) == 24
        assert target_maturity_hours({20: 36}, 10, 20, 2, 1, 48) == 36

    def test_blend_both_missing_uses_default(self):
        assert target_maturity_hours({}, 10, 20, 2, 1, 48) == 48
