"""CSV parsing: delimiters, quoting, value preservation, shape and headers."""

import pandas as pd
import pytest

from refine.errors import LoadError
from refine.source import TEXT_DTYPE
from tests.conftest import codes, column, load_csv

# ------------------------------------------------------------------
# Delimiters
# ------------------------------------------------------------------


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_delimiter_detected(delim):
    text = delim.join(["id", "name", "score"]) + "\n" + delim.join(["1", "Ann", "9"]) + "\n"
    ds = load_csv(text)
    assert ds.source.delimiter == delim and ds.source.delimiter_detected
    assert list(ds.data.columns) == ["id", "name", "score"]


def test_semicolon_file_with_decimal_commas():
    ds = load_csv("item;price;qty\nTea;1,50;2\nCake;12,25;1\n")
    assert ds.source.delimiter == ";"
    assert column(ds, "price") == ["1,50", "12,25"]


def test_delimiter_override():
    ds = load_csv("a;b\n1;2\n", delimiter=";")
    assert not ds.source.delimiter_detected
    assert ds.data.shape == (1, 2)


def test_override_that_splits_nothing_warns():
    ds = load_csv("a,b\n1,2\n", delimiter=";")
    assert ds.data.shape == (1, 1)
    assert ds.note("single_column").severity == "warning"


def test_single_column_file_is_reported():
    ds = load_csv("name\nAnn\nBo\n")
    assert ds.data.shape == (2, 1)
    assert ds.note("single_column").severity == "info"


@pytest.mark.parametrize("bad", ["", ";;", '"', "\n"])
def test_invalid_delimiter_rejected(bad):
    with pytest.raises(LoadError) as err:
        load_csv("a\n1\n", delimiter=bad)
    assert err.value.code == "bad_delimiter"


# ------------------------------------------------------------------
# Values are preserved exactly as text
# ------------------------------------------------------------------


def test_values_are_kept_verbatim_as_text():
    text = (
        "id,zip,amount,flag,marker,spaced,sci\n"
        '007,00501,"1,234.50",TRUE,NA,"  padded  ",1e5\n'
        "8,02134,-0.0,false,null,\ttab,1E-3\n"
    )
    ds = load_csv(text)
    assert all(dtype == TEXT_DTYPE for dtype in ds.data.dtypes)
    assert column(ds, "id") == ["007", "8"]
    assert column(ds, "zip") == ["00501", "02134"]
    assert column(ds, "amount") == ["1,234.50", "-0.0"]
    assert column(ds, "flag") == ["TRUE", "false"]
    assert column(ds, "marker") == ["NA", "null"]           # Not converted to missing
    assert column(ds, "spaced") == ["  padded  ", "\ttab"]  # Not trimmed
    assert column(ds, "sci") == ["1e5", "1E-3"]


def test_empty_field_is_empty_text_not_missing():
    ds = load_csv('a,b,c\n1,,""\n')
    assert column(ds, "b") == [""] and column(ds, "c") == [""]
    assert not ds.data.isna().any().any()


def test_header_whitespace_kept():
    ds = load_csv(" id ,Name \n1,2\n")
    assert list(ds.data.columns) == [" id ", "Name "]


def test_quoted_delimiters_newlines_and_escaped_quotes():
    text = 'id,comment\n1,"Hello, world"\n2,"line one\nline two"\n3,"She said ""hi"""\n4,5" monitor\n'
    ds = load_csv(text)
    assert column(ds, "comment") == ["Hello, world", "line one\nline two", 'She said "hi"', '5" monitor']
    assert codes(ds) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_line_endings(newline):
    ds = load_csv(newline.join(["a,b", "1,2", "3,4"]) + newline)
    assert column(ds, "b") == ["2", "4"]


def test_quoted_crlf_inside_value_is_kept():
    ds = load_csv('a,b\r\n1,"x\r\ny"\r\n')
    assert column(ds, "b") == ["x\r\ny"]


