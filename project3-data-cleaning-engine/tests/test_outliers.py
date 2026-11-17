"""Outlier detection (refine/outliers.py): IQR, z-score and percentile boundaries, hand-calculated.

Quantiles use linear interpolation: for sorted values v[0..n-1], the q-th
quantile sits at position (n - 1) * q, between the two nearest values.
"""

import math

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.outliers import (
    MIN_VALUES,
    check_settings,
    describe_settings,
    detect_outliers,
    eligible_columns,
    fmt,
    is_eligible,
    outside,
)
from refine.source import TEXT_DTYPE


def ints(*values, name="x"):
    return pd.Series(pd.array(list(values), dtype="Int64"), name=name)


def floats(*values, name="x"):
    return pd.Series(pd.array(list(values), dtype="Float64"), name=name)


def positions(found):
    return np.flatnonzero(found.mask).tolist()


# ---- eligibility --------------------------------------------------------

def test_only_whole_and_decimal_columns_are_eligible():
    data = pd.DataFrame({
        "i": pd.array([1, 2], dtype="Int64"), "f": pd.array([1.5, None], dtype="Float64"),
        "np_i": np.array([1, 2], dtype=np.int64), "np_f": np.array([1.0, np.nan]),
        "b": pd.array([True, False], dtype="boolean"), "np_b": np.array([True, False]),
        "t": pd.array(["1", "2"], dtype=TEXT_DTYPE), "d": pd.to_datetime(["2026-01-01", "2026-01-02"]),
    })
    assert eligible_columns(data) == ["i", "f", "np_i", "np_f"]
    assert not is_eligible(pd.BooleanDtype()) and not is_eligible(np.dtype(bool))


def test_text_numbers_are_not_parsed():
    found = detect_outliers(pd.Series(pd.array(["1", "2", "300"], dtype=TEXT_DTYPE), name="t"), "iqr", {})
    assert found.status == "unsupported" and "Data types" in found.reason
    assert detect_outliers(pd.Series(pd.array([True, False, True], dtype="boolean"), name="b"), "iqr", {}).status \
        == "unsupported"


# ---- IQR ----------------------------------------------------------------

def test_iqr_hand_calculated():
    found = detect_outliers(ints(1, 2, 3, 4, 5, 6, 7, 8, 9, 100), "iqr", {})
    # Q1 at position 2.25 -> 3.25; Q3 at 6.75 -> 7.75; IQR 4.5; 1.5 x 4.5 = 6.75
    assert found.statistics == {"q1": 3.25, "q3": 7.75, "iqr": 4.5}
    assert (found.lower, found.upper) == (-3.5, 14.5)
    assert positions(found) == [9] and found.n_above == 1 and found.n_below == 0
    assert found.settings == {"multiplier": 1.5}                 # Default multiplier
    assert found.evaluated == 10 and found.missing == 0 and found.pct == 10.0


def test_iqr_custom_multiplier_and_lower_outliers():
    values = ints(-50, 1, 2, 3, 4, 5, 6, 7, 8, 9, 100)
    default = detect_outliers(values, "iqr", {})
    # Q1 at 2.5 -> 2.5, Q3 at 7.5 -> 7.5, IQR 5
    assert (default.lower, default.upper) == (-5.0, 15.0)
    assert positions(default) == [0, 10] and (default.n_below, default.n_above) == (1, 1)
    wide = detect_outliers(values, "iqr", {"multiplier": 20})
    assert (wide.lower, wide.upper) == (-97.5, 107.5) and wide.outliers == 0


def test_iqr_values_equal_to_a_boundary_are_not_outliers():
    # n = 6: Q1 at 1.25 -> 0, Q3 at 3.75 -> 10, IQR 10, boundaries -15 and 25
    found = detect_outliers(ints(-15, 0, 0, 10, 10, 25), "iqr", {})
    assert (found.lower, found.upper) == (-15.0, 25.0)
    assert found.outliers == 0


