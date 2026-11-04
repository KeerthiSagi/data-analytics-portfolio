"""Dataset and column profiling (refine.profile)."""

import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from refine.profile import classify_date, profile_column, profile_dataset
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR, load_csv, load_xlsx, make_xlsx


def text(values, name="c"):
    return pd.Series(values, dtype=TEXT_DTYPE, name=name)


def frame(**cols):
    return pd.DataFrame({k: pd.array(v, dtype=TEXT_DTYPE) for k, v in cols.items()})


# ------------------------------------------------------------------
# Dataset level
# ------------------------------------------------------------------

def test_dataset_counts():
    df = frame(a=["1", "2", "2", None], b=["x", "", "", "y"])
    p = profile_dataset(df)
    assert (p.n_rows, p.n_columns, p.n_cells) == (4, 2, 8)
    assert p.missing_cells == 3                     # one NA + two empty text
    assert p.missing_pct == pytest.approx(37.5)
    assert p.duplicate_rows == 1 and p.duplicate_groups == 1
    assert p.duplicate_pct == pytest.approx(25.0)
    assert p.duplicate_examples == (2,)
    assert p.memory_bytes == int(df.memory_usage(deep=True).sum())
    assert p.storage_type_counts == {str(TEXT_DTYPE): 2}
    assert sum(p.semantic_type_counts.values()) == 2


def test_duplicates_count_repeats_not_first_occurrence():
    df = frame(a=["1", "1", "1", "2", "2", "3"])
    p = profile_dataset(df)
    assert p.duplicate_rows == 3 and p.duplicate_groups == 2


def test_duplicates_are_exact_text():
    df = frame(a=["x", "x ", "X"])
    assert profile_dataset(df).duplicate_rows == 0


def test_missing_values_compare_equal_for_duplicates():
    df = frame(a=[None, None], b=["", ""])
    assert profile_dataset(df).duplicate_rows == 1


def test_empty_columns_reported():
    df = frame(a=["1", "2"], b=[None, ""], c=["  ", None])
    p = profile_dataset(df)
    assert p.empty_columns == ("b", "c")
    assert p.column("b").inference.semantic_type == "empty"


def test_single_row_and_single_column():
    p = profile_dataset(frame(a=["only"]))
    assert p.n_cells == 1 and p.duplicate_rows == 0
    assert p.column("a").inference.confidence == "low"


def test_zero_row_table():
    df = pd.DataFrame({"a": pd.array([], dtype=TEXT_DTYPE)})
    p = profile_dataset(df)
    assert p.n_rows == 0 and p.missing_pct == 0.0 and p.duplicate_rows == 0
    assert p.column("a").is_empty


def test_zero_column_table_duplicates_not_assessed():
    p = profile_dataset(pd.DataFrame(index=range(3)))
    assert p.n_columns == 0 and p.duplicate_rows is None and p.duplicate_error


def test_rejects_non_unique_column_names_and_non_frames():
    df = pd.DataFrame([["1", "2"]], columns=["a", "a"])
    with pytest.raises(ValueError):
        profile_dataset(df)
    with pytest.raises(TypeError):
        profile_dataset([["1"]])


def test_input_is_not_mutated(sample_bytes):
    from refine.loader import load_file

    ds = load_file("s.csv", sample_bytes)
    before = ds.data.copy(deep=True)
    dtypes = ds.data.dtypes.copy()
    profile_dataset(ds.data)
    pd.testing.assert_frame_equal(ds.data, before)
    assert (ds.data.dtypes == dtypes).all()


def test_profile_is_deterministic(sample_bytes):
    from refine.loader import load_file

    df = load_file("s.csv", sample_bytes).data
    assert profile_dataset(df) == profile_dataset(df)     # profile_seconds is excluded from equality


# ------------------------------------------------------------------
# Missing, empty text, whitespace, null-like tokens
# ------------------------------------------------------------------

