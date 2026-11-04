"""Missing-value helpers and operations, on small hand-checked tables."""

import json

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.missing import (
    EMPTY_TEXT,
    WHITESPACE,
    exact_mean,
    exact_median,
    format_number,
    missing_mask,
    missing_summary,
    parse_plain_numbers,
    scan_markers,
    suggest_markers,
)
from refine.operations import get_operation
from refine.operations.missing_ops import indicator_name
from refine.source import TEXT_DTYPE

STANDARDIZE = get_operation("standardize_missing")
DROP_ROWS = get_operation("drop_missing_rows")
DROP_COLS = get_operation("drop_missing_columns")
FILL = get_operation("fill_missing")
INDICATOR = get_operation("add_missing_indicator")


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "3", "4", "5", "6"),
        "age": text("40", None, "NA", "20", "", "na"),
        "city": text("Oslo", "  ", "null", None, "Rome", "None"),
        "score": text("1.5", "2", None, "x", "3", ""),
    })


def values(series):
    return [None if pd.isna(v) else v for v in series.tolist()]


def run(op, params, df=None, ids=None):
    df = frame() if df is None else df
    ids = np.arange(len(df)) if ids is None else ids
    plan = op.plan(params, df)
    return plan, op.apply(df, ids, plan.resolved)


# ---- definitions and scans -------------------------------------------

def test_missing_means_no_value_or_empty_text():
    s = text("a", None, "", "  ", "NA")
    assert missing_mask(s).tolist() == [False, True, True, False, False]
    summary = {c.name: c for c in missing_summary(frame())}
    assert (summary["age"].missing, summary["age"].no_value, summary["age"].empty_text) == (2, 1, 1)
    assert summary["city"].missing == 1 and round(summary["score"].pct, 2) == 33.33


def test_scan_markers_counts_exact_text_and_respects_case():
    df = frame()
    exact = scan_markers(df, ["age", "city"], ["NA", "null"], case_sensitive=True, empty_text=False,
                         whitespace_only=False)
    assert [(m.column, m.marker, m.value, m.count) for m in exact] == [("age", "NA", "NA", 1),
                                                                       ("city", "null", "null", 1)]
    loose = scan_markers(df, ["age"], ["NA"], case_sensitive=False, empty_text=True, whitespace_only=False)
    assert sorted((m.marker, m.value) for m in loose) == [("NA", "NA"), ("NA", "na"), (EMPTY_TEXT, "")]
    ws = scan_markers(df, ["city"], [], case_sensitive=True, empty_text=False, whitespace_only=True)
    assert [(m.marker, m.value) for m in ws] == [(WHITESPACE, "  ")]


def test_suggestions_never_include_ordinary_values():
    found = suggest_markers(frame(), ["age", "city"])
    assert {(m.column, m.value) for m in found} == {
        ("age", "NA"), ("age", ""), ("age", "na"), ("city", "  "), ("city", "null"), ("city", "None")}
    assert not suggest_markers(pd.DataFrame({"x": text("unknown", "Oslo")}), ["x"])     # 'unknown' is legit


def test_plain_number_parsing_and_exact_statistics():
    parsed = parse_plain_numbers(text("1", " 2.5 ", "$3", "1,000", None, "", "x", "x", "-4e1"))
    assert parsed.values.tolist() == [1.0, 2.5, -40.0] and parsed.plain == 3
    assert parsed.other == 4 and parsed.other_examples == ("x", "$3", "1,000")   # Most frequent first
    assert not parsed.all_integral
    assert exact_mean(np.array([0.1] * 10)) == 0.1 * 10 / 10
    assert exact_median(np.array([3.0, 1.0, 2.0, 10.0])) == 2.5
    assert format_number(42.0, None) == "42" and format_number(2.125, None) == "2.125"
    assert format_number(2.125, 2) == "2.12" and format_number(-0.001, 2) == "0.00"
    assert format_number(7.5, 0) == "8" and format_number(0.0, None) == "0"


# ---- A. standardize markers -------------------------------------------

def test_standardize_case_sensitive_converts_only_exact_tokens():
    plan, result = run(STANDARDIZE, {"columns": ["age", "city"], "tokens": ["NA", "null"]})
    assert plan.resolved["matches"] == {"age": 1, "city": 1}
    assert plan.resolved["values"] == {"age": {"NA": 1}, "city": {"null": 1}}
    assert values(result.data["age"]) == ["40", None, None, "20", "", "na"]          # 'na' and "" untouched
    assert values(result.data["city"]) == ["Oslo", "  ", None, None, "Rome", "None"]
    assert result.counts == {"cells_changed": 2, "columns_changed": 2}
    assert result.data["age"].dtype == TEXT_DTYPE


