"""Duplicate detection and the Remove duplicate rows operation, on hand-checked tables."""

import numpy as np
import pandas as pd
import pytest

from refine.duplicates import find_duplicates, group_ids, normalize_text, removal_mask
from refine.errors import CleaningError
from refine.operations import get_operation
from refine.source import TEXT_DTYPE

REMOVE = get_operation("remove_duplicates")


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    # Row: 0      1      2       3      4        5      6
    return pd.DataFrame({
        "id":    text("A", "B", "A", "a", "B", "C", "A"),
        "name":  text("Ann", "Bo", "Ann", "ann", "Bo ", None, "Ann"),
        "city":  text("Oslo", "Rome", "Oslo", "Oslo", "Rome", "Lima", "Oslo"),
    })


def values(series):
    return [None if pd.isna(v) else v for v in series.tolist()]


# ---- detection ---------------------------------------------------------

def test_exact_duplicates_across_all_columns():
    r = find_duplicates(frame())
    # Rows 0, 2 and 6 are identical; nothing else matches exactly.
    assert (r.groups, r.duplicate_rows, r.rows_in_groups, r.rows_after) == (1, 2, 3, 5)
    assert [g.positions for g in r.examples] == [(0, 2, 6)]
    assert round(r.duplicate_pct, 2) == 28.57 and r.all_columns


def test_key_based_duplicates():
    r = find_duplicates(frame(), ["id"])
    # Groups: A (0, 2, 6), B (1, 4). 'a' and 'C' are alone.
    assert (r.groups, r.duplicate_rows, r.rows_in_groups) == (2, 3, 5)
    assert [g.positions for g in r.examples] == [(0, 2, 6), (1, 4)]
    assert [g.number for g in r.examples] == [1, 2]


def test_case_insensitive_matching():
    r = find_duplicates(frame(), ["id"], ignore_case=True)
    assert [g.positions for g in r.examples] == [(0, 2, 3, 6), (1, 4)]
    full = find_duplicates(frame(), ignore_case=True)
    assert [g.positions for g in full.examples] == [(0, 2, 3, 6)]          # 'Bo ' still differs from 'Bo'


def test_whitespace_normalized_matching():
    full = find_duplicates(frame(), normalize_whitespace=True)
    assert [g.positions for g in full.examples] == [(0, 2, 6), (1, 4)]
    both = find_duplicates(frame(), ignore_case=True, normalize_whitespace=True)
    assert (both.groups, both.duplicate_rows) == (2, 4)
    assert normalize_text("  New \t York ", ignore_case=True, normalize_whitespace=True) == "new york"


def test_matching_never_changes_stored_values():
    df = frame()
    before = df.copy(deep=True)
    find_duplicates(df, ignore_case=True, normalize_whitespace=True)
    pd.testing.assert_frame_equal(df, before)


def test_no_value_matches_no_value_but_not_empty_text():
    df = pd.DataFrame({"a": text(None, None, "", " ")})
    assert group_ids(df, ["a"]).tolist() == [0, 0, 1, 2]
    assert group_ids(df, ["a"], normalize_whitespace=True).tolist() == [0, 0, 1, 1]


def test_groups_numbered_by_first_appearance_and_keep_masks():
    df = pd.DataFrame({"k": text("z", "y", "z", "y", "x")})
    groups = group_ids(df, ["k"])
    assert groups.tolist() == [0, 1, 0, 1, 2]
    assert removal_mask(groups, "first").tolist() == [False, False, True, True, False]
    assert removal_mask(groups, "last").tolist() == [True, True, False, False, False]


def test_detection_on_empty_and_unique_tables():
    empty = find_duplicates(pd.DataFrame({"a": text()}))
    assert (empty.groups, empty.duplicate_rows, empty.duplicate_pct) == (0, 0, 0.0)
    unique = find_duplicates(pd.DataFrame({"a": text("1", "2")}))
    assert unique.groups == 0 and unique.examples == ()


# ---- removal -----------------------------------------------------------

