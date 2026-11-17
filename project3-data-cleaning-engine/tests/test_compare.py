"""The comparison engine (refine/compare.py): aggregate comparisons, row matching
through lineage, statistics, edge cases, and that nothing is modified."""

import time

import numpy as np
import pandas as pd
import pytest

from refine.compare import check_lineage, compare_cells, compare_datasets, history_summary
from refine.fingerprint import frame_fingerprint
from refine.profile import profile_dataset
from refine.quality import ValidityRule, assess_quality
from refine.source import TEXT_DTYPE


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def base() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "3", "4", "5", "6"),
        "city": text("Paris", "Rome", None, "Rome", "", "Oslo"),
        "amount": text("10", "20", "30", "NA", "50", "1000"),
    })


def ids(n):
    return np.arange(n, dtype=np.int64)


# ------------------------------------------------------------------
# Identical tables
# ------------------------------------------------------------------

def test_identical_tables_show_no_change():
    df = base()
    c = compare_datasets(df, df.copy(), row_ids=ids(len(df)))
    assert c.identical
    assert all(m.direction == "no change" and m.difference == 0 for m in c.overview)
    assert not any(m.changed for m in c.missing)
    assert c.duplicates.difference == 0
    assert not c.columns.added and not c.columns.removed and not c.columns.type_changed
    assert not c.quality.changed_issues
    assert c.cells.available and c.cells.cells_changed == 0 and c.cells.rows_removed == 0
    assert c.cells.cells_compared == 18


def test_inputs_are_not_modified():
    a, b = base(), base().iloc[[0, 1, 3, 5]].reset_index(drop=True)
    b["amount"] = text("10", "21", None, "1000")
    fa, fb = frame_fingerprint(a), frame_fingerprint(b)
    lineage = np.array([0, 1, 3, 5])
    lineage.setflags(write=False)
    compare_datasets(a, b, row_ids=lineage)
    assert frame_fingerprint(a) == fa and frame_fingerprint(b) == fb
    assert lineage.tolist() == [0, 1, 3, 5]


# ------------------------------------------------------------------
# Overview, missing values, duplicates
# ------------------------------------------------------------------

def test_overview_metrics_and_directions():
    a = base()
    b = a.iloc[[0, 1, 2, 3, 5]].reset_index(drop=True)
    c = compare_datasets(a, b, row_ids=[0, 1, 2, 3, 5])
    rows = c.metric("rows")
    assert (rows.original, rows.current, rows.difference, rows.direction) == (6, 5, -1, "decrease")
    assert c.metric("cells").original == 18 and c.metric("cells").current == 15
    missing = c.metric("missing_cells")
    assert (missing.original, missing.current, missing.direction) == (2, 1, "decrease")
    assert c.metric("missing_pct").original == pytest.approx(100 * 2 / 18)
    assert c.metric("missing_pct").current == pytest.approx(100 * 1 / 15)
    assert c.metric("text_columns").current == 3 and c.metric("numeric_columns").current == 0


def test_missing_uses_each_tables_row_count():
    a = base()
    b = a.iloc[[0, 1, 2]].reset_index(drop=True)      # 3 rows, the None in city kept
    c = compare_datasets(a, b, row_ids=[0, 1, 2])
    city = c.column_missing("city")
    assert (city.original_missing, city.current_missing) == (2, 1)
    assert city.original_pct == pytest.approx(100 * 2 / 6)
    assert city.current_pct == pytest.approx(100 * 1 / 3)
    assert city.difference == -1 and city.direction == "decrease"


def test_missing_matches_the_profile_definition():
    a = base()
    profile = profile_dataset(a)
    c = compare_datasets(a, a, row_ids=ids(6))
    for col in profile.columns:
        assert c.column_missing(col.name).original_missing == col.missing_count
    assert c.metric("missing_cells").original == profile.missing_cells
    # Null-like text is not missing, as on the Profile page.
    assert c.column_missing("amount").original_missing == 0