def test_standardize_case_insensitive_with_empty_and_whitespace():
    plan, result = run(STANDARDIZE, {"columns": ["age", "city"], "tokens": ["NA", "none"], "case_sensitive": False,
                                     "empty_text": True, "whitespace_only": True})
    assert values(result.data["age"]) == ["40", None, None, "20", None, None]
    assert values(result.data["city"]) == ["Oslo", None, "null", None, "Rome", None]
    assert plan.resolved["by_marker"] == {EMPTY_TEXT: 1, WHITESPACE: 1, "NA": 2, "none": 1}
    assert any("other spellings also match" in w and "'None'" in w for w in plan.warnings)
    assert "ignoring case" in plan.summary


def test_standardize_trims_surrounding_spaces_when_matching():
    df = pd.DataFrame({"x": text(" NA ", "NA", "N A")})
    _, result = run(STANDARDIZE, {"columns": ["x"], "tokens": ["NA"]}, df)
    assert values(result.data["x"]) == [None, None, "N A"]


def test_standardize_warns_when_most_values_would_go():
    df = pd.DataFrame({"x": text("None", "None", "None", "a")})
    plan, _ = run(STANDARDIZE, {"columns": ["x"], "tokens": ["None"]}, df)
    assert any("more than half" in w for w in plan.warnings)


@pytest.mark.parametrize("params, code, fragment", [
    ({"columns": ["age"]}, "invalid_params", "at least one marker"),
    ({"columns": ["age"], "tokens": ["  "]}, "invalid_params", "cannot be blank"),
    ({"columns": ["age"], "tokens": "NA"}, "invalid_params", "list of text"),
    ({"columns": [], "tokens": ["NA"]}, "invalid_params", "at least one"),
    ({"columns": ["id"], "tokens": ["NA"]}, "no_change", "nothing would change"),
    ({"columns": ["age"], "tokens": ["NA"], "extra": 1}, "invalid_params", "Unknown setting"),
])
def test_standardize_validation(params, code, fragment):
    with pytest.raises(CleaningError) as err:
        STANDARDIZE.plan(params, frame())
    assert err.value.code == code and fragment in err.value.message


# ---- B. drop rows -----------------------------------------------------

def test_drop_rows_any_all_and_minimum():
    # age missing: rows 1, 4 (None, ""); city missing: row 3; score missing: rows 2, 5
    _, any_ = run(DROP_ROWS, {"columns": ["age", "city"], "how": "any"})
    assert values(any_.data["id"]) == ["1", "3", "6"] and any_.row_ids.tolist() == [0, 2, 5]
    assert any_.counts == {"rows_removed": 3, "rows_kept": 3}

    pair = pd.DataFrame({"a": text(None, "x", "", "y"), "b": text("", None, None, "z")})
    _, all_ = run(DROP_ROWS, {"columns": ["a", "b"], "how": "all"}, pair)
    assert values(all_.data["a"]) == ["x", "y"] and all_.row_ids.tolist() == [1, 3]

    # Values present among age, city, score: row 1 has 3, every other row 2.
    _, minimum = run(DROP_ROWS, {"columns": ["age", "city", "score"], "how": "min_present", "min_present": 3})
    assert values(minimum.data["id"]) == ["1"] and minimum.row_ids.tolist() == [0]
    with pytest.raises(CleaningError) as err:
        DROP_ROWS.plan({"columns": ["age", "city", "score"], "how": "min_present", "min_present": 2}, frame())
    assert err.value.code == "no_change"


def test_drop_rows_preview_numbers_and_sample():
    plan = DROP_ROWS.plan({"columns": ["age"]}, frame())
    assert plan.resolved["rows_removed"] == 2
    assert plan.warnings[0].startswith("2 rows of 6 (33.3%) would be removed")
    assert any("not treated as missing" in w for w in plan.warnings)      # 'NA' and 'na' are text
    sample = DROP_ROWS.sample(frame(), plan.resolved, 10)
    assert sample.index.tolist() == [1, 4] and list(sample.columns)[0] == "age"
    assert plan.summary == "Remove 2 rows with missing a value in 'age'."


