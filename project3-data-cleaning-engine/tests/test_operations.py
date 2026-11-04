"""The operation framework and the Drop columns operation."""

import json

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.operations import Plan, Step, available_operations, get_operation, jsonable
from refine.operations.registry import register
from refine.source import TEXT_DTYPE

DROP = get_operation("drop_columns")


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": pd.array(["1", "2", "3"], dtype=TEXT_DTYPE),
        "name": pd.array(["Ann", None, "Cy"], dtype=TEXT_DTYPE),
        "notes": pd.array(["", None, "x"], dtype=TEXT_DTYPE),
        "empty": pd.array([None, "", None], dtype=TEXT_DTYPE),
    })


def test_registry():
    assert [op.op_id for op in available_operations()] == [
        "drop_columns", "standardize_missing", "drop_missing_rows", "drop_missing_columns", "fill_missing",
        "add_missing_indicator", "remove_duplicates",
        # Phase 5
        "convert_type", "trim_whitespace", "change_case", "collapse_whitespace", "normalize_unicode",
        "remove_control_characters", "find_replace", "map_categories",
    ]
    with pytest.raises(CleaningError) as err:
        get_operation("convert_types")          # Not an operation id
    assert err.value.code == "unknown_operation"
    with pytest.raises(ValueError):
        class Other(type(DROP)):
            pass
        register(Other())


@pytest.mark.parametrize("params, fragment", [
    ({}, "list of column names"),
    ({"columns": "name"}, "list of column names"),
    ({"columns": []}, "at least one column"),
    ({"columns": [1]}, "must be text"),
    ({"columns": ["name", "name"]}, "repeated"),
    ({"columns": ["nope"]}, "no column 'nope'"),
    ({"columns": ["id", "name", "notes", "empty"]}, "At least one column must remain"),
    ({"columns": ["name"], "extra": 1}, "Unknown setting"),
])
def test_validation_errors(params, fragment):
    with pytest.raises(CleaningError) as err:
        DROP.plan(params, frame())
    assert err.value.code == "invalid_params"
    assert fragment in err.value.message


def test_plan_rejects_non_mapping_and_non_json_values():
    with pytest.raises(CleaningError):
        DROP.plan(["name"], frame())
    with pytest.raises(CleaningError):
        jsonable({"x": object()})


def test_plan_normalizes_to_table_order_and_freezes_values():
    plan = DROP.plan({"columns": ("notes", "name")}, frame())
    assert isinstance(plan, Plan)
    assert plan.step == Step("drop_columns", {"columns": ["name", "notes"]})
    assert plan.resolved == {"columns": ["name", "notes"], "positions": [2, 3]}
    assert plan.affected_columns == ("name", "notes")
    assert plan.summary == "Drop 2 columns: 'name', 'notes'."
    json.dumps(plan.to_dict())          # Serializable
    assert Step.from_dict(plan.step.to_dict()) == plan.step


def test_warnings_count_values_that_are_not_missing():
    plan = DROP.plan({"columns": ["name", "notes"]}, frame())
    assert plan.warnings[0].startswith("3 values that are not missing")
    assert "at least half" in plan.warnings[1]
    quiet = DROP.plan({"columns": ["empty"]}, frame())
    assert quiet.warnings == ()          # Only NA and "" cells: nothing lost, under half the table


def test_apply_returns_new_frame_and_leaves_input_unchanged():
    df = frame()
    before = df.copy(deep=True)
    ids = np.array([10, 11, 12])
    plan = DROP.plan({"columns": ["notes"]}, df)
    result = DROP.apply(df, ids, plan.resolved)
    pd.testing.assert_frame_equal(df, before)
    assert list(result.data.columns) == ["id", "name", "empty"]
    assert result.counts == {"columns_removed": 1, "cells_removed": 3, "values_removed": 1}
    assert result.row_ids.tolist() == [10, 11, 12] and result.row_ids is not ids
    result.data.iloc[0, 0] = "changed"
    pd.testing.assert_frame_equal(df, before)                 # No shared buffers
    assert all(dtype == TEXT_DTYPE for dtype in result.data.dtypes)


def test_apply_uses_frozen_resolved_values_only():
    with pytest.raises(CleaningError) as err:
        DROP.apply(frame().drop(columns="notes"), np.arange(3), {"columns": ["notes"]})
    assert err.value.code == "replay_mismatch"


def test_sample_shows_affected_columns():
    sample = DROP.sample(frame(), {"columns": ["name"]}, limit=2)
    assert list(sample.columns) == ["name"] and len(sample) == 2