def test_iqr_constant_column_has_no_outliers():
    found = detect_outliers(floats(4.2, 4.2, 4.2, 4.2), "iqr", {})
    assert found.ok and found.statistics["iqr"] == 0 and found.outliers == 0 and not found.notes


def test_iqr_zero_iqr_follows_the_rule_and_says_so():
    # n = 9: Q1 at position 2 and Q3 at position 6 are both 5
    found = detect_outliers(ints(5, 5, 5, 5, 5, 5, 5, 6, 100), "iqr", {})
    assert found.statistics["iqr"] == 0 and (found.lower, found.upper) == (5.0, 5.0)
    assert positions(found) == [7, 8]                            # Everything other than 5
    assert "IQR is 0" in found.notes[0]


def test_iqr_missing_values_are_not_outliers_or_statistics():
    found = detect_outliers(floats(None, 1, 2, 3, 4, 5, 6, 7, 8, 9, 100, None), "iqr", {})
    assert found.missing == 2 and found.evaluated == 10
    assert (found.lower, found.upper) == (-3.5, 14.5)            # Same as without the missing cells
    assert positions(found) == [10]


def test_numpy_float_nan_is_missing_and_int_is_supported():
    found = detect_outliers(pd.Series([np.nan, 1.0, 2, 3, 4, 5, 6, 7, 8, 9, 100], name="f"), "iqr", {})
    assert found.missing == 1 and positions(found) == [10]
    assert detect_outliers(pd.Series(np.arange(1, 11, dtype=np.int64), name="i"), "iqr", {}).outliers == 0


@pytest.mark.parametrize("series, status", [
    (floats(name="x"), "no_values"),                             # Empty column
    (floats(None, None, None), "no_values"),                     # All missing
    (ints(1, 2), "too_few"),
    (ints(None, 7, None), "too_few"),
])
def test_columns_without_enough_values_are_refused(series, status):
    found = detect_outliers(series, "iqr", {})
    assert found.status == status and not found.ok and found.reason
    assert found.outliers == 0 and found.lower is None


def test_minimum_is_three_values():
    assert MIN_VALUES == 3
    assert detect_outliers(ints(1, 2, 3), "iqr", {}).ok


@pytest.mark.parametrize("bad", [np.inf, -np.inf])
def test_infinite_values_are_refused(bad):
    found = detect_outliers(pd.Series([1.0, 2.0, 3.0, bad], name="f"), "zscore", {})
    assert found.status == "non_finite" and "non-finite" in found.reason


def test_nan_inside_a_nullable_float_column_is_refused():
    array = pd.arrays.FloatingArray(np.array([np.nan, 1.0, 2.0, 3.0]), np.zeros(4, dtype=bool))
    series = pd.Series(array, name="f")
    if bool(series.isna().iloc[0]):
        pytest.skip("This pandas version treats NaN in a Float64 column as missing.")
    assert detect_outliers(series, "iqr", {}).status == "non_finite"


def test_very_large_and_very_small_values():
    big = detect_outliers(floats(1e300, 2e300, 3e300, 4e300, 1e306), "iqr", {})
    assert big.ok and positions(big) == [4]
    tiny = detect_outliers(floats(1e-300, 2e-300, 3e-300, 4e-300, 1e-290), "iqr", {})
    assert tiny.ok and positions(tiny) == [4]
    huge = detect_outliers(floats(-1e308, 0.0, 1e308), "iqr", {})
    assert huge.status == "too_large"                            # Boundaries would overflow to infinity


@pytest.mark.parametrize("multiplier", [0, -1, 101, math.nan, math.inf, "1.5", True])
def test_invalid_multipliers(multiplier):
    with pytest.raises(CleaningError) as err:
        detect_outliers(ints(1, 2, 3), "iqr", {"multiplier": multiplier})
    assert err.value.code == "invalid_params"