def test_drop_rows_refuses_removing_everything_or_nothing():
    df = pd.DataFrame({"a": text(None, ""), "b": text("x", "y")})
    with pytest.raises(CleaningError) as err:
        DROP_ROWS.plan({"columns": ["a"]}, df)
    assert "At least one row must remain" in err.value.message
    with pytest.raises(CleaningError) as err:
        DROP_ROWS.plan({"columns": ["b"]}, df)
    assert err.value.code == "no_change"
    with pytest.raises(CleaningError):
        DROP_ROWS.plan({"columns": ["a", "b"], "how": "min_present", "min_present": 3}, df)
    with pytest.raises(CleaningError):
        DROP_ROWS.plan({"columns": ["a"], "how": "any", "min_present": 1}, df)


def test_drop_rows_apply_checks_recorded_count():
    plan = DROP_ROWS.plan({"columns": ["age"]}, frame())
    changed = frame()
    changed.loc[0, "age"] = None
    with pytest.raises(CleaningError) as err:
        DROP_ROWS.apply(changed, np.arange(6), plan.resolved)
    assert err.value.code == "replay_mismatch"


# ---- C. drop columns by missing share ---------------------------------

def test_drop_columns_by_threshold():
    df = frame()
    plan, result = run(DROP_COLS, {"threshold": 33.0})       # age 2/6, score 2/6 qualify; city 1/6 does not
    assert plan.resolved["columns"] == ["age", "score"]
    assert plan.resolved["missing"] == {"age": 2, "score": 2}
    assert plan.resolved["missing_pct"] == {"age": 33.3333, "score": 33.3333}
    assert list(result.data.columns) == ["id", "city"]
    assert result.counts == {"columns_removed": 2, "cells_removed": 12, "values_removed": 8}
    assert list(df.columns) == ["id", "age", "city", "score"]            # Input untouched
    with pytest.raises(CleaningError) as err:
        DROP_COLS.plan({"threshold": 34.0}, df)
    assert err.value.code == "no_change" and "'age' at 33.3%" in err.value.message


def test_drop_columns_threshold_edges():
    df = pd.DataFrame({"a": text(None, None), "b": text("x", None), "c": text("y", "z")})
    plan = DROP_COLS.plan({"threshold": 100}, df)
    assert plan.resolved["columns"] == ["a"] and not plan.warnings        # Nothing but missing cells lost
    assert DROP_COLS.plan({"threshold": 50, "columns": ["b", "c"]}, df).resolved["columns"] == ["b"]
    with pytest.raises(CleaningError):
        DROP_COLS.plan({"threshold": 0}, df)
    with pytest.raises(CleaningError):
        DROP_COLS.plan({"threshold": 101}, df)
    all_missing = pd.DataFrame({"a": text(None), "b": text("")})
    with pytest.raises(CleaningError) as err:
        DROP_COLS.plan({"threshold": 50}, all_missing)
    assert "At least one column must remain" in err.value.message


# ---- D. fill -----------------------------------------------------------

def test_fill_constant_fills_no_value_and_empty_text_only():
    plan, result = run(FILL, {"columns": ["age"], "method": "constant", "value": "0"})
    assert values(result.data["age"]) == ["40", "0", "NA", "20", "0", "na"]
    assert plan.resolved["fills"] == {"age": "0"} and plan.resolved["cells"] == {"age": 2}
    assert result.counts["cells_filled"] == 2 and result.data["age"].dtype == TEXT_DTYPE


def test_fill_mean_and_median_need_plain_numbers():
    df = pd.DataFrame({"n": text("1", None, "2", "", "6")})
    _, mean = run(FILL, {"columns": ["n"], "method": "mean"}, df)
    assert values(mean.data["n"]) == ["1", "3", "2", "3", "6"]           # (1+2+6)/3 = 3
    _, median = run(FILL, {"columns": ["n"], "method": "median"}, df)
    assert values(median.data["n"]) == ["1", "2", "2", "2", "6"]
    odd = pd.DataFrame({"n": text("1", "2", None)})
    plan, _ = run(FILL, {"columns": ["n"], "method": "mean"}, odd)
    assert plan.resolved["fills"] == {"n": "1.5"}
    assert any("whole number" in w for w in plan.warnings)
    plan, _ = run(FILL, {"columns": ["n"], "method": "mean", "decimals": 0}, odd)
    assert plan.resolved["fills"] == {"n": "2"}                          # Python rounds half to even


def test_fill_mean_rejects_text_unless_asked_and_never_converts():
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["score"], "method": "mean"}, frame())
    assert "1 value in 'score' is not" in err.value.message and "'x'" in err.value.message
    assert "never converted" in err.value.message
    plan, result = run(FILL, {"columns": ["score"], "method": "mean", "non_numeric": "exclude", "decimals": 2})
    assert plan.resolved["fills"] == {"score": "2.17"}                   # (1.5 + 2 + 3) / 3
    assert values(result.data["score"]) == ["1.5", "2", "2.17", "x", "3", "2.17"]
    assert any("ignores 1 value" in w for w in plan.warnings)


