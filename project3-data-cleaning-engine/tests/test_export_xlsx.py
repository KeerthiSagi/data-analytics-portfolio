"""Excel export: a valid workbook read back with openpyxl and pandas, cell types, limits and safety."""

import datetime as dt
import io
import re
import zipfile

import numpy as np
import openpyxl
import pandas as pd
import pyarrow as pa
import pytest

from refine.export import XLSX_MIME, xlsx_export
from refine.loader import load_file
from refine.source import TEXT_DTYPE
from refine.xlsx_writer import (
    DEFAULT_SHEET_NAME,
    EXCEL_MAX_CELL_CHARS,
    EXCEL_MAX_COLUMNS,
    EXCEL_MAX_ROWS,
    ExcelLimitError,
    check_sheet_name,
    column_letter,
    excel_adjustments,
    limit_error,
    write_xlsx,
)


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def sheet(data: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == [DEFAULT_SHEET_NAME]
    return wb[DEFAULT_SHEET_NAME]


def values(data: bytes) -> list[tuple]:
    return list(sheet(data).iter_rows(values_only=True))


def sheet_xml(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode("utf-8")


def dates(*days):
    return pd.Series(pd.arrays.ArrowExtensionArray(pa.array(list(days), pa.date32())))


def stamps(*texts):
    return pd.Series(pd.to_datetime(list(texts), format="ISO8601").as_unit("us"))


# ---- workbook structure ---------------------------------------------------

def test_valid_workbook_with_header_rows_and_order():
    f = pd.DataFrame({"z": text("a", "b"), "a": pd.array([1, 2], dtype="Int64")})
    f.index = [7, 7]                                   # Index labels are not written
    data = write_xlsx(f)
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None
    assert values(data) == [("z", "a"), ("a", 1), ("b", 2)]
    ws = sheet(data)
    assert (ws.max_row, ws.max_column) == (3, 2)
    assert ws["A1"].font.b                             # Header row in bold
    assert ws.freeze_panes == "A2"


def test_pandas_reads_it_back():
    f = pd.DataFrame({"t": text("x", None, "z"), "i": pd.array([1, None, 3], dtype="Int64"),
                      "b": pd.array([True, False, None], dtype="boolean")})
    back = pd.read_excel(io.BytesIO(write_xlsx(f)), sheet_name=DEFAULT_SHEET_NAME)
    assert list(back.columns) == ["t", "i", "b"] and back.shape == (3, 3)
    assert back["t"].isna().tolist() == [False, True, False]
    assert back["i"].tolist()[0] == 1 and np.isnan(back["i"].tolist()[1])


def test_same_table_gives_same_bytes_and_frame_is_not_changed():
    f = pd.DataFrame({"t": text("a", "=1"), "d": dates(dt.date(2026, 1, 1), None)})
    before = f.copy(deep=True)
    assert write_xlsx(f) == write_xlsx(f)
    pd.testing.assert_frame_equal(f, before)


def test_empty_tables():
    assert values(write_xlsx(pd.DataFrame({"a": text(), "b": pd.array([], dtype="Int64")}))) == [("a", "b")]
    assert values(write_xlsx(pd.DataFrame())) == []


def test_export_metadata():
    exp = xlsx_export(pd.DataFrame({"a": text("x")}), source_name="Q1 sales.xlsx")
    assert exp.file_name == "Q1_sales_cleaned.xlsx" and exp.mime == XLSX_MIME
    assert (exp.rows, exp.columns) == (1, 1) and exp.adjustments.fits


# ---- cell values ----------------------------------------------------------

def test_missing_values_are_empty_cells():
    f = pd.DataFrame({"t": text(None, "x"), "i": pd.array([None, 1], dtype="Int64"),
                      "f": pd.array([None, 1.5], dtype="Float64"), "b": pd.array([None, True], dtype="boolean"),
                      "d": dates(None, dt.date(2026, 1, 1)), "at": stamps(None, "2026-01-01 10:00:00")})
    assert values(write_xlsx(f))[1] == (None,) * 6
    assert '<c r="A2"' not in sheet_xml(write_xlsx(f))


def test_empty_text_is_an_empty_string_cell():
    ws = sheet(write_xlsx(pd.DataFrame({"t": text("", "x")})))
    assert ws["A2"].value == "" and ws["A2"].data_type == "s"


def test_numbers_booleans_and_types():
    f = pd.DataFrame({"i": pd.array([1, -2, 999_999_999_999_999], dtype="Int64"),
                      "f": pd.array([0.1, -2.5, 1e-7], dtype="Float64"),
                      "b": pd.array([True, False, True], dtype="boolean")})
    ws = sheet(write_xlsx(f))
    assert [c.value for c in ws["A"][1:]] == [1, -2, 999_999_999_999_999]
    assert [c.value for c in ws["B"][1:]] == [0.1, -2.5, 1e-7]
    assert [c.value for c in ws["C"][1:]] == [True, False, True]
    assert {c.data_type for c in ws["A"][1:] + ws["B"][1:]} == {"n"} and {c.data_type for c in ws["C"][1:]} == {"b"}


def test_integers_over_15_digits_are_written_as_text():
    f = pd.DataFrame({"i": pd.array([10 ** 15, -(10 ** 15), 2 ** 63 - 1, 5], dtype="Int64")})
    ws = sheet(write_xlsx(f))
    assert [c.value for c in ws["A"][1:]] == ["1000000000000000", "-1000000000000000", str(2 ** 63 - 1), 5]
    assert excel_adjustments(f).integers_as_text == 3


def test_decimals_needing_more_than_15_digits_are_counted_but_kept_as_numbers():
    f = pd.DataFrame({"f": pd.array([0.1, 1 / 3, 2.5, 0.1 + 0.2], dtype="Float64")})
    ws = sheet(write_xlsx(f))
    assert [c.value for c in ws["A"][1:]] == [0.1, 1 / 3, 2.5, 0.1 + 0.2]   # Stored exactly; Excel rounds on open
    assert excel_adjustments(f).decimals_rounded == 2


def test_infinite_values_are_text():
    f = pd.DataFrame({"f": pd.array([np.inf, -np.inf, 1.0], dtype="Float64")})
    assert [r[0] for r in values(write_xlsx(f))[1:]] == ["inf", "-inf", 1.0]
    assert excel_adjustments(f).infinite_as_text == 2


def test_dates_are_excel_dates():
    f = pd.DataFrame({"d": dates(dt.date(2026, 1, 15), dt.date(1900, 3, 1), dt.date(2000, 2, 29),
                                 dt.date(9999, 12, 31))})
    ws = sheet(write_xlsx(f))
    assert [c.value for c in ws["A"][1:]] == [dt.datetime(2026, 1, 15), dt.datetime(1900, 3, 1),
                                               dt.datetime(2000, 2, 29), dt.datetime(9999, 12, 31)]
    assert {c.number_format for c in ws["A"][1:]} == {"yyyy-mm-dd"}
    assert excel_adjustments(f).dates_as_text == 0


def test_dates_before_march_1900_are_text():
    f = pd.DataFrame({"d": dates(dt.date(1899, 12, 31), dt.date(1900, 2, 28), dt.date(1900, 3, 1))})
    assert [r[0] for r in values(write_xlsx(f))[1:]] == ["1899-12-31", "1900-02-28", dt.datetime(1900, 3, 1)]
    assert excel_adjustments(f).dates_as_text == 2


def test_datetimes_and_milliseconds():
    f = pd.DataFrame({"at": stamps("2026-01-15 14:30:00", "2026-01-15 14:30:00.250", "1700-01-01 00:00:00",
                                   "2026-01-15 14:30:00.123456")})
    ws = sheet(write_xlsx(f))
    got = [c.value for c in ws["A"][1:]]
    assert got[0] == dt.datetime(2026, 1, 15, 14, 30) and got[1] == dt.datetime(2026, 1, 15, 14, 30, 0, 250000)
    assert got[2] == "1700-01-01 00:00:00"             # Before 1900: text, even outside pandas' nanosecond range
    assert abs(got[3] - dt.datetime(2026, 1, 15, 14, 30, 0, 123456)) < dt.timedelta(milliseconds=1)
    assert ws["A2"].number_format == "yyyy-mm-dd hh:mm:ss.000"
    adj = excel_adjustments(f)
    assert adj.times_below_ms == 1 and adj.dates_as_text == 1


def test_whole_second_datetimes_use_a_seconds_format():
    ws = sheet(write_xlsx(pd.DataFrame({"at": stamps("2026-01-15 14:30:00")})))
    assert ws["A2"].number_format == "yyyy-mm-dd hh:mm:ss"


def test_utc_datetimes_are_written_in_utc_and_reported():
    f = pd.DataFrame({"at": stamps("2026-01-15 14:30:00").dt.tz_localize("UTC")})
    assert values(write_xlsx(f))[1] == (dt.datetime(2026, 1, 15, 14, 30),)
    assert excel_adjustments(f).utc_columns == ("at",)


def test_other_storage_types_are_text_per_cell():
    f = pd.DataFrame({"o": pd.Series([1, True, 1.0, None, "=x"], dtype=object)})
    assert [r[0] for r in values(write_xlsx(f))[1:]] == ["1", "True", "1.0", None, "=x"]
    assert excel_adjustments(f).text_columns_other == ("o",)


# ---- text and safety --------------------------------------------------------

def test_formula_like_text_is_never_a_formula():
    f = pd.DataFrame({"=SUM(A1)": text("=SUM(A1:A9)", "@cmd", "+1+1", "-2+3", "=HYPERLINK(\"x\")")})
    data = write_xlsx(f)
    assert "<f>" not in sheet_xml(data) and "<f " not in sheet_xml(data)
    ws = sheet(data)
    assert [c.value for c in ws["A"]] == ["=SUM(A1)", "=SUM(A1:A9)", "@cmd", "+1+1", "-2+3", "=HYPERLINK(\"x\")"]
    assert {c.data_type for c in ws["A"]} == {"s"}
    assert excel_adjustments(f).changed_cells == 0     # Nothing is changed to make it safe


def test_unicode_whitespace_and_line_breaks():
    vals = ["naïve café", "🎉 emoji", "日本語", "  lead and trail  ", "two\nlines", "cr\r\nlf", "tab\there", "<&>\"'"]
    assert [r[0] for r in values(write_xlsx(pd.DataFrame({"t": text(*vals)})))[1:]] == vals


def test_control_characters_are_replaced_and_counted():
    f = pd.DataFrame({"t": text("a\x01b", "a\x01b", None, "ok", "\x00\x1f")})
    assert [r[0] for r in values(write_xlsx(f))[1:]] == ["a�b", "a�b", None, "ok", "��"]
    assert excel_adjustments(f).control_characters == 3


def test_rows_with_no_values_keep_their_place():
    f = pd.DataFrame({"a": text("x", None, "y", None), "b": pd.array([1, None, 2, None], dtype="Int64")})
    xml = sheet_xml(write_xlsx(f))
    assert '<row r="3"></row>' in xml and '<row r="5"></row>' in xml
    # Readers stop at the last row that holds a value, so a trailing empty row is not read back.
    assert values(write_xlsx(f))[1:] == [("x", 1), (None, None), ("y", 2)]


def test_escape_like_text_is_kept_literal_for_excel():
    xml = sheet_xml(write_xlsx(pd.DataFrame({"t": text("_x0041_", "a_b", "_x12_")})))
    assert "_x005F_x0041_" in xml and ">a_b<" in xml and ">_x12_<" in xml


def test_long_text_is_cut_to_the_cell_limit():
    long = "a" * (EXCEL_MAX_CELL_CHARS + 10)
    emoji = "🎉" * 20_000                               # 40,000 UTF-16 units
    f = pd.DataFrame({"t": text(long, "a" * EXCEL_MAX_CELL_CHARS, emoji)})
    got = [r[0] for r in values(write_xlsx(f))[1:]]
    assert len(got[0]) == EXCEL_MAX_CELL_CHARS and len(got[1]) == EXCEL_MAX_CELL_CHARS
    assert got[2] == "🎉" * (EXCEL_MAX_CELL_CHARS // 2)   # No surrogate pair is split
    assert excel_adjustments(f).truncated_text == 2


# ---- limits ---------------------------------------------------------------

def test_excel_limits():
    assert limit_error(EXCEL_MAX_ROWS - 1, 10) is None
    assert "rows" in limit_error(EXCEL_MAX_ROWS, 10)            # The header row takes one row
    assert limit_error(10, EXCEL_MAX_COLUMNS) is None
    assert "columns" in limit_error(10, EXCEL_MAX_COLUMNS + 1)
    wide = pd.DataFrame({f"c{i}": text() for i in range(EXCEL_MAX_COLUMNS + 1)})
    assert not excel_adjustments(wide).fits
    with pytest.raises(ExcelLimitError):
        write_xlsx(wide)


def test_refine_limits_fit_in_a_worksheet():
    from refine.config import MAX_COLUMNS, MAX_ROWS
    assert limit_error(MAX_ROWS, 2 * MAX_COLUMNS) is None       # Indicator columns can add up to one per column


@pytest.mark.parametrize("name", ["", "   ", "a" * 32, "a/b", "x[1]", "what?", "'quoted'", "History", "a:b"])
def test_invalid_sheet_names(name):
    with pytest.raises(ValueError):
        check_sheet_name(name)
    with pytest.raises(ValueError):
        write_xlsx(pd.DataFrame({"a": text("x")}), sheet_name=name)


def test_valid_sheet_name_with_xml_characters():
    data = write_xlsx(pd.DataFrame({"a": text("x")}), sheet_name='R&D "Q1"')
    assert openpyxl.load_workbook(io.BytesIO(data)).sheetnames == ['R&D "Q1"']


def test_column_letters():
    assert [column_letter(i) for i in (0, 25, 26, 51, 52, 701, 702, 16383)] == \
           ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA", "XFD"]


def test_wide_table_references():
    f = pd.DataFrame({f"c{i}": text("v") for i in range(300)})
    ws = sheet(write_xlsx(f))
    assert ws.max_column == 300 and ws.cell(row=1, column=300).value == "c299"
    assert re.search(r'<dimension ref="A1:KN2"/>', sheet_xml(write_xlsx(f)))


def test_reads_back_through_refine_loader(sample_bytes):
    ds = load_file("messy_customers.csv", sample_bytes)
    again = load_file("again.xlsx", write_xlsx(ds.data))
    assert again.source.sheet_name == DEFAULT_SHEET_NAME
    assert list(again.data.columns) == list(ds.data.columns) and again.data.shape == ds.data.shape
    pd.testing.assert_frame_equal(again.data.fillna(""), ds.data.fillna(""), check_dtype=False)


def mixed_frame(n: int) -> pd.DataFrame:
    i = np.arange(n)
    return pd.DataFrame({
        "t": pd.array([("=x" if k % 7 == 0 else f"v{k % 50}\x01" if k % 11 == 0 else None if k % 13 == 0
                        else f"row {k}") for k in i], dtype=TEXT_DTYPE),
        "i": pd.array([10 ** 15 + k if k % 5 == 0 else (None if k % 9 == 0 else int(k)) for k in i], dtype="Int64"),
        "f": pd.array([1 / 3 if k % 4 == 0 else (np.inf if k % 17 == 0 else k + 0.5) for k in i], dtype="Float64"),
        "d": dates(*[dt.date(1899, 1, 1) if k % 10 == 0 else dt.date(2026, 1, 1 + k % 28) for k in i]),
        "at": stamps(*[f"2026-01-01 00:00:{k % 60:02d}.{k % 1000:06d}" for k in i]),
    })


def test_writer_counts_match_the_page_check_across_chunks():
    """The page's notes (excel_adjustments) and what the writer does come from the same rules."""
    from refine.xlsx_writer import _Counts, _write
    f = mixed_frame(12_345)                              # Several chunks of rows
    data, counts = _write(f, DEFAULT_SHEET_NAME)
    adj = excel_adjustments(f)
    for key in vars(_Counts()):
        assert getattr(counts, key) == getattr(adj, key), key
    assert adj.integers_as_text and adj.decimals_rounded and adj.dates_as_text and adj.control_characters


def test_rows_stay_in_order_across_chunks():
    f = mixed_frame(12_345)
    ws = sheet(write_xlsx(f))
    assert ws.max_row == 12_346
    got = [c.value for c in ws["B"][1:]]
    expected = [None if pd.isna(x) else (str(int(x)) if int(x) >= 10 ** 15 else int(x)) for x in f["i"]]
    assert got == expected
