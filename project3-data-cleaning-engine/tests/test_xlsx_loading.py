"""XLSX loading: typed values to text, sheets, formulas, errors and structure."""

import datetime as dt
import io
import zipfile

import pytest

from refine.errors import LoadError
from refine.loader import list_sheets
from refine.source import TEXT_DTYPE
from refine.xlsx_reader import excel_value_to_text
from tests.conftest import codes, column, load_xlsx, make_xlsx, patch_xlsx_part

# ------------------------------------------------------------------
# Value conversion rules
# ------------------------------------------------------------------


@pytest.mark.parametrize("value, text, kind", [
    (None, None, "empty"),
    ("00123", "00123", "text"),
    ("  spaced ", "  spaced ", "text"),
    (42, "42", "number"),
    (-7, "-7", "number"),
    (3.0, "3", "number"),
    (2.5, "2.5", "number"),
    (0.1, "0.1", "number"),
    (1e-07, "1e-07", "number"),
    (1e20, "1e+20", "number"),
    (True, "TRUE", "boolean"),
    (False, "FALSE", "boolean"),
    (dt.datetime(2024, 1, 5), "2024-01-05", "datetime"),
    (dt.datetime(2024, 1, 5, 13, 4, 5), "2024-01-05 13:04:05", "datetime"),
    (dt.datetime(2024, 1, 5, 13, 4, 5, 250000), "2024-01-05 13:04:05.250000", "datetime"),
    (dt.date(2024, 2, 3), "2024-02-03", "datetime"),
    (dt.time(9, 30), "09:30:00", "datetime"),
    (dt.timedelta(hours=26), "1 day, 2:00:00", "datetime"),
])
def test_excel_value_to_text(value, text, kind):
    assert excel_value_to_text(value) == (text, kind)


def test_typed_cells_loaded_as_text_and_reported():
    data = make_xlsx({"S": [
        ["id", "amount", "when", "active", "code"],
        [1, 2.5, dt.datetime(2024, 1, 5), True, "007"],
        [2, 1250.0, dt.datetime(2024, 3, 1, 8, 15), False, "008"],
    ]})
    ds = load_xlsx(data)
    assert all(dtype == TEXT_DTYPE for dtype in ds.data.dtypes)
    assert column(ds, "id") == ["1", "2"]
    assert column(ds, "amount") == ["2.5", "1250"]
    assert column(ds, "when") == ["2024-01-05", "2024-03-01 08:15:00"]
    assert column(ds, "active") == ["TRUE", "FALSE"]
    assert column(ds, "code") == ["007", "008"]
    note = ds.note("excel_values_as_text")
    assert note.severity == "info"
    assert "4 numbers" in note.message and "2 date/time values" in note.message
    assert "2 TRUE/FALSE values" in note.message


def test_text_only_sheet_has_no_conversion_note():
    ds = load_xlsx(make_xlsx({"S": [["a", "b"], ["x", "y"]]}))
    assert codes(ds) == []


def test_empty_cells_are_missing():
    ds = load_xlsx(make_xlsx({"S": [["a", "b", "c"], ["x", None, "z"], [None, None, "w"]]}))
    assert column(ds, "a") == ["x", None]
    assert column(ds, "b") == [None, None]


def test_numeric_and_date_headers_become_text():
    ds = load_xlsx(make_xlsx({"S": [["region", 2024, dt.date(2025, 1, 1)], ["EU", 1, 2]]}))
    assert list(ds.data.columns) == ["region", "2024", "2025-01-01"]


# ------------------------------------------------------------------
# Sheets
# ------------------------------------------------------------------


def test_list_sheets_in_order_with_hidden_flag():
    data = make_xlsx({"Visible": [["a"], [1]], "Secret": [["b"], [2]]}, hidden=("Secret",))
    sheets = list_sheets(data)
    assert [(s.name, s.hidden) for s in sheets] == [("Visible", False), ("Secret", True)]


def test_first_visible_sheet_is_default():
    data = make_xlsx({"Hidden": [["h"], [0]], "Main": [["m"], [1]]}, hidden=("Hidden",))
    ds = load_xlsx(data)
    assert ds.source.sheet_name == "Main"
    assert "other_sheets" in codes(ds)


def test_choose_sheet():
    data = make_xlsx({"A": [["a"], [1]], "B": [["b"], [2]]})
    ds = load_xlsx(data, sheet="B")
    assert list(ds.data.columns) == ["b"]
    assert [s.name for s in ds.source.sheets] == ["A", "B"]


def test_choosing_hidden_sheet_warns():
    data = make_xlsx({"A": [["a"], [1]], "H": [["h"], [2]]}, hidden=("H",))
    ds = load_xlsx(data, sheet="H")
    assert ds.note("hidden_sheet").severity == "warning"


def test_unknown_sheet():
    with pytest.raises(LoadError) as err:
        load_xlsx(make_xlsx({"A": [["a"], [1]]}), sheet="Nope")
    assert err.value.code == "sheet_not_found"
    assert "'A'" in err.value.message


def test_single_sheet_has_no_sheet_note():
    assert "other_sheets" not in codes(load_xlsx(make_xlsx({"Only": [["a"], [1]]})))