def test_no_trailing_newline():
    assert column(load_csv("a,b\n1,2"), "b") == ["2"]


def test_unicode_values():
    ds = load_csv("name,emoji\n李雷,👍\nÅsa,🇸🇪\n")
    assert column(ds, "name") == ["李雷", "Åsa"]


# ------------------------------------------------------------------
# Quoting problems
# ------------------------------------------------------------------


def test_unclosed_quote_is_an_error():
    with pytest.raises(LoadError) as err:
        load_csv('a,b\n1,"never closed\n2,3\n')
    assert err.value.code == "unclosed_quote"
    assert "line 2" in err.value.message


def test_stray_quote_parsed_leniently_with_warning():
    ds = load_csv('a,b\n1,"abc"def\n2,ok\n')
    note = ds.note("lenient_quotes")
    assert note.severity == "warning" and note.rows == (2,)
    assert column(ds, "a") == ["1", "2"]


def test_field_too_large(monkeypatch):
    import refine.csv_reader as reader
    monkeypatch.setattr(reader, "CSV_FIELD_SIZE_LIMIT", 100)
    with pytest.raises(LoadError) as err:
        load_csv("a\n" + "x" * 200 + "\n")
    assert err.value.code == "field_too_large"
    # Every load sets the limit again, so later tests are unaffected.


# ------------------------------------------------------------------
# Blank lines, ragged rows, empty columns
# ------------------------------------------------------------------


def test_blank_lines_skipped_and_reported_with_line_numbers():
    ds = load_csv("a,b\n1,2\n\n3,4\n\n\n5,6\n")
    assert column(ds, "a") == ["1", "3", "5"]
    note = ds.note("blank_lines_skipped")
    assert note.rows == (3, 5, 6)
    assert "3 completely blank lines" in note.message


def test_row_of_empty_fields_is_kept():
    ds = load_csv("a,b\n,\n1,2\n")
    assert ds.data.shape == (2, 2)
    assert column(ds, "a") == ["", "1"]


def test_short_rows_padded_with_missing_and_warned():
    ds = load_csv("a,b,c\n1,2,3\n4\n5,6\n")
    assert column(ds, "b") == ["2", None, "6"]
    assert column(ds, "c") == ["3", None, None]
    note = ds.note("short_rows_padded")
    assert note.severity == "warning" and note.rows == (3, 4)
    assert "2 rows have fewer values" in note.message


def test_rows_longer_than_header_are_an_error():
    with pytest.raises(LoadError) as err:
        load_csv("a,b\n1,2\n3,4,5\n6,7\n")
    assert err.value.code == "ragged_rows"
    assert "line 3" in err.value.message


def test_trailing_empty_fields_beyond_header_ignored():
    ds = load_csv("a,b\n1,2,\n3,4,,\n")
    assert ds.data.shape == (2, 2)
    assert ds.note("extra_empty_fields").rows == (2, 3)


def test_trailing_delimiter_on_every_line_keeps_empty_unnamed_column():
    ds = load_csv("a,b,\n1,2,\n3,4,\n")
    assert list(ds.data.columns) == ["a", "b", "column_3"]
    assert column(ds, "column_3") == ["", ""]          # Empty CSV fields stay empty text
    assert ds.note("blank_header").columns == ("column_3",)
    note = ds.note("empty_unnamed_columns")
    assert note.severity == "info" and note.columns == ("column_3",)
    assert "kept as 'column_3', not removed" in note.message
    assert "short_rows_padded" not in codes(ds)


def test_several_empty_unnamed_columns_get_unique_deterministic_names():
    # 'column_2' is already a real header, so the blank at position 2 must not reuse it.
    ds = load_csv("a,,column_2,,\n1,,x,,\n2,,y,,\n")
    assert list(ds.data.columns) == ["a", "column_2_2", "column_2", "column_4", "column_5"]
    assert ds.note("empty_unnamed_columns").columns == ("column_2_2", "column_4", "column_5")
    assert load_csv("a,,column_2,,\n1,,x,,\n2,,y,,\n").notes == ds.notes