def test_added_and_removed_columns_in_missing_comparison():
    a = base()
    b = a.drop(columns=["city"]).assign(flag=pd.array([True, None, False, True, True, None], dtype="boolean"))
    c = compare_datasets(a, b, row_ids=ids(6))
    added, removed = c.column_missing("flag"), c.column_missing("city")
    assert added.status == "added" and added.original_missing is None and added.current_missing == 2
    assert added.original_pct is None and added.difference is None and added.direction == "not available"
    assert removed.status == "removed" and removed.current_missing is None and removed.original_missing == 2
    assert added.changed and removed.changed


def test_duplicates_follow_the_profile_definition():
    a = pd.DataFrame({"k": text("a", "a", "b", "b", "b", "c"), "v": text("1", "1", "2", "2", "2", "3")})
    b = a.iloc[[0, 2, 5]].reset_index(drop=True)
    c = compare_datasets(a, b, row_ids=[0, 2, 5])
    p = profile_dataset(a)
    assert c.duplicates.original_duplicates == p.duplicate_rows == 3
    assert c.duplicates.original_groups == p.duplicate_groups == 2
    assert c.duplicates.current_duplicates == 0
    assert c.duplicates.difference == -3 and c.duplicates.direction == "decrease"
    assert c.duplicates.original_pct == pytest.approx(50.0)
    assert c.metric("duplicate_rows").original == 3


def test_case_variants_are_not_exact_duplicates():
    a = pd.DataFrame({"k": text("a", "A", " a")})
    c = compare_datasets(a, a, row_ids=ids(3))
    assert c.duplicates.original_duplicates == 0


# ------------------------------------------------------------------
# Columns and dtypes
# ------------------------------------------------------------------

def test_column_changes_and_dtypes():
    a = base()
    b = pd.DataFrame({
        "amount": pd.array([10, 20, 30, None, 50, 1000], dtype="Int64"),
        "id": a["id"],
        "amount_missing": pd.array([False, False, False, True, False, False], dtype="boolean"),
    })
    c = compare_datasets(a, b, row_ids=ids(6))
    cols = c.columns
    assert [x.name for x in cols.retained] == ["amount", "id"]
    assert [x.name for x in cols.added] == ["amount_missing"] and cols.added[0].kind == "boolean"
    assert [x.name for x in cols.removed] == ["city"]
    assert [t.name for t in cols.type_changed] == ["amount"]
    t = cols.type_changed[0]
    assert (t.original_kind, t.current_kind, t.current_dtype) == ("text", "integer", "Int64")
    assert t.original_label == "Text" and t.current_label == "Whole number"
    assert cols.order_changed
    assert c.metric("numeric_columns").current == 1 and c.metric("boolean_columns").current == 1


def test_column_order_unchanged_when_a_column_is_inserted():
    a = base()
    b = a.copy()
    b.insert(2, "city_missing", pd.array([False] * 6, dtype="boolean"))
    c = compare_datasets(a, b, row_ids=ids(6))
    assert not c.columns.order_changed and [x.name for x in c.columns.added] == ["city_missing"]


# ------------------------------------------------------------------
# Quality
# ------------------------------------------------------------------

def test_quality_reuses_assess_quality_with_the_same_rules():
    a = base()
    b = a.copy()
    b["amount"] = text("10", "20", "30", None, "50", "1000")
    rule = ValidityRule("amount", "range", minimum=0, maximum=100)
    c = compare_datasets(a, b, row_ids=ids(6), rules=(rule,))
    qa = assess_quality(a, profile_dataset(a), (rule,))
    assert c.quality.original_severity == qa.severity_counts()
    by_check = {(i.check, i.column): i for i in c.quality.issues}
    tokens = by_check[("null_tokens", "amount")]
    assert tokens.status == "no longer detected" and tokens.current is None
    new_missing = by_check[("missing_values", "amount")]
    assert new_missing.status == "new" and new_missing.original is None
    rule_issue = by_check[(f"rule:{rule.summary}", "amount")]
    assert rule_issue.status == "unchanged"
    assert c.quality.rules == (rule,)