def test_missing_distinguishes_na_empty_whitespace_and_tokens():
    c = profile_column(text([None, "", "  ", "NA", "n/a", "null", "None", "value"]))
    assert c.null_count == 1 and c.empty_text_count == 1
    assert c.missing_count == 2 and c.missing_pct == pytest.approx(25.0)
    assert c.whitespace_only_count == 1
    assert c.null_token_count == 4
    assert {t.value for t in c.null_tokens} == {"NA", "n/a", "null", "None"}
    assert c.values_checked == 1


def test_tokens_matched_after_trim_and_case_but_kept_as_written():
    c = profile_column(text([" NULL ", "N/A", "-", "#N/A", "nan", "unknown"]))
    assert c.null_token_count == 5
    assert " NULL " in {t.value for t in c.null_tokens}
    assert "unknown" not in {t.value for t in c.null_tokens}    # A legitimate category, not a token


def test_whitespace_counts():
    c = profile_column(text([" a", "b ", " c ", "d  e", "f", "\tg"]))
    assert c.leading_whitespace_count == 3
    assert c.trailing_whitespace_count == 2
    assert c.edge_whitespace_count == 4            # " c " counted once
    assert c.repeated_space_count == 1
    assert {v.value for v in c.whitespace_examples} >= {" a", "d  e"}


def test_distinct_and_top_values_exact_text_and_deterministic():
    c = profile_column(text(["b", "a", "a", "USA", "usa", None, ""]))
    assert c.distinct_count == 4                   # "" is missing, not a distinct value
    assert c.distinct_pct == pytest.approx(80.0)
    assert [v.value for v in c.top_values] == ["a", "USA", "b", "usa"]   # Count desc, then text
    assert c.top_values[0].count == 2 and c.top_values[0].share == pytest.approx(100 * 2 / 7)


def test_length_statistics():
    c = profile_column(text(["a", "abc", "abc", None]))
    assert (c.length.minimum, c.length.maximum) == (1, 3)
    assert c.length.mean == pytest.approx(7 / 3) and c.length.median == 3


# ------------------------------------------------------------------
# Semantic type inference
# ------------------------------------------------------------------

@pytest.mark.parametrize("values, expected", [
    (["1", "2", "3", "40", "-5"], "integer"),
    (["1.5", "2", "3.25", "1e3", ".5"], "decimal"),
    (["43", "43.0", "12", "7", "8"], "integer"),              # Integral values written as 43.0
    (["2024-01-05", "2023-12-31", "2022-02-28", "2021-07-04", "2020-01-01"], "date"),
    (["2024-01-05 10:30:00", "2024-01-06 11:00", "2024-01-07", "2024-01-08", "2024-01-09"], "datetime"),
    (["yes", "no", "Yes", "N", "true", "0", "1"], "boolean"),
    (["a@x.com", "b@y.org", "c@z.co", "d@w.io", "e@v.net"], "email"),
])
def test_inferred_types(values, expected):
    inf = profile_column(text(values)).inference
    assert inf.semantic_type == expected
    assert inf.status == "inferred"


def test_zero_one_only_is_integer_not_boolean():
    assert profile_column(text(["1", "0", "1", "0", "1"])).inference.semantic_type == "integer"


def test_categorical_identifier_and_text():
    cat = profile_column(text(["Red", "Blue", "Red", "Green", "Blue", "Red"] * 5))
    assert cat.inference.semantic_type == "categorical"
    ident = profile_column(text([f"C-{i:04d}" for i in range(50)]))
    assert ident.inference.semantic_type == "identifier"
    free = profile_column(text([f"note number {i} about things" for i in range(60)]))
    assert free.inference.semantic_type == "text"


def test_mostly_numeric_column_with_text_is_reported():
    c = profile_column(text(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "unknown"]))
    assert c.inference.semantic_type == "integer" and c.inference.confidence == "medium"
    assert c.numeric_plain_count == 10
    assert [v.value for v in c.non_numeric_examples] == ["unknown"]


