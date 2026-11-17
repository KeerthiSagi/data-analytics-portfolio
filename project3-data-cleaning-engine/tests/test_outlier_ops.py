"""Treat outliers (refine/operations/outlier_ops.py): validation and the four treatments,
called on the operation directly (plan, apply, sample) without a session."""

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.operations import get_operation, jsonable
from refine.operations.outlier_ops import replacement_value
from refine.source import TEXT_DTYPE

OP = get_operation("treat_outliers")
# IQR on 1..9 and 100: boundaries -3.5 and 14.5; with -50 first: -5 and 15 (see tests/test_outliers.py)
BASE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 100]


def table(values=None, dtype="Int64", index=None):
    values = BASE if values is None else values
    n = len(values)
    return pd.DataFrame({
        "id": pd.array([f"r{i}" for i in range(n)], dtype=TEXT_DTYPE),
        "x": pd.array(values, dtype=dtype) if dtype is not None else values,
        "note": pd.array(["n"] * n, dtype=TEXT_DTYPE),
    }, index=index)


def run(data, **params):
    params = {"column": "x", "method": "iqr", "treatment": "cap", **params}
    plan = OP.plan(params, data)
    result = OP.apply(data, np.arange(len(data), dtype=np.int64), plan.resolved)
    return plan, result


def error(data, **params):
    with pytest.raises(CleaningError) as err:
        run(data, **params)
    return err.value


# ---- validation -------------------------------------------------------------

def test_normalized_settings_and_frozen_values():
    plan, _ = run(table())
    assert plan.step.params == {"column": "x", "method": "iqr", "multiplier": 1.5, "treatment": "cap", "value": None}
    r = plan.resolved
    assert (r["lower_bound"], r["upper_bound"]) == (-3.5, 14.5)
    assert r["statistics"] == {"q1": 3.25, "q3": 7.75, "iqr": 4.5}
    assert (r["below"], r["above"], r["outliers"], r["evaluated"], r["missing"], r["rows"]) == (0, 1, 1, 10, 0, 10)
    assert r["source_dtype"] == "Int64" and r["result_dtype"] == "Float64"   # 14.5 is not a whole number
    assert plan.affected_columns == ("x",)
    assert jsonable(plan.to_dict()) == plan.to_dict()
    assert plan.summary == "Cap 1 outlier in 'x' outside -3.5 to 14.5 (IQR, k = 1.5) at the boundaries."


@pytest.mark.parametrize("params, message", [
    ({"column": "id"}, "stored as"),
    ({"column": "missing"}, "no column"),
    ({"column": None}, "Choose a column"),
    ({"method": "mad"}, "method"),
    ({"treatment": "winsorize"}, "treatment"),
    ({"multiplier": 0}, "more than 0"),
    ({"multiplier": "2"}, "must be a number"),
    ({"threshold": 3}, "not used by the IQR"),
    ({"method": "zscore", "threshold": -1}, "more than 0"),
    ({"method": "percentile", "lower_percentile": 99, "upper_percentile": 1}, "lower < upper"),
    ({"value": 5}, "only used with 'Replace with a fixed value'"),
    ({"surprise": 1}, "Unknown setting"),
])
def test_invalid_settings(params, message):
    err = error(table(), **params)
    assert err.code == "invalid_params" and message in err.message


def test_boolean_column_is_refused():
    data = table()
    data["flag"] = pd.array([True, False] * 5, dtype="boolean")
    err = error(data, column="flag")
    assert "True / false columns have no outliers" in err.message


@pytest.mark.parametrize("data, message", [
    (table([None] * 4, dtype="Float64"), "no values"),
    (table([], dtype="Float64"), "has no rows"),
    (table([1, None, 2], dtype="Int64"), "at least 3"),
    (table([1.0, 2.0, np.inf, 4.0], dtype="float64"), "non-finite"),
])
def test_columns_that_cannot_be_checked(data, message):
    err = error(data)
    assert err.code == "invalid_params" and message in err.message


def test_no_outliers_is_no_change():
    err = error(table([1, 2, 3, 4, 5]))                         # Q1 2, Q3 4: boundaries -1 and 7
    assert err.code == "no_change" and "-1 to 7" in err.message
    err = error(table([7, 7, 7, 7]))                              # Constant column
    assert err.code == "no_change"


def test_small_sample_zscore_no_change_explains_why():
    err = error(table([1, 2, 1000]), method="zscore")
    assert err.code == "no_change" and "√(n - 1)" in err.message


# ---- cap ----------------------------------------------------------------------

def test_cap_example_from_the_specification():
    # [10, 12, 14, 15, 100]: Q1 12, Q3 15, IQR 3 -> boundaries 7.5 and 19.5; 100 becomes 19.5
    plan, result = run(table([10, 12, 14, 15, 100], dtype="Float64"))
    assert result.data["x"].tolist() == [10.0, 12.0, 14.0, 15.0, 19.5]
    assert str(result.data["x"].dtype) == "Float64"
    assert result.counts == {"outliers": 1, "cells_changed": 1, "rows_removed": 0}