def test_quality_changed_when_only_the_denominator_changes():
    a = base()
    b = a.iloc[[0, 1, 2, 3, 4]].reset_index(drop=True)      # amount keeps its NA marker
    c = compare_datasets(a, b, row_ids=ids(5))
    issue = next(i for i in c.quality.issues if i.check == "null_tokens")
    assert issue.original.affected_count == issue.current.affected_count == 1
    assert issue.status == "changed"


# ------------------------------------------------------------------
# Row matching
# ------------------------------------------------------------------

def test_row_deletion_gives_no_false_cell_changes():
    a = base()
    keep = [0, 2, 3, 5]
    b = a.iloc[keep].reset_index(drop=True)
    cells = compare_cells(a, b, keep)
    assert cells.available and cells.rows_removed == 2 and cells.rows_kept == 4
    assert cells.cells_changed == 0 and cells.cells_compared == 12


def test_positional_matching_would_be_wrong_but_lineage_is_right():
    a = pd.DataFrame({"v": text("a", "b", "c", "d")})
    b = pd.DataFrame({"v": text("b", "c", "d")})          # First row removed
    cells = compare_cells(a, b, [1, 2, 3])
    assert cells.cells_changed == 0
    wrong = compare_cells(a.iloc[:3], b, [0, 1, 2])       # What position matching would claim
    assert wrong.cells_changed == 3


def test_value_changes_are_classified():
    a = pd.DataFrame({"v": text("x", None, "y", "z", "", "w")})
    b = pd.DataFrame({"v": text("x", "filled", None, "Z", "now", "")})
    col = compare_cells(a, b, ids(6)).column("v")
    assert (col.unchanged, col.modified, col.filled, col.made_missing) == (1, 1, 2, 2)
    assert col.compared_as == "values" and col.changed == 5
    assert [e.original_row for e in col.examples] == [2, 3, 4, 5, 6]
    assert col.examples[0].before == "(no value)" and col.examples[0].after == '"filled"'


def test_empty_text_to_no_value_is_a_change_of_stored_value():
    a = pd.DataFrame({"v": text("", None)})
    b = pd.DataFrame({"v": text(None, None)})
    col = compare_cells(a, b, ids(2)).column("v")
    assert (col.modified, col.unchanged, col.filled, col.made_missing) == (1, 1, 0, 0)


def test_changed_rows_after_deletion_point_to_original_rows():
    a = pd.DataFrame({"v": text("a", "b", "c", "d", "e")})
    b = pd.DataFrame({"v": text("a", "C", "e")})
    cells = compare_cells(a, b, [0, 2, 4])
    col = cells.column("v")
    assert col.modified == 1 and col.examples[0].original_row == 3
    assert cells.rows_removed == 2


def test_duplicate_index_labels_do_not_affect_matching():
    a = pd.DataFrame({"v": text("a", "b", "c", "d")}, index=[7, 7, 7, 7])
    b = pd.DataFrame({"v": text("b", "X")}, index=[0, 0])
    cells = compare_cells(a, b, [1, 3])
    col = cells.column("v")
    assert col.unchanged == 1 and col.modified == 1 and col.examples[0].original_row == 4
    full = compare_datasets(a, b, row_ids=[1, 3])
    assert full.cells.cells_changed == 1


def test_equal_row_counts_without_lineage_are_not_matched():
    a = base()
    b = base().iloc[::-1].reset_index(drop=True)     # Same shape, rows reversed
    c = compare_datasets(a, b)
    assert not c.cells.available and c.cells.columns == ()
    assert "Equal row counts" in c.cells.reason
    assert not c.identical
    # The aggregate comparisons are still given.
    assert c.metric("rows").difference == 0 and c.column_missing("city").current_missing == 2