def test_fill_mean_on_all_missing_or_non_numeric_column():
    df = pd.DataFrame({"e": text(None, ""), "t": text("a", None)})
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["e"], "method": "median"}, df)
    assert "has no values" in err.value.message
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["t"], "method": "mean", "non_numeric": "exclude"}, df)
    assert "no value in 't' is a plain number" in err.value.message
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["e"], "method": "mode"}, df)
    assert "no most frequent value" in err.value.message


def test_fill_mode_breaks_ties_by_first_appearance():
    df = pd.DataFrame({"c": text("b", "a", None, "a", "b", "", "c")})
    plan, result = run(FILL, {"columns": ["c"], "method": "mode"}, df)
    assert plan.resolved["fills"] == {"c": "b"}                          # 'b' and 'a' tie at 2; 'b' is first
    assert values(result.data["c"]) == ["b", "a", "b", "a", "b", "b", "c"]
    assert any("equally frequent" in w and "'b', 'a'" in w for w in plan.warnings)
    marker = pd.DataFrame({"c": text("NA", "NA", None, "x")})
    assert any("looks like a missing-value marker" in w
               for w in FILL.plan({"columns": ["c"], "method": "mode"}, marker).warnings)


def test_forward_and_backward_fill():
    df = pd.DataFrame({"c": text(None, "a", "", None, "b", None)})
    plan, ff = run(FILL, {"columns": ["c"], "method": "ffill"}, df)
    assert values(ff.data["c"]) == [None, "a", "a", "a", "b", "b"]
    assert plan.resolved["cells"] == {"c": 3} and plan.resolved["unfilled"] == {"c": 1}
    assert ff.counts == {"cells_filled": 3, "columns_changed": 1, "cells_left_missing": 1}
    assert "row order" in plan.warnings[0] and any("before the first value" in w for w in plan.warnings)
    plan, bf = run(FILL, {"columns": ["c"], "method": "bfill"}, df)
    assert values(bf.data["c"]) == ["a", "a", "b", "b", "b", None]
    assert plan.resolved["unfilled"] == {"c": 1}
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["c"], "method": "ffill"}, pd.DataFrame({"c": text("a", None)[::-1]}))
    assert err.value.code == "no_change"


def test_fill_several_columns_freezes_each_value():
    df = pd.DataFrame({"a": text("1", None, "3"), "b": text("10", "20", None)})
    plan, result = run(FILL, {"columns": ["b", "a"], "method": "median"}, df)
    assert plan.resolved["columns"] == ["a", "b"] and plan.resolved["fills"] == {"a": "2", "b": "15"}
    assert values(result.data["a"]) == ["1", "2", "3"] and values(result.data["b"]) == ["10", "20", "15"]


def test_fill_uses_frozen_value_not_recomputed():
    df = pd.DataFrame({"n": text("1", None, "3")})
    plan = FILL.plan({"columns": ["n"], "method": "mean"}, df)
    other = pd.DataFrame({"n": text("100", None, "300")})
    result = FILL.apply(other, np.arange(3), plan.resolved)
    assert values(result.data["n"]) == ["100", "2", "300"]               # The frozen mean, not 200


@pytest.mark.parametrize("params, fragment", [
    ({"columns": ["age"], "method": "constant"}, "Enter the value"),
    ({"columns": ["age"], "method": "constant", "value": ""}, "Enter the value"),
    ({"columns": ["age"], "method": "mean", "value": "1"}, "only used with the 'constant'"),
    ({"columns": ["age"], "method": "mode", "decimals": 2}, "only used with the mean"),
    ({"columns": ["age"], "method": "mean", "decimals": 11}, "at most 10"),
    ({"columns": ["age"], "method": "interpolate"}, "must be one of"),
])
def test_fill_validation(params, fragment):
    with pytest.raises(CleaningError) as err:
        FILL.plan(params, frame())
    assert fragment in err.value.message


def test_fill_typed_columns_keeps_type_and_refuses_bad_values_and_no_missing():
    # Phase 5: typed columns are filled with a value of their own type instead of being refused.
    df = pd.DataFrame({"n": pd.array([1, None], dtype="Int64"), "t": text("a", "b")})
    out = FILL.apply(df, np.arange(2), FILL.plan({"columns": ["n"], "method": "constant", "value": "0"},
                                                  df).resolved).data
    assert str(out["n"].dtype) == "Int64" and out["n"].tolist() == [1, 0]
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["n"], "method": "constant", "value": "zero"}, df)
    assert "is stored as Int64" in err.value.message
    with pytest.raises(CleaningError) as err:
        FILL.plan({"columns": ["t"], "method": "constant", "value": "0"}, df)
    assert err.value.code == "no_change"