def test_cap_both_sides_with_missing_values():
    values = [-50, None, 1, 2, 3, 4, 5, 6, 7, 8, 9, 100]
    plan, result = run(table(values, dtype="Float64"))
    assert (plan.resolved["lower_bound"], plan.resolved["upper_bound"]) == (-5.0, 15.0)
    out = result.data["x"].tolist()
    assert out[0] == -5.0 and out[-1] == 15.0 and out[2:11] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert pd.isna(out[1])                                       # Missing stays missing
    assert plan.resolved["missing"] == 1


def test_cap_keeps_int64_when_boundaries_are_whole():
    plan, result = run(table([-50, *BASE]))                     # -5 and 15
    assert plan.resolved["result_dtype"] == "Int64"
    assert str(result.data["x"].dtype) == "Int64"
    assert result.data["x"].tolist() == [-5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 15]


def test_cap_promotes_int64_to_float64_instead_of_rounding():
    plan, result = run(table())
    assert str(result.data["x"].dtype) == "Float64" and result.data["x"].iloc[-1] == 14.5
    assert any("Int64 to Float64" in w for w in plan.warnings)


def test_cap_only_checks_the_boundary_it_uses():
    # Lower boundary -3.5 is fractional but nothing is below it; the upper, 15, is whole.
    plan, result = run(table([1, 2, 3, 4, 5, 6, 7, 8, 9, 100]), multiplier=2)
    # Q1 3.25, Q3 7.75, IQR 4.5 -> -5.75 and 16.75: fractional and used, so Float64
    assert plan.resolved["result_dtype"] == "Float64"
    plan, result = run(table([-50, *BASE]), multiplier=1.5)
    assert plan.resolved["result_dtype"] == "Int64"


def test_cap_numpy_dtypes():
    _, result = run(table([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 100], dtype="float64"))
    assert result.data["x"].dtype == np.float64 and result.data["x"].iloc[-1] == 14.5
    _, result = run(table([-50, *BASE], dtype="int64"))
    assert result.data["x"].dtype == np.int64 and result.data["x"].tolist()[0] == -5


def test_cap_refuses_float64_when_integers_are_too_large_to_keep_exactly():
    err = error(table([1, 2, 3, 4, 5, 6, 7, 8, 9, 2 ** 60]))     # Upper boundary 14.5
    assert "cannot hold its largest whole numbers exactly" in err.message


# ---- set missing ----------------------------------------------------------------

def test_set_missing_keeps_dtype_and_existing_missing_values():
    plan, result = run(table([None, -50, *BASE]), treatment="set_missing")
    out = result.data["x"]
    assert str(out.dtype) == "Int64"
    assert out.isna().tolist() == [True, True] + [False] * 9 + [True]
    assert plan.resolved["missing"] == 1 and result.counts["cells_changed"] == 2


def test_set_missing_numpy_float_and_int():
    _, result = run(table([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 100], dtype="float64"), treatment="set_missing")
    assert result.data["x"].dtype == np.float64 and np.isnan(result.data["x"].iloc[-1])
    plan, result = run(table(BASE, dtype="int64"), treatment="set_missing")
    assert str(result.data["x"].dtype) == "Int64" and pd.isna(result.data["x"].iloc[-1])
    assert any("int64 to Int64" in w for w in plan.warnings)


def test_set_missing_result_works_with_fill_missing():
    _, result = run(table(BASE, dtype="Float64"), treatment="set_missing")
    fill = get_operation("fill_missing")
    plan = fill.plan({"columns": ["x"], "method": "median"}, result.data)
    filled = fill.apply(result.data, result.row_ids, plan.resolved).data
    assert filled["x"].iloc[-1] == 5.0 and str(filled["x"].dtype) == "Float64"


# ---- remove rows -------------------------------------------------------------------