# ------------------------------------------------------------------
# Formulas and error values
# ------------------------------------------------------------------


def test_formula_without_saved_result_is_missing_and_warned():
    data = make_xlsx({"S": [["a", "double"], [2, "=A2*2"]]})
    ds = load_xlsx(data)
    assert column(ds, "double") == [None]
    assert ds.note("formula_no_result").severity == "warning"
    assert "1 cell contains a formula" in ds.note("formulas_cached").message


def test_formula_with_saved_result_uses_result():
    data = make_xlsx({"S": [["a", "double"], [2, "=A2*2"]]})
    data = patch_xlsx_part(data, "xl/worksheets/sheet1.xml", b"<f>A2*2</f><v />", b"<f>A2*2</f><v>4</v>")
    ds = load_xlsx(data)
    assert column(ds, "double") == ["4"]
    assert "formulas_cached" in codes(ds)
    assert "formula_no_result" not in codes(ds)


def test_excel_error_values_loaded_as_text_and_warned():
    data = make_xlsx({"S": [["a", "b"], [1, "#DIV/0!"], [2, "#N/A"]]})
    ds = load_xlsx(data)
    assert column(ds, "b") == ["#DIV/0!", "#N/A"]
    assert "2 cells contain Excel error values" in ds.note("excel_errors").message


# ------------------------------------------------------------------
# Structure
# ------------------------------------------------------------------


def test_title_rows_skipped_and_row_numbers_reported():
    data = make_xlsx({"S": [["Sales export"], [None], ["id", "v"], [1, "x"], [2, "y"]]})
    ds = load_xlsx(data, skip_rows=1)
    assert list(ds.data.columns) == ["id", "v"]
    assert ds.source_rows.tolist() == [4, 5]
    assert ds.note("rows_skipped").rows == (1,)


def test_table_not_starting_at_a1():
    data = make_xlsx({"S": [[None], [None], [None, None, "id", "v"], [None, None, 1, "x"]]})
    ds = load_xlsx(data)
    # Empty columns to the left of the table are kept (named, reported), not removed.
    assert list(ds.data.columns) == ["column_1", "column_2", "id", "v"]
    assert column(ds, "column_1") == [None]
    assert ds.source_rows.tolist() == [4]
    assert ds.note("empty_unnamed_columns").columns == ("column_1", "column_2")
    assert ds.source.header_labels == (None, None, "id", "v")


def test_empty_rows_inside_table_are_skipped_and_reported():
    data = make_xlsx({"S": [["a"], [1], [None], [2]]})
    ds = load_xlsx(data)
    assert column(ds, "a") == ["1", "2"]
    assert ds.note("blank_rows_skipped").rows == (3,)


def test_formatted_but_empty_cells_are_ignored():
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["a", "b"])
    ws.append([1, 2])
    ws["H40"].font = Font(bold=True)  # Styled, no value: extends the "used range"
    buffer = io.BytesIO()
    wb.save(buffer)
    ds = load_xlsx(buffer.getvalue())
    assert ds.data.shape == (1, 2)
    assert codes(ds) == ["excel_values_as_text"]


def test_duplicate_and_blank_headers():
    ds = load_xlsx(make_xlsx({"S": [["id", None, "id"], [1, "x", 2]]}))
    assert list(ds.data.columns) == ["id", "column_2", "id_2"]
    assert {"duplicate_header", "blank_header"} <= set(codes(ds))


def test_empty_sheet():
    with pytest.raises(LoadError) as err:
        load_xlsx(make_xlsx({"S": []}))
    assert err.value.code == "empty_file"


def test_header_only_sheet():
    with pytest.raises(LoadError) as err:
        load_xlsx(make_xlsx({"S": [["a", "b"]]}))
    assert err.value.code == "header_only"


# ------------------------------------------------------------------
# Damaged and unsafe archives
# ------------------------------------------------------------------


def test_damaged_zip():
    data = make_xlsx({"S": [["a"], [1]]})
    with pytest.raises(LoadError) as err:
        load_xlsx(data[: len(data) // 2])
    assert err.value.code in {"invalid_xlsx", "xlsx_unreadable"}


def test_zip_that_is_not_a_workbook():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(LoadError) as err:
        load_xlsx(buffer.getvalue())
    assert err.value.code == "invalid_xlsx"


def test_corrupt_workbook_part_gives_clean_error():
    data = make_xlsx({"S": [["a"], [1]]})
    broken = patch_xlsx_part(data, "xl/workbook.xml", b"<sheets>", b"<sheets><<<")
    with pytest.raises(LoadError) as err:
        load_xlsx(broken)
    assert err.value.code == "xlsx_unreadable"


def test_uncompressed_size_limit(monkeypatch):
    import refine.xlsx_reader as reader
    monkeypatch.setattr(reader, "MAX_XLSX_UNCOMPRESSED_BYTES", 1000)
    with pytest.raises(LoadError) as err:
        load_xlsx(make_xlsx({"S": [["a"], [1]]}))
    assert err.value.code == "xlsx_too_large"