@pytest.mark.parametrize("row_ids, fragment", [
    ([0, 1], "one entry per current row"),
    ([0, 1, 2, 3, 4, 9], "not in the original"),
    ([-1, 1, 2, 3, 4, 5], "not in the original"),
    ([0, 0, 2, 3, 4, 5], "more than once"),
    ([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], "whole-number"),
    ([[0, 1, 2, 3, 4, 5]], "one entry per current row"),
])
def test_invalid_lineage_gives_an_explicit_limitation(row_ids, fragment):
    a = base()
    cells = compare_cells(a, a, row_ids)
    assert not cells.available and fragment in cells.reason
    assert cells.cells_changed == 0 and cells.columns == ()


def test_check_lineage_accepts_valid_ids():
    got, reason = check_lineage(np.array([0, 2, 5], dtype=np.int32), 6, 3)
    assert reason is None and got.dtype == np.int64 and got.tolist() == [0, 2, 5]


def test_reordered_rows_are_matched_by_identity():
    a = pd.DataFrame({"v": text("a", "b", "c")})
    b = pd.DataFrame({"v": text("c", "a", "b")})
    cells = compare_cells(a, b, [2, 0, 1])
    assert cells.cells_changed == 0 and not cells.order_preserved


# ------------------------------------------------------------------
# Type changes, nullable dtypes, mixed values
# ------------------------------------------------------------------

def test_dtype_change_compared_as_canonical_text():
    a = pd.DataFrame({"n": text("42", " 7", "1.50", "NA", None)})
    b = pd.DataFrame({"n": pd.array([42, 7, None, None, None], dtype="Int64")})
    col = compare_cells(a, b, ids(5)).column("n")
    assert col.compared_as == "text"
    # "42" -> 42 unchanged; " 7" -> 7 modified; "1.50" -> missing, "NA" -> missing; None -> None unchanged
    assert (col.unchanged, col.modified, col.made_missing, col.filled) == (2, 1, 2, 0)


def test_nullable_dtypes_same_type():
    a = pd.DataFrame({
        "i": pd.array([1, None, 3], dtype="Int64"),
        "f": pd.array([1.5, None, 2.0], dtype="Float64"),
        "b": pd.array([True, None, False], dtype="boolean"),
    })
    b = pd.DataFrame({
        "i": pd.array([1, 2, None], dtype="Int64"),
        "f": pd.array([1.5, None, 9.0], dtype="Float64"),
        "b": pd.array([True, None, True], dtype="boolean"),
    })
    cells = compare_cells(a, b, ids(3))
    assert (cells.column("i").filled, cells.column("i").made_missing, cells.column("i").unchanged) == (1, 1, 1)
    assert (cells.column("f").modified, cells.column("f").unchanged) == (1, 2)
    assert (cells.column("b").modified, cells.column("b").unchanged) == (1, 2)
    assert all(c.compared_as == "values" for c in cells.columns)


def test_dates_and_datetimes():
    import datetime as dt
    import pyarrow as pa

    dates = pd.array([dt.date(2024, 1, 1), None], dtype=pd.ArrowDtype(pa.date32()))
    a = pd.DataFrame({"d": dates, "t": pd.to_datetime(["2024-01-01 10:00", None])})
    b = pd.DataFrame({"d": dates, "t": pd.to_datetime(["2024-01-01 11:00", None])})
    c = compare_datasets(a, b, row_ids=ids(2))
    assert c.cells.column("d").changed == 0
    assert c.cells.column("t").modified == 1 and c.cells.column("t").unchanged == 1
    assert c.metric("datetime_columns").original == 2


def test_mixed_object_values():
    a = pd.DataFrame({"m": pd.Series([1, "1", 2.5, None, True], dtype=object)})
    b = pd.DataFrame({"m": pd.Series([1, 1, 2.5, None, 1], dtype=object)})
    col = compare_cells(a, b, ids(5)).column("m")
    # "1" -> 1 and True -> 1 are different values of different types.
    assert (col.unchanged, col.modified) == (3, 2)
    c = compare_datasets(a, b, row_ids=ids(5))
    assert c.metric("other_columns").original == 1


