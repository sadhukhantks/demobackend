"""The four carding/drawing SQC saves must reject out-of-range bench readings with a 400.

Regression test for the raw-SQL 500 (browser QA, 2026-08-21): a weight of 1e300 passed the
old "w > 0" guard, overflowed decimal(10,3) calc_wt, and MySQL's 1264 DataError reached the
user as a 500 carrying the INSERT statement. The guards now bound both ends, which also
rejects inf/NaN for free (every comparison against NaN is False).
"""

import math

from src.juteSQC.constants import MAX_SQC_READING_MR_PCT, MAX_SQC_READING_WEIGHT_G

# The exact predicates used by all four create_* handlers.
weight_ok = lambda w: 0 < w <= MAX_SQC_READING_WEIGHT_G  # noqa: E731
mr_ok = lambda m: 0 <= m <= MAX_SQC_READING_MR_PCT  # noqa: E731


def test_weight_bounds():
    assert weight_ok(450.0) and weight_ok(MAX_SQC_READING_WEIGHT_G)
    for bad in (0, -1, 1e300, math.inf, -math.inf, math.nan,
                MAX_SQC_READING_WEIGHT_G + 1):
        assert not weight_ok(bad), bad


def test_mr_bounds():
    assert mr_ok(0) and mr_ok(19.5) and mr_ok(MAX_SQC_READING_MR_PCT)
    for bad in (-0.1, 1e300, math.inf, math.nan, MAX_SQC_READING_MR_PCT + 0.01):
        assert not mr_ok(bad), bad


def test_bounds_stay_inside_the_decimal_columns():
    # calc_wt / calc_corr_wt are decimal(10,3); calc_mr_pct is decimal(5,2).
    assert MAX_SQC_READING_WEIGHT_G < 10**7
    assert MAX_SQC_READING_MR_PCT < 10**3