def test_empty_unnamed_column_with_short_rows_is_missing_not_text():
    ds = load_csv("a,b,\n1,2,\n3\n")
    assert column(ds, "column_3") == ["", None]
    assert "short_rows_padded" in codes(ds)


def test_blank_header_with_values_is_kept_and_named():
    ds = load_csv("a,,c\n1,x,3\n")
    assert list(ds.data.columns) == ["a", "column_2", "c"]
    assert column(ds, "column_2") == ["x"]
    note = ds.note("blank_header")
    assert note.severity == "warning" and note.columns == ("column_2",)


def test_header_with_no_data_values_is_kept():
    ds = load_csv("a,b\n1,\n2,\n")
    assert list(ds.data.columns) == ["a", "b"]   # Named columns are never dropped


def test_source_rows_point_to_file_lines():
    ds = load_csv('a,b\n1,"multi\nline"\n\n2,x\n')
    assert ds.source_rows.tolist() == [2, 5]


# ------------------------------------------------------------------
# Headers
# ------------------------------------------------------------------


def test_duplicate_headers_renamed_and_reported():
    ds = load_csv("id,email,email,email\n1,a,b,c\n")
    assert list(ds.data.columns) == ["id", "email", "email_2", "email_3"]
    assert ds.source.header_labels == ("id", "email", "email", "email")
    note = ds.note("duplicate_header")
    assert note.severity == "warning"
    assert note.columns == ("email_2", "email_3")
    assert "'email' at position 3 → 'email_2'" in note.message


def test_duplicate_rename_avoids_existing_names():
    ds = load_csv("x,x,x_2\n1,2,3\n")
    assert list(ds.data.columns) == ["x", "x_3", "x_2"]
    assert len(set(ds.data.columns)) == 3


def test_case_variants_are_not_duplicates():
    ds = load_csv("ID,id\n1,2\n")
    assert list(ds.data.columns) == ["ID", "id"]
    assert "duplicate_header" not in codes(ds)


def test_blank_header_name_avoids_collision():
    ds = load_csv("column_2,,b\n1,2,3\n")
    assert len(set(ds.data.columns)) == 3
    assert ds.data.columns[1] != "column_2"


def test_skip_rows_above_header():
    ds = load_csv("Quarterly export\nGenerated 2026-10-01\nid,value\n1,2\n", skip_rows=2)
    assert list(ds.data.columns) == ["id", "value"]
    note = ds.note("rows_skipped")
    assert note.rows == (1, 2)


def test_skip_rows_counts_non_blank_rows():
    ds = load_csv("Title\n\nid,value\n1,2\n", skip_rows=1)
    assert list(ds.data.columns) == ["id", "value"]


def test_skip_too_many_rows():
    with pytest.raises(LoadError) as err:
        load_csv("a\n1\n", skip_rows=5)
    assert err.value.code == "skip_too_many"


def test_negative_skip_rows():
    with pytest.raises(LoadError) as err:
        load_csv("a\n1\n", skip_rows=-1)
    assert err.value.code == "bad_option"


# ------------------------------------------------------------------
# Empty inputs
# ------------------------------------------------------------------


@pytest.mark.parametrize("text", ["   ", "\n\n\n", " \r\n \t\n"])
def test_whitespace_only_file(text):
    with pytest.raises(LoadError) as err:
        load_csv(text)
    assert err.value.code == "empty_file"


def test_header_only_file():
    with pytest.raises(LoadError) as err:
        load_csv("id,name,email\n")
    assert err.value.code == "header_only"


def test_all_cells_empty():
    with pytest.raises(LoadError) as err:
        load_csv(",,\n,,\n")
    assert err.value.code == "no_values"


def test_result_has_default_index():
    ds = load_csv("a\n1\n2\n3\n")
    assert isinstance(ds.data.index, pd.RangeIndex) and ds.data.index.tolist() == [0, 1, 2]