def test_settings_of_another_method_are_refused():
    with pytest.raises(CleaningError, match="not used by the IQR"):
        check_settings("iqr", {"threshold": 2})
    with pytest.raises(CleaningError, match="method"):
        check_settings("mad", {})


def test_detection_does_not_modify_the_input():
    series = floats(None, 1, 2, 3, 400, name="x")
    before = series.copy()
    detect_outliers(series, "iqr", {})
    detect_outliers(series, "zscore", {"threshold": 1})
    pd.testing.assert_series_equal(series, before)


def test_duplicate_index_labels_do_not_matter():
    series = pd.Series(pd.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 100], dtype="Int64"), index=[0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
    found = detect_outliers(series, "iqr", {})
    assert positions(found) == [9] and len(found.mask) == 10


# ---- Z-score --------------------------------------------------------------

def test_zscore_hand_calculated():
    # Mean 5, population standard deviation 2 (sample sd would be 2.14)
    values = ints(2, 4, 4, 4, 5, 5, 7, 9)
    found = detect_outliers(values, "zscore", {"threshold": 1.5})
    assert found.statistics == {"mean": 5.0, "std": 2.0}
    assert (found.lower, found.upper) == (2.0, 8.0)
    assert positions(found) == [7]                               # 9: z = 2; 2 has z = -1.5, on the boundary
    default = detect_outliers(values, "zscore", {})
    assert default.settings == {"threshold": 3.0} and (default.lower, default.upper) == (-1.0, 11.0)
    assert default.outliers == 0


def test_zscore_negative_outliers():
    found = detect_outliers(ints(-9, -7, -5, -5, -4, -4, -4, -2), "zscore", {"threshold": 1.5})
    assert (found.lower, found.upper) == (-8.0, -2.0)
    assert positions(found) == [0] and found.n_below == 1 and found.n_above == 0


def test_zscore_default_threshold_finds_an_extreme_value():
    values = [0] * 20 + [100]
    found = detect_outliers(ints(*values), "zscore", {})
    mean = 100 / 21
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / 21)
    assert found.statistics["mean"] == pytest.approx(mean, rel=1e-12)
    assert found.statistics["std"] == pytest.approx(sd, rel=1e-12)
    assert positions(found) == [20]                              # z = 4.47


def test_zscore_constant_and_near_constant_columns():
    constant = detect_outliers(floats(0.1, 0.1, 0.1, 0.1, 0.1), "zscore", {"threshold": 0.001})
    assert constant.statistics == {"mean": 0.1, "std": 0.0} and constant.outliers == 0     # No division by zero
    assert (constant.lower, constant.upper) == (0.1, 0.1)


def test_zscore_small_samples_cannot_exceed_sqrt_n_minus_1():
    found = detect_outliers(ints(1, 2, 1000), "zscore", {})
    assert found.ok and found.outliers == 0                      # Largest |z| with 3 values is √2
    assert "√(n - 1)" in found.notes[0]


def test_zscore_with_missing_values():
    found = detect_outliers(ints(None, 2, 4, 4, 4, 5, 5, 7, 9, None), "zscore", {"threshold": 1.5})
    assert found.missing == 2 and found.evaluated == 8 and positions(found) == [8]


def test_zscore_is_stable_for_large_magnitudes():
    found = detect_outliers(floats(1e200, 2e200, 2e200, 3e200), "zscore", {"threshold": 1})
    assert found.ok and found.statistics["mean"] == pytest.approx(2e200)
    assert found.statistics["std"] == pytest.approx(math.sqrt(0.5) * 1e200)
    assert positions(found) == [0, 3]


@pytest.mark.parametrize("threshold", [0, -3, 1000, math.nan, "3"])
def test_invalid_thresholds(threshold):
    with pytest.raises(CleaningError) as err:
        detect_outliers(ints(1, 2, 3), "zscore", {"threshold": threshold})
    assert err.value.code == "invalid_params"


# ---- Percentile -------------------------------------------------------------