def test_remove_rows_keeps_order_identities_and_missing_rows():
    data = table([None, -50, 1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
    plan, result = run(data, treatment="remove_rows")
    # Boundaries -5 and 15: -50 (row 1) and 100 (row 11) go; row 0 (missing) stays
    assert result.data["id"].tolist() == ["r0", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10"]
    assert result.row_ids.tolist() == [0, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert list(result.data.columns) == ["id", "x", "note"] and str(result.data["x"].dtype) == "Int64"
    assert result.counts == {"outliers": 2, "rows_removed": 2, "rows_kept": 10, "cells_changed": 0}
    assert isinstance(result.data.index, pd.RangeIndex)
    assert any("Rows where the column is missing are kept" in w for w in plan.warnings)


def test_remove_rows_refuses_to_remove_every_row():
    # Percentiles 40-60 of [0, 10] are 4 and 6, so both values are outliers
    err = error(table([0, 10, 0, 10]), method="percentile", lower_percentile=40, upper_percentile=60,
                treatment="remove_rows")
    assert err.code == "invalid_params" and "At least one row must remain" in err.message


def test_remove_rows_with_duplicate_index_labels():
    data = table(index=[0, 0, 1, 1, 2, 2, 3, 3, 9, 9])          # 100 shares label 9 with 9
    plan, result = run(data, treatment="remove_rows")
    assert len(result.data) == 9 and result.data["x"].tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert result.row_ids.tolist() == list(range(9))


def test_cap_with_duplicate_index_labels():
    data = table(index=[5] * 10)
    _, result = run(data)
    assert result.data["x"].tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 14.5]
    assert result.data["id"].tolist() == data["id"].tolist()


# ---- replace with a fixed value ------------------------------------------------------

def test_replace_value_int_and_float():
    _, result = run(table(), treatment="replace_value", value="0")
    assert result.data["x"].tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 0] and str(result.data["x"].dtype) == "Int64"
    _, result = run(table(dtype="Float64"), treatment="replace_value", value=-2.5)
    assert result.data["x"].iloc[-1] == -2.5 and str(result.data["x"].dtype) == "Float64"
    plan, _ = run(table(), treatment="replace_value", value="1e1")
    assert plan.step.params["value"] == 10 and isinstance(plan.step.params["value"], int)


def test_replace_value_outside_the_boundaries_is_warned():
    plan, _ = run(table(), treatment="replace_value", value=500)
    assert any("itself outside the boundaries" in w for w in plan.warnings)


@pytest.mark.parametrize("value, message", [
    ("", "Enter the number"), ("  ", "Enter the number"), (None, "Enter the number"),
    ("abc", "is not a number"), ("$5", "is not a number"), ("1,000", "is not a number"),
    ("nan", "is not a number"), ("inf", "is not a number"), ("-inf", "is not a number"),
    (float("nan"), "finite"), (float("inf"), "finite"), (float("-inf"), "finite"),
    (True, "not true or false"), ([1], "must be a number"),
    (2.5, "whole numbers"), ("2.5", "whole numbers"), (2 ** 70, "range"),
    ("1e999", "finite"),
])
def test_invalid_replacement_values(value, message):
    err = error(table(), treatment="replace_value", value=value)
    assert err.code == "invalid_params" and message in err.message


def test_replacement_value_parsing():
    assert replacement_value(" 42 ", "integer") == 42
    assert replacement_value("-3.5", "decimal") == -3.5
    assert replacement_value(7, "decimal") == 7.0 and isinstance(replacement_value(7, "decimal"), float)
    assert replacement_value(3.0, "integer") == 3


# ---- shared guarantees ----------------------------------------------------------------

@pytest.mark.parametrize("treatment, extra", [("cap", {}), ("set_missing", {}), ("remove_rows", {}),
                                              ("replace_value", {"value": "0"})])
def test_inputs_are_not_modified_and_other_columns_are_kept(treatment, extra):
    data = table([None, -50, *BASE])
    before = data.copy(deep=True)
    plan, result = run(data, treatment=treatment, **extra)
    OP.sample(data, plan.resolved, 10)
    pd.testing.assert_frame_equal(data, before)
    assert list(result.data.columns) == ["id", "x", "note"]
    kept = result.row_ids
    assert result.data["id"].tolist() == before["id"].iloc[kept].tolist()
    assert result.data["note"].tolist() == before["note"].iloc[kept].tolist()


def test_apply_refuses_data_that_does_not_match_the_plan():
    data = table()
    plan = OP.plan({"column": "x", "method": "iqr", "treatment": "cap"}, data)
    ids = np.arange(10, dtype=np.int64)
    changed = data.copy()
    changed.loc[0, "x"] = 99                                     # A second value above 14.5
    with pytest.raises(CleaningError) as err:
        OP.apply(changed, ids, plan.resolved)
    assert err.value.code == "replay_mismatch" and "values above 14.5" in err.value.message
    retyped = data.astype({"x": "Float64"})
    with pytest.raises(CleaningError, match="recorded step expects Int64"):
        OP.apply(retyped, ids, plan.resolved)
    with pytest.raises(CleaningError, match="no column"):
        OP.apply(data.drop(columns="x"), ids, plan.resolved)


def test_apply_uses_the_frozen_boundaries():
    data = table()
    plan = OP.plan({"column": "x", "method": "iqr", "treatment": "cap"}, data)
    frozen = dict(plan.resolved, lower_bound=-1.0, upper_bound=50.0)          # Still 0 below, 1 above
    result = OP.apply(data, np.arange(10, dtype=np.int64), frozen)
    assert result.data["x"].iloc[-1] == 50.0                    # Not recalculated from the data


def test_sample_shows_low_and_high_outliers_before_and_after():
    data = table([*([100] * 12), -50, -40, *range(1, 41)])      # Boundaries -27.5 and 78.5
    plan = OP.plan({"column": "x", "method": "iqr", "treatment": "set_missing"}, data)
    sample = OP.sample(data, plan.resolved, 10)
    assert list(sample.columns) == ["x", "x (after)"] and len(sample) == 10
    assert sample["x"].tolist()[-2:] == [-50, -40]               # Low ones are not crowded out by 12 high ones
    assert sample.index.tolist() == [0, 1, 2, 3, 4, 5, 6, 7, 12, 13]   # Table order
    assert all(v is None for v in sample["x (after)"])
    removal = OP.sample(data, dict(plan.resolved, treatment="remove_rows"), 4)
    assert list(removal.columns)[:2] == ["Outcome", "x"] and set(removal["Outcome"]) == {"Removed"}
