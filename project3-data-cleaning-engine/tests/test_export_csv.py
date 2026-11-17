"""CSV export: structure, text forms of typed values, missing values, Unicode, formula escaping, round trips."""

import csv
import datetime as dt
import io

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from refine.export import (
    CSV_MIME,
    csv_bytes,
    csv_export,
    export_file_name,
    file_stem,
    inspect_export,
    is_formula_like,
    text_values,
)
from refine.loader import load_file
from refine.source import TEXT_DTYPE


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def typed_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "name": text("Ana", "Zoë", None, "", "日本"),
        "count": pd.array([1, None, -3, 0, 2 ** 53 + 1], dtype="Int64"),
        "amount": pd.array([0.1, 1 / 3, None, 76.0, 1e-7], dtype="Float64"),
        "flag": pd.array([True, False, None, True, False], dtype="boolean"),
        "day": pd.Series(pd.arrays.ArrowExtensionArray(
            pa.array([dt.date(2026, 1, 15), None, dt.date(1899, 12, 31), dt.date(2000, 2, 29), dt.date(2026, 1, 1)],
                     pa.date32()))),
        "at": pd.Series(pd.to_datetime(["2026-01-15 14:30:00", None, "2026-01-15 14:30:00.123456",
                                        "1950-06-01 00:00:00", "2026-01-01 00:00:00"], format="ISO8601").as_unit("us")),
    })


# ---- structure -------------------------------------------------------

def test_header_rows_order_and_no_index():
    f = typed_frame()
    f.index = [10, 10, 3, 2, 1]                       # Index labels are never written
    out = rows(csv_bytes(f)[0])
    assert out[0] == ["name", "count", "amount", "flag", "day", "at"]
    assert len(out) == 1 + len(f)
    assert [r[0] for r in out[1:]] == ["Ana", "Zoë", "", "", "日本"]


def test_utf8_without_bom_and_crlf_line_endings():
    data, _ = csv_bytes(typed_frame())
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("utf-8")                              # Valid UTF-8
    assert data.count(b"\r\n") == 6 and b"\n" not in data.replace(b"\r\n", b"")


def test_column_order_is_preserved():
    f = pd.DataFrame({"z": text("1"), "a": text("2"), "m": text("3")})
    assert rows(csv_bytes(f)[0]) == [["z", "a", "m"], ["1", "2", "3"]]


def test_typed_values_use_refine_text_forms():
    out = rows(csv_bytes(typed_frame())[0])
    count = [r[1] for r in out[1:]]
    amount = [r[2] for r in out[1:]]
    assert count == ["1", "", "-3", "0", str(2 ** 53 + 1)]           # No float rounding of large integers
    assert amount == ["0.1", "0.3333333333333333", "", "76.0", "1e-07"]
    assert [float(v) for v in amount if v] == [0.1, 1 / 3, 76.0, 1e-7]  # Shortest exact form reads back exactly
    assert [r[3] for r in out[1:]] == ["true", "false", "", "true", "false"]
    assert [r[4] for r in out[1:]] == ["2026-01-15", "", "1899-12-31", "2000-02-29", "2026-01-01"]
    assert [r[5] for r in out[1:]] == ["2026-01-15 14:30:00", "", "2026-01-15 14:30:00.123456",
                                       "1950-06-01 00:00:00", "2026-01-01 00:00:00"]


def test_utc_datetimes_keep_their_offset():
    f = pd.DataFrame({"at": pd.Series(pd.to_datetime(["2026-01-15 14:30:00"]).as_unit("us")).dt.tz_localize("UTC")})
    assert rows(csv_bytes(f)[0])[1] == ["2026-01-15 14:30:00+00:00"]


def test_missing_and_empty_text_are_both_empty_fields():
    f = pd.DataFrame({"a": text(None, "", "x")})
    data, _ = csv_bytes(f)
    assert data == b'a\r\n""\r\n""\r\nx\r\n'          # A lone empty field is quoted, so no row becomes a blank line