def test_fill_constant_warns_about_text_in_a_numeric_column():
    df = pd.DataFrame({"n": text("1", "2", "3", None)})
    plan = FILL.plan({"columns": ["n"], "method": "constant", "value": "Unknown"}, df)
    assert any("mostly numbers" in w for w in plan.warnings)


# ---- E. indicator -------------------------------------------------------

def test_indicator_column_is_placed_after_source_with_text_values():
    plan, result = run(INDICATOR, {"column": "age"})
    assert plan.resolved["name"] == "age_missing" and plan.resolved["missing"] == 2
    assert list(result.data.columns) == ["id", "age", "age_missing", "city", "score"]
    assert values(result.data["age_missing"]) == ["false", "true", "false", "false", "true", "false"]
    assert result.data["age_missing"].dtype == TEXT_DTYPE
    assert values(result.data["age"]) == values(frame()["age"])
    assert result.counts == {"columns_added": 1, "rows_flagged": 2}


def test_indicator_names_never_collide():
    assert indicator_name("a", ["a"]) == "a_missing"
    assert indicator_name("a", ["a", "a_missing", "a_missing_2"]) == "a_missing_3"
    df = frame().rename(columns={"city": "age_missing"})
    assert INDICATOR.plan({"column": "age"}, df).resolved["name"] == "age_missing_2"
    with pytest.raises(CleaningError) as err:
        INDICATOR.plan({"column": "age", "name": "id"}, frame())
    assert "already has a column" in err.value.message
    with pytest.raises(CleaningError):
        INDICATOR.plan({"column": "age", "name": " padded "}, frame())
    assert INDICATOR.plan({"column": "age", "name": "flag"}, frame()).resolved["name"] == "flag"
    assert INDICATOR.plan({"column": "age", "name": ""}, frame()).resolved["name"] == "age_missing"


def test_indicator_warns_when_constant():
    assert "every row would be 'false'" in INDICATOR.plan({"column": "id"}, frame()).warnings[0]


# ---- shared properties ----------------------------------------------------

ALL_STEPS = [
    (STANDARDIZE, {"columns": ["age", "city"], "tokens": ["NA", "null"], "case_sensitive": False}),
    (DROP_ROWS, {"columns": ["age"], "how": "any"}),
    (DROP_COLS, {"threshold": 30}),
    (FILL, {"columns": ["age"], "method": "constant", "value": "?"}),
    (FILL, {"columns": ["city"], "method": "ffill"}),
    (INDICATOR, {"column": "score"}),
]


@pytest.mark.parametrize("op, params", ALL_STEPS)
def test_operations_never_modify_inputs_and_share_no_buffers(op, params):
    df = frame()
    before = df.copy(deep=True)
    ids = np.arange(6) + 100
    plan = op.plan(params, df)
    op.sample(df, plan.resolved, 10)
    result = op.apply(df, ids, plan.resolved)
    pd.testing.assert_frame_equal(df, before)
    assert ids.tolist() == list(range(100, 106)) and result.row_ids is not ids
    result.data.iloc[0, 0] = "changed"
    pd.testing.assert_frame_equal(df, before)
    json.dumps(plan.to_dict())
    assert all(isinstance(v, int) for v in result.counts.values())


@pytest.mark.parametrize("op, params", ALL_STEPS)
def test_operations_are_deterministic(op, params):
    a = op.plan(params, frame())
    b = op.plan(params, frame())
    assert a.to_dict() == b.to_dict()
    ra, rb = op.apply(frame(), np.arange(6), a.resolved), op.apply(frame(), np.arange(6), b.resolved)
    pd.testing.assert_frame_equal(ra.data, rb.data)


def test_empty_table():
    df = pd.DataFrame({"a": text(), "b": text()})
    for op, params in [(STANDARDIZE, {"columns": ["a"], "tokens": ["NA"]}), (DROP_ROWS, {"columns": ["a"]}),
                       (DROP_COLS, {"threshold": 50}), (FILL, {"columns": ["a"], "method": "ffill"}),
                       (FILL, {"columns": ["a"], "method": "mean"})]:
        with pytest.raises(CleaningError) as err:
            op.plan(params, df)
        assert err.value.code == "no_change"
    plan, result = run(INDICATOR, {"column": "a"}, df)
    assert list(result.data.columns) == ["a", "a_missing", "b"] and len(result.data) == 0