def test_unhashable_values_do_not_break_comparison():
    a = pd.DataFrame({"m": pd.Series([[1], [2]], dtype=object)})
    b = pd.DataFrame({"m": pd.Series([[1], [3]], dtype=object)})
    c = compare_datasets(a, b, row_ids=ids(2))
    assert c.cells.column("m").modified == 1 and c.cells.column("m").unchanged == 1
    # Whatever the profile concludes about duplicates is passed through unchanged.
    p = profile_dataset(a)
    assert c.duplicates.original_duplicates == p.duplicate_rows
    if p.duplicate_rows is None:
        assert c.metric("duplicate_rows").direction == "not available"


def test_duplicates_not_assessed_gives_no_direction():
    from refine.compare import DuplicateChange

    d = DuplicateChange(3, 3, None, 0, None, 0, "Rows contain values that cannot be compared.", None)
    assert d.difference is None and d.direction == "not available" and d.original_pct is None


# ------------------------------------------------------------------
# Statistics
# ------------------------------------------------------------------

def test_numeric_statistics_from_the_profile():
    a = pd.DataFrame({"x": text("1", "2", "3", "100")})
    b = pd.DataFrame({"x": pd.array([1, 2, 3, None], dtype="Int64")})
    d = compare_datasets(a, b, row_ids=ids(4)).detail("x")
    stats = {m.key: m for m in d.numeric_changes()}
    assert stats["maximum"].original == 100 and stats["maximum"].current == 3
    assert stats["mean"].original == pytest.approx(26.5) and stats["mean"].current == pytest.approx(2.0)
    assert stats["median"].original == pytest.approx(2.5) and stats["median"].current == pytest.approx(2.0)
    assert stats["std"].current == pytest.approx(1.0)
    assert stats["count"].difference == -1 and stats["maximum"].direction == "decrease"
    assert d.has_numeric


def test_constant_and_single_value_columns():
    a = pd.DataFrame({"x": pd.array([5, 5, 5], dtype="Int64"), "y": pd.array([4, None, None], dtype="Int64")})
    d = compare_datasets(a, a, row_ids=ids(3))
    x = {m.key: m for m in d.detail("x").numeric_changes()}
    assert x["std"].original == 0.0 and x["std"].direction == "no change"
    y = {m.key: m for m in d.detail("y").numeric_changes()}
    assert y["std"].original is None and y["std"].direction == "not available"
    assert y["mean"].original == 4.0


def test_all_missing_column():
    a = pd.DataFrame({"e": text(None, "", None), "v": text("1", "2", "3")})
    b = a.copy()
    b["e"] = text("a", None, None)
    c = compare_datasets(a, b, row_ids=ids(3))
    e = c.column_missing("e")
    assert (e.original_missing, e.current_missing) == (3, 2)
    d = c.detail("e")
    assert not d.has_numeric
    assert {m.key: m.original for m in d.categorical_changes()}["distinct"] == 0   # Empty text is missing
    col = c.cells.column("e")
    assert (col.filled, col.modified, col.unchanged) == (1, 1, 1)


def test_categorical_statistics_and_top_values():
    a = pd.DataFrame({"c": text("NY", "ny", "NY", "LA", None)})
    b = pd.DataFrame({"c": text("NY", "NY", "NY", "LA", None)})
    d = compare_datasets(a, b, row_ids=ids(5)).detail("c")
    cat = {m.key: m for m in d.categorical_changes()}
    assert (cat["distinct"].original, cat["distinct"].current, cat["distinct"].direction) == (3, 2, "decrease")
    assert cat["missing"].difference == 0
    before, after = d.top_values()
    assert (before[0].value, before[0].count) == ("NY", 2) and (after[0].value, after[0].count) == ("NY", 3)
    assert not d.has_numeric


def test_details_for_added_and_removed_columns():
    a = base()
    b = a.drop(columns=["city"]).assign(n=pd.array([1, 2, 3, 4, 5, 6], dtype="Int64"))
    c = compare_datasets(a, b, row_ids=ids(6))
    n, city = c.detail("n"), c.detail("city")
    assert n.status == "added" and n.original is None
    assert {m.key: m for m in n.numeric_changes()}["mean"].original is None
    assert city.status == "removed" and city.current is None and city.top_values()[1] == ()


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