def test_quoting_of_delimiters_quotes_and_line_breaks():
    f = pd.DataFrame({"a": text("x,y", 'say "hi"', "two\nlines", "cr\rhere", "  spaces  ")})
    out = rows(csv_bytes(f)[0])
    assert [r[0] for r in out[1:]] == ["x,y", 'say "hi"', "two\nlines", "cr\rhere", "  spaces  "]


def test_unicode_text_is_kept():
    f = pd.DataFrame({"città": text("naïve café", "🎉 emoji", "Ελληνικά", "\u200bzero-width")})
    out = rows(csv_bytes(f)[0])
    assert out[0] == ["città"] and [r[0] for r in out[1:]] == ["naïve café", "🎉 emoji", "Ελληνικά", "\u200bzero-width"]


def test_empty_tables():
    no_rows = pd.DataFrame({"a": text(), "b": pd.array([], dtype="Int64")})
    assert csv_bytes(no_rows) == (b"a,b\r\n", 0)
    assert csv_bytes(pd.DataFrame()) == (b"", 0)


def test_nullable_dtypes_all_missing():
    f = pd.DataFrame({"i": pd.array([None, None], dtype="Int64"), "f": pd.array([None, None], dtype="Float64"),
                      "b": pd.array([None, None], dtype="boolean")})
    assert rows(csv_bytes(f)[0]) == [["i", "f", "b"], ["", "", ""], ["", "", ""]]


def test_object_columns_are_written_per_cell():
    f = pd.DataFrame({"o": pd.Series([1, True, 1.0, None, "x"], dtype=object)})
    assert [r[0] for r in rows(csv_bytes(f)[0])[1:]] == ["1", "True", "1.0", "", "x"]


def test_same_table_gives_same_bytes_and_input_is_not_changed():
    f = typed_frame()
    before = f.copy(deep=True)
    assert csv_bytes(f) == csv_bytes(f)
    pd.testing.assert_frame_equal(f, before)


def test_text_values_matches_column_length():
    f = typed_frame()
    for name in f.columns:
        assert len(text_values(f[name])) == len(f)


# ---- formula-like text ----------------------------------------------------

@pytest.mark.parametrize("value", ["=SUM(A1:A2)", "=1+1", "@SUM(1)", "+1 555 0100", "-2+3", "\t=1", "\rcmd",
                                   "=", "-cmd|' /C calc'!A0"])
def test_formula_like(value):
    assert is_formula_like(value)


@pytest.mark.parametrize("value", ["-12.5", "+3", "-1e5", "-", "+", "", "a=b", " =1", "email@x.com", "12-3"])
def test_not_formula_like(value):
    assert not is_formula_like(value)


def test_values_are_exact_by_default_and_counted():
    f = pd.DataFrame({"a": text("=1+1", "-5", "ok", "@x", None), "n": pd.array([-1, 2, 3, 4, 5], dtype="Int64")})
    data, escaped = csv_bytes(f)
    assert escaped == 0 and [r[0] for r in rows(data)[1:]] == ["=1+1", "-5", "ok", "@x", ""]
    check = inspect_export(f)
    assert check.formula_like_cells == 2 and check.formula_like_columns == ("a",)


def test_escaping_prefixes_an_apostrophe_and_reports_the_count():
    f = pd.DataFrame({"=head": text("=1+1", "-5", "ok", "@x", None, "+1 (555)"),
                      "n": pd.array([-1, 2, 3, 4, 5, 6], dtype="Int64")})
    data, escaped = csv_bytes(f, escape_formulas=True)
    out = rows(data)
    assert out[0] == ["'=head", "n"]
    assert [r[0] for r in out[1:]] == ["'=1+1", "-5", "ok", "'@x", "", "'+1 (555)"]
    assert [r[1] for r in out[1:]] == ["-1", "2", "3", "4", "5", "6"]   # Numbers are never escaped
    assert escaped == 4
    assert inspect_export(f).formula_like_cells == 4                     # The page's count is the same
    assert f["=head"].tolist()[0] == "=1+1"                              # The table is not changed