def test_mixed_column():
    c = profile_column(text(["1", "2", "3", "abc", "def", "4"]))
    assert c.inference.semantic_type == "mixed"
    assert c.inference.dominant_type == "integer"
    assert c.inference.label == "Mixed (mostly integer)"
    assert c.numeric_stats is not None          # Mostly numeric, so stats are shown


def test_confidence_levels():
    assert profile_column(text(["1", "2", "3", "4", "5"])).inference.confidence == "high"
    assert profile_column(text(["1", "2"])).inference.confidence == "low"      # Too few values


def test_only_tokens_is_unknown_type():
    c = profile_column(text(["NA", "null", "-"]))
    assert c.inference.semantic_type == "unknown" and not c.is_empty


def test_confirmed_types_from_storage():
    df = pd.DataFrame({
        "i": pd.array([1, 2, None], dtype="Int64"),
        "f": [1.5, np.nan, 2.5],
        "b": [True, False, True],
        "d": pd.to_datetime(["2024-01-01", None, "2024-01-03"]),
    })
    p = profile_dataset(df)
    assert p.column("i").inference.semantic_type == "integer"
    assert p.column("f").inference.semantic_type == "decimal"
    assert p.column("b").inference.semantic_type == "boolean"
    assert p.column("d").inference.semantic_type == "datetime"
    for c in p.columns:
        assert c.inference.status == "confirmed" and not c.text_values
    assert p.column("i").null_count == 1 and p.column("f").missing_count == 1
    assert p.column("i").numeric_stats.mean == pytest.approx(1.5)


def test_object_column_with_mixed_python_types():
    s = pd.Series(["a", 1, 2.5, None, np.nan, "b"], dtype=object, name="m")
    c = profile_column(s)
    assert c.null_count == 2
    assert c.python_types == ("float", "int")
    assert c.inference.status == "inferred"
    assert s.tolist()[1] == 1                    # Not converted


def test_python_backed_and_default_string_dtypes():
    for dtype in ("string", "object"):
        c = profile_column(pd.Series(["1", "2", None, "3", "4", "5"], dtype=dtype, name="s"))
        assert c.inference.semantic_type == "integer" and c.null_count == 1


def test_unhashable_values_do_not_crash():
    df = pd.DataFrame({"x": pd.Series([[1], [1], "a"], dtype=object)})
    p = profile_dataset(df)       # Must not raise; duplicates are either counted or not assessed
    assert p.duplicate_rows in (None, 1)
    assert (p.duplicate_rows is None) == (p.duplicate_error is not None)


# ------------------------------------------------------------------
# Numeric and date indicators
# ------------------------------------------------------------------

def test_numeric_formatted_values_counted_but_excluded_from_stats():
    c = profile_column(text(["100", "200", "$1,234.50", "1,235", "(50)", "12%", "1,23", "300"]))
    assert c.numeric_plain_count == 3
    assert c.numeric_formatted_count == 4          # "1,23" is not a valid grouping
    assert c.numeric_stats.count == 3 and c.numeric_stats.maximum == 300


def test_numeric_stats_values():
    c = profile_column(text(["1", "2", "3", "4", "-10", "0", " 5 "]))
    s = c.numeric_stats
    assert (s.minimum, s.maximum, s.median) == (-10, 5, 2)
    assert s.mean == pytest.approx(5 / 7)
    assert s.negatives == 1 and s.zeros == 1 and s.all_integral


def test_infinite_values_excluded_from_stats():
    c = profile_column(text(["1e400", "1", "2", "3", "4"]))
    assert c.numeric_stats.count == 4


def test_no_numeric_stats_for_boolean_column():
    c = profile_column(text(["yes", "no", "1", "0", "yes"]))
    assert c.inference.semantic_type == "boolean" and c.numeric_stats is None