def test_percentile_hand_calculated():
    values = pd.Series(np.arange(0, 101, dtype=np.int64), name="p")      # The p-th percentile is p
    found = detect_outliers(values, "percentile", {"lower_percentile": 5, "upper_percentile": 95})
    assert (found.lower, found.upper) == (5.0, 95.0)
    assert positions(found) == [0, 1, 2, 3, 4, 96, 97, 98, 99, 100]     # 5 and 95 themselves are not
    default = detect_outliers(values, "percentile", {})
    assert default.settings == {"lower_percentile": 1.0, "upper_percentile": 99.0}
    assert positions(default) == [0, 100]


def test_percentile_interpolates_between_values():
    found = detect_outliers(ints(10, 20, 30, 40), "percentile", {"lower_percentile": 10, "upper_percentile": 90})
    # Positions 0.3 and 2.7: 10 + 0.3 * 10 = 13 and 30 + 0.7 * 10 = 37
    assert (found.lower, found.upper) == pytest.approx((13.0, 37.0))
    assert positions(found) == [0, 3]


def test_percentile_full_range_has_no_outliers():
    found = detect_outliers(ints(1, 5, 9, 1000), "percentile", {"lower_percentile": 0, "upper_percentile": 100})
    assert (found.lower, found.upper) == (1.0, 1000.0) and found.outliers == 0


def test_percentile_repeated_values_and_identical_bounds():
    ties = detect_outliers(ints(*([1] * 50 + [2] * 50)), "percentile", {})
    assert (ties.lower, ties.upper) == (1.0, 2.0) and ties.outliers == 0     # Not 1% in each tail
    same = detect_outliers(ints(1, *([5] * 10), 9), "percentile", {"lower_percentile": 10, "upper_percentile": 90})
    assert (same.lower, same.upper) == (5.0, 5.0) and positions(same) == [0, 11]
    assert "same value" in same.notes[0]


def test_percentile_constant_and_small_columns():
    assert detect_outliers(ints(3, 3, 3), "percentile", {}).outliers == 0
    small = detect_outliers(ints(1, 2, 3), "percentile", {"lower_percentile": 40, "upper_percentile": 60})
    assert (small.lower, small.upper) == pytest.approx((1.8, 2.2)) and positions(small) == [0, 2]


def test_percentile_with_missing_values():
    found = detect_outliers(floats(None, 10, 20, 30, 40, None), "percentile",
                            {"lower_percentile": 10, "upper_percentile": 90})
    assert found.missing == 2 and positions(found) == [1, 4]


@pytest.mark.parametrize("lower, upper", [(50, 50), (60, 40), (-1, 99), (1, 101), (math.nan, 99)])
def test_invalid_percentile_ranges(lower, upper):
    with pytest.raises(CleaningError) as err:
        detect_outliers(ints(1, 2, 3), "percentile", {"lower_percentile": lower, "upper_percentile": upper})
    assert err.value.code == "invalid_params"


# ---- helpers -----------------------------------------------------------------

def test_outside_is_strict_and_ignores_nan():
    below, above = outside(np.array([0.0, 1.0, 2.0, 3.0, np.nan]), 1.0, 2.0)
    assert below.tolist() == [True, False, False, False, False]
    assert above.tolist() == [False, False, False, True, False]


def test_formatting():
    assert [fmt(35.0), fmt(-5.0), fmt(12.25), fmt(1.5e20)] == ["35", "-5", "12.25", "1.5e+20"]
    assert describe_settings("iqr", {"multiplier": 1.5}) == "IQR, k = 1.5"
    assert describe_settings("zscore", {"threshold": 3.0}) == "Z-score, threshold 3"
    assert describe_settings("percentile", {"lower_percentile": 1.0, "upper_percentile": 99.0}) == \
        "Percentile, 1st to 99th"
    assert describe_settings("percentile", {"lower_percentile": 2.5, "upper_percentile": 11.0}) == \
        "Percentile, 2.5th to 11th"