def test_empty_tables():
    a = pd.DataFrame({"v": pd.array([], dtype=TEXT_DTYPE)})
    c = compare_datasets(a, a, row_ids=np.zeros(0, dtype=np.int64))
    assert c.metric("rows").original == 0 and c.metric("missing_pct").original == 0.0
    assert c.cells.available and c.cells.cells_compared == 0
    assert c.column_missing("v").original_pct == 0.0


def test_all_rows_removed():
    a = base()
    b = a.iloc[[]].reset_index(drop=True)
    c = compare_datasets(a, b, row_ids=np.zeros(0, dtype=np.int64))
    assert c.cells.rows_removed == 6 and c.cells.cells_changed == 0
    assert c.column_missing("city").current_pct == 0.0


def test_no_columns():
    a = pd.DataFrame(index=range(3))
    c = compare_datasets(a, a, row_ids=ids(3))
    assert c.duplicates.original_duplicates is None and c.duplicates.original_error
    assert c.details == () and c.cells.columns == ()


def test_reports_passed_in_are_reused():
    a = base()
    p = profile_dataset(a)
    q = assess_quality(a, p)
    c = compare_datasets(a, a, row_ids=ids(6), original_profile=p, current_profile=p,
                         original_quality=q, current_quality=q)
    assert c.original_profile is p and c.current_quality is q


def test_type_errors():
    with pytest.raises(TypeError):
        compare_datasets([1, 2], base())


# ------------------------------------------------------------------
# History summary
# ------------------------------------------------------------------

def test_history_summary_from_session_records():
    from refine.session import CleaningSession

    s = CleaningSession(base())
    assert history_summary(s.steps, s.audit_log) == ()
    s.apply(s.preview("standardize_missing", {"columns": ["amount"], "tokens": ["NA"]}), confirmed=True)
    s.apply(s.preview("drop_columns", {"columns": ["city"]}), confirmed=True)
    items = history_summary(s.steps, s.audit_log)
    assert [i.number for i in items] == [1, 2]
    assert [i.operation for i in items] == ["standardize_missing", "drop_columns"]
    assert items[0].columns == ("amount",) and dict(items[0].settings)["tokens"] == "NA"
    assert items[1].columns_before == 3 and items[1].columns_after == 2
    assert all(isinstance(v, int) for _, v in items[0].counts)
    text_out = repr(items)
    assert s.fingerprint not in text_out and "fingerprint" not in text_out and "resolved" not in text_out


def test_history_settings_are_shortened():
    from refine.compare import _setting_text

    assert _setting_text(list("abcdefg")) == "a, b, c, d, e, and 2 more"
    assert _setting_text("x" * 100).endswith("...") and len(_setting_text("x" * 100)) == 60
    assert _setting_text(True) == "yes" and _setting_text(None) == "(none)" and _setting_text([]) == "(none)"


# ------------------------------------------------------------------
# Size
# ------------------------------------------------------------------

def test_large_table_compares_quickly():
    n = 200_000
    rng = np.random.default_rng(0)
    a = pd.DataFrame({f"c{i}": pd.array(rng.integers(0, 1000, n).astype(str), dtype=TEXT_DTYPE) for i in range(10)})
    keep = np.flatnonzero(rng.random(n) > 0.01)
    b = a.iloc[keep].reset_index(drop=True)
    b["c0"] = pd.array(pd.to_numeric(b["c0"]).astype("Int64"))            # Type change
    b.loc[b.index[:500], "c1"] = None                                       # Made missing
    start = time.perf_counter()
    cells = compare_cells(a, b, keep)
    elapsed = time.perf_counter() - start
    assert cells.rows_removed == n - len(keep)
    assert cells.column("c0").compared_as == "text" and cells.column("c0").changed == 0
    assert cells.column("c1").made_missing == 500
    assert sum(c.changed for c in cells.columns) == 500
    assert elapsed < 10          # Generous bound for slow CI machines