@pytest.mark.parametrize("value, label, valid, ambiguous", [
    ("2024-01-05", "YYYY-MM-DD", True, False),
    ("2024-02-30", "YYYY-MM-DD", False, False),
    ("01/05/2024", "MM/DD/YYYY or DD/MM/YYYY", True, True),
    ("13/05/2024", "MM/DD/YYYY or DD/MM/YYYY", True, False),     # Only day-first works
    ("05/05/2024", "MM/DD/YYYY or DD/MM/YYYY", True, False),     # Same either way
    ("05.01.2024", "DD.MM.YYYY or MM.DD.YYYY", True, True),       # 5 Jan or 1 May: flagged, not assumed
    ("Jan 5, 2024", "Mon D, YYYY", True, False),
    ("5 March 2024", "D Mon YYYY", True, False),
    ("2024-01-05 25:00", "YYYY-MM-DD", False, False),
    ("hello", None, False, False),
])
def test_classify_date(value, label, valid, ambiguous):
    found, ok, amb, _ = classify_date(value)
    assert (found, ok, amb) == (label, valid, ambiguous)


def test_date_indicators():
    c = profile_column(text(["2024-01-05", "2024-01-06", "01/05/2024", "05.01.2024", "Jan 5, 2024",
                             "2024-02-30", "soon"]))
    assert c.date_count == 5 and c.date_invalid_count == 1 and c.date_ambiguous_count == 2
    assert [(f.label, f.count) for f in c.date_formats][0] == ("YYYY-MM-DD", 2)
    assert len(c.date_formats) == 4
    assert {v.value for v in c.non_date_examples} == {"2024-02-30", "soon"}


# ------------------------------------------------------------------
# Capitalization
# ------------------------------------------------------------------

def test_case_variant_groups():
    c = profile_column(text(["USA", "USA", "usa", "Usa", " usa ", "Canada", "canada", "Mexico"]))
    assert c.case_variant_group_count == 2
    first = c.case_variant_groups[0]
    assert first.variants[0].value == "USA" and first.minority_count == 3   # " usa " groups with "usa"
    assert c.case_variant_cells == 4


def test_case_styles():
    c = profile_column(text(["Alice Smith", "BOB JONES", "carol white", "McDonald", "123"]))
    assert c.case_styles == {"upper": 1, "lower": 1, "title": 1}


# ------------------------------------------------------------------
# Sample, loader integration, independence
# ------------------------------------------------------------------

def test_sample_profile_matches_documented_defects(sample_bytes):
    from refine.loader import load_file

    p = profile_dataset(load_file("s.csv", sample_bytes).data)
    assert p.duplicate_rows == 12
    assert p.column("signup_date").inference.semantic_type == "date"
    assert len(p.column("signup_date").date_formats) == 4
    assert p.column("annual_spend").numeric_formatted_count > 0
    assert p.column("age").non_numeric_examples[0].value == "unknown"
    assert p.column("is_active").inference.semantic_type == "boolean"
    assert p.column("email").inference.semantic_type == "email"
    assert {t.value for t in p.column("email").null_tokens} == {"NA", "N/A", "null", "-"}
    assert p.empty_columns == ()


def test_empty_unnamed_csv_column_is_profiled_as_empty():
    ds = load_csv("a,b,\n1,2,\n3,4,\n")
    p = profile_dataset(ds.data)
    assert p.empty_columns == ("column_3",)
    assert p.column("column_3").empty_text_count == 2


def test_empty_unnamed_xlsx_column_is_profiled_as_empty():
    ds = load_xlsx(make_xlsx({"S": [["id", None, "v"], [1, None, "x"], [2, None, "y"]]}))
    p = profile_dataset(ds.data)
    assert p.empty_columns == ("column_2",)
    assert p.column("column_2").null_count == 2


def test_profile_module_does_not_import_streamlit():
    code = (
        "import sys, refine.profile, refine.quality, pandas as pd; "
        "from refine.source import TEXT_DTYPE; "
        "df = pd.DataFrame({'a': pd.array(['1', 'NA'], dtype=TEXT_DTYPE)}); "
        "p = refine.profile.profile_dataset(df); refine.quality.assess_quality(df, p); "
        "sys.exit(1 if 'streamlit' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or "profiling imported streamlit"