def test_keep_first_removes_extra_copies_and_preserves_order():
    df = frame()
    ids = np.arange(7) + 10
    plan = REMOVE.plan({}, df)
    result = REMOVE.apply(df, ids, plan.resolved)
    assert plan.resolved["columns"] == ["id", "name", "city"] and plan.resolved["all_columns"]
    assert plan.resolved["rows_removed"] == 2 and plan.resolved["groups"] == 1
    assert result.row_ids.tolist() == [10, 11, 13, 14, 15]                 # Rows 2 and 6 removed
    assert values(result.data["id"]) == ["A", "B", "a", "B", "C"]
    assert result.counts == {"rows_removed": 2, "groups": 1, "rows_kept": 5}
    assert result.data.index.tolist() == list(range(5))


def test_keep_last_on_key_columns():
    plan = REMOVE.plan({"columns": ["id"], "keep": "last"}, frame())
    result = REMOVE.apply(frame(), np.arange(7), plan.resolved)
    assert result.row_ids.tolist() == [3, 4, 5, 6]                         # Last A (6) and last B (4) survive
    assert any("not an exact copy" in w and "other columns differ" in w for w in plan.warnings)


def test_normalized_removal_keeps_the_survivors_exact_values():
    plan = REMOVE.plan({"columns": ["id", "name"], "ignore_case": True, "normalize_whitespace": True}, frame())
    result = REMOVE.apply(frame(), np.arange(7), plan.resolved)
    assert result.row_ids.tolist() == [0, 1, 5]
    assert values(result.data["name"]) == ["Ann", "Bo", None]              # Stored spelling of the kept rows
    assert any("ignores case and extra spaces" in w for w in plan.warnings)
    assert plan.summary == ("Remove 4 duplicate rows matching on 'id', 'name' (ignoring case, ignoring extra "
                            "spaces), keeping the first row of each of 2 groups.")


def test_exact_full_row_removal_has_no_difference_warning():
    plan = REMOVE.plan({}, frame())
    assert not any("not an exact copy" in w for w in plan.warnings)


def test_sample_marks_kept_and_removed_rows():
    plan = REMOVE.plan({"columns": ["id"]}, frame())
    sample = REMOVE.sample(frame(), plan.resolved, 10)
    assert sample.index.tolist() == [0, 2, 6, 1, 4]
    assert sample["Duplicate group"].tolist() == [1, 1, 1, 2, 2]
    assert sample["Outcome"].tolist() == ["Kept", "Removed", "Removed", "Kept", "Removed"]
    assert REMOVE.sample(frame(), plan.resolved, 2).index.tolist() == [0, 2]


def test_sample_labels_do_not_collide_with_user_columns():
    df = pd.DataFrame({"Outcome": text("x", "x"), "Duplicate group": text("1", "1")})
    plan = REMOVE.plan({}, df)
    sample = REMOVE.sample(df, plan.resolved, 10)
    assert list(sample.columns) == ["Duplicate group (2)", "Outcome (2)", "Outcome", "Duplicate group"]


def test_never_removes_without_duplicates_and_validates():
    with pytest.raises(CleaningError) as err:
        REMOVE.plan({"columns": ["city", "name"]}, frame().iloc[[0, 1]].reset_index(drop=True))
    assert err.value.code == "no_change"
    for params in ({"columns": []}, {"columns": ["nope"]}, {"keep": "none"}, {"ignore_case": "yes"},
                   {"extra": True}):
        with pytest.raises(CleaningError) as err:
            REMOVE.plan(params, frame())
        assert err.value.code == "invalid_params"


def test_apply_checks_recorded_counts():
    plan = REMOVE.plan({}, frame())
    with pytest.raises(CleaningError) as err:
        REMOVE.apply(frame().iloc[:3].reset_index(drop=True), np.arange(3), plan.resolved)
    assert err.value.code == "replay_mismatch"


def test_removal_does_not_modify_inputs():
    df = frame()
    before = df.copy(deep=True)
    ids = np.arange(7)
    plan = REMOVE.plan({"ignore_case": True}, df)
    result = REMOVE.apply(df, ids, plan.resolved)
    result.data.iloc[0, 0] = "changed"
    pd.testing.assert_frame_equal(df, before)
    assert ids.tolist() == list(range(7))