def test_typed_columns_are_never_formula_like():
    f = pd.DataFrame({"n": pd.array([-5, -1], dtype="Int64"), "x": pd.array([-0.5, -2.0], dtype="Float64")})
    assert inspect_export(f).formula_like_cells == 0
    assert csv_bytes(f, escape_formulas=True)[1] == 0


# ---- round trips through REFINE's loader ----------------------------------

def test_round_trip_sample_text_columns(sample_bytes):
    ds = load_file("messy_customers.csv", sample_bytes)
    data, _ = csv_bytes(ds.data)
    again = load_file("again.csv", data)
    assert again.source.encoding == "utf-8" and again.source.delimiter == ","
    assert list(again.data.columns) == list(ds.data.columns) and again.data.shape == ds.data.shape
    pd.testing.assert_frame_equal(again.data.fillna(""), ds.data.fillna(""), check_dtype=False)


def test_round_trip_typed_columns_as_text():
    f = typed_frame()
    again = load_file("t.csv", csv_bytes(f)[0]).data
    for name in f.columns:
        expected = [v if v is not None else "" for v in text_values(f[name])]
        assert again[name].fillna("").tolist() == expected


# ---- file names ---------------------------------------------------------

@pytest.mark.parametrize("name, stem", [
    ("messy_customers.csv", "messy_customers"), ("../../etc/passwd", "passwd"), ("C:\\data\\Q1 report.xlsx",
                                                                                 "Q1_report"),
    ("Café résumé.csv", "Cafe_resume"), ("日本.csv", "dataset"), ("", "dataset"), (None, "dataset"),
    (".hidden", "hidden"), ("a" * 200 + ".csv", "a" * 60), ("x<>:|?*.csv", "x"),
])
def test_file_stem_is_safe(name, stem):
    assert file_stem(name) == stem


def test_export_file_names_and_metadata():
    f = typed_frame()
    exp = csv_export(f, source_name="sales 2026.csv")
    assert exp.file_name == "sales_2026_cleaned.csv" and exp.mime == CSV_MIME
    assert (exp.rows, exp.columns, exp.size) == (5, 6, len(exp.data))
    assert csv_export(f, source_name="a.csv", cleaned=False).file_name == "a_original.csv"
    assert export_file_name("x.csv", "recipe", "json") == "x_recipe.json"


def test_large_numbers_are_not_rounded():
    big = pd.DataFrame({"i": pd.array([np.iinfo(np.int64).max, np.iinfo(np.int64).min + 1], dtype="Int64"),
                        "f": pd.array([1.0000000000000002, 123456789.12345679], dtype="Float64")})
    out = rows(csv_bytes(big)[0])
    assert [r[0] for r in out[1:]] == [str(np.iinfo(np.int64).max), str(np.iinfo(np.int64).min + 1)]
    assert [float(r[1]) for r in out[1:]] == [1.0000000000000002, 123456789.12345679]


def test_rows_stay_in_order_across_chunks():
    from refine.export import CSV_ROW_CHUNK
    n = CSV_ROW_CHUNK * 2 + 17
    f = pd.DataFrame({"a": text(*[f"r{i}" for i in range(n)]),
                      "b": pd.array([None if i % 3 else i for i in range(n)], dtype="Int64")})
    out = rows(csv_bytes(f)[0])
    assert len(out) == n + 1
    assert [r[0] for r in out[1:]] == [f"r{i}" for i in range(n)]
    assert [r[1] for r in out[1:]] == ["" if i % 3 else str(i) for i in range(n)]


def test_formula_count_matches_escaping_on_a_large_column():
    vals = ["=1", "-5", "@a", "x", None, "+1 2", "-", "\tz"] * 3000
    f = pd.DataFrame({"a": text(*vals)})
    assert inspect_export(f).formula_like_cells == csv_bytes(f, escape_formulas=True)[1] == 4 * 3000
