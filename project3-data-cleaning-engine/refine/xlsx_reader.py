"""Read one worksheet of an .xlsx workbook into raw records.

Excel stores typed cells (numbers, dates, TRUE/FALSE), so an .xlsx file
cannot be loaded "as text" in the same sense as a CSV. REFINE reads the
stored value of each cell and converts it to text with fixed rules (see
`excel_value_to_text`). Display formatting (currency symbols, thousands
separators, rounding, custom date formats) is not stored with the value
and is therefore not reproduced.

Formulas are not recalculated: the result Excel last saved is used. A
formula cell with no saved result (common in files written by scripts)
is loaded as empty and reported.
"""

from __future__ import annotations

import datetime as dt
import io
import warnings
import zipfile
from typing import Any

from refine.config import MAX_CELLS, MAX_ROWS, MAX_XLSX_UNCOMPRESSED_BYTES
from refine.errors import LoadError
from refine.notes import example_list, plural, quote, verb
from refine.raw_table import RawTable
from refine.source import LoadNote, SheetInfo


class ExcelErrorValue(str):
    """A cell holding an Excel error such as #DIV/0! or #N/A."""


# ------------------------------------------------------------------
# Value conversion
# ------------------------------------------------------------------

def _format_float(value: float) -> str:
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)  # Shortest text that round-trips to the same float


def excel_value_to_text(value: Any) -> tuple[str | None, str]:
    """Convert one stored Excel value to text. Returns (text, kind).

    kind is one of: empty, text, number, boolean, datetime, error.

    * Numbers: integers as digits; other floats in Python's shortest
      round-trip form (0.1 -> "0.1", 1e-07 -> "1e-07").
    * TRUE/FALSE as "TRUE" / "FALSE".
    * Dates as YYYY-MM-DD; date-times as YYYY-MM-DD HH:MM:SS (a date-time
      at exactly midnight is written as a date); times as HH:MM:SS.
      Fractional seconds are kept when present.
    """
    if value is None:
        return None, "empty"
    if isinstance(value, ExcelErrorValue):
        return str(value), "error"
    if isinstance(value, str):
        return value, "text"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), "boolean"
    if isinstance(value, int):
        return str(value), "number"
    if isinstance(value, float):
        return _format_float(value), "number"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0) and value.tzinfo is None:
            return value.date().isoformat(), "datetime"
        spec = "microseconds" if value.microsecond else "seconds"
        return value.isoformat(sep=" ", timespec=spec), "datetime"
    if isinstance(value, dt.date):
        return value.isoformat(), "datetime"
    if isinstance(value, dt.time):
        spec = "microseconds" if value.microsecond else "seconds"
        return value.isoformat(timespec=spec), "datetime"
    if isinstance(value, dt.timedelta):
        return str(value), "datetime"
    return str(value), "text"


# ------------------------------------------------------------------
# Workbook access
# ------------------------------------------------------------------

def check_archive(data: bytes) -> None:
    """Reject files that are not real .xlsx archives, or would expand too far."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            expanded = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        raise LoadError(
            "invalid_xlsx",
            "The file has an .xlsx name but is not a valid Excel workbook (it may be damaged, "
            "or saved in another format). Open it in Excel and save it again as .xlsx.",
        ) from None
    if "xl/workbook.xml" not in names:
        raise LoadError(
            "invalid_xlsx",
            "The file is a zip archive but not an Excel workbook (no workbook part found).",
        )
    if expanded > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise LoadError(
            "xlsx_too_large",
            f"The workbook expands to {expanded / 1024 / 1024:,.0f} MB when opened, above the "
            f"{MAX_XLSX_UNCOMPRESSED_BYTES / 1024 / 1024:,.0f} MB limit. Remove unused sheets or "
            "formatting, or export the sheet you need as CSV.",
        )


def _open(data: bytes, data_only: bool):
    import openpyxl

    try:
        with warnings.catch_warnings():
            # openpyxl warns about workbook features it ignores (data
            # validation, conditional formatting); they do not affect values.
            warnings.simplefilter("ignore")
            return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=data_only)
    except LoadError:
        raise
    except Exception as exc:  # openpyxl raises many exception types for damaged files
        raise LoadError(
            "xlsx_unreadable",
            f"The workbook could not be read ({type(exc).__name__}). It may be damaged or use "
            "features this reader does not support. Open it in Excel and save it again as .xlsx, "
            "or export the sheet as CSV.",
        ) from None


def list_sheets(data: bytes) -> list[SheetInfo]:
    """Worksheets in workbook order (chart sheets are excluded)."""
    check_archive(data)
    wb = _open(data, data_only=True)
    try:
        return [SheetInfo(ws.title, ws.sheet_state != "visible") for ws in wb.worksheets]
    finally:
        wb.close()


def _choose_sheet(sheets: list[SheetInfo], requested: str | None) -> SheetInfo:
    if not sheets:
        raise LoadError("no_worksheets", "The workbook has no worksheets.")
    if requested is not None:
        for s in sheets:
            if s.name == requested:
                return s
        raise LoadError(
            "sheet_not_found",
            f"The workbook has no sheet named {quote(requested)}. "
            f"Sheets: {', '.join(quote(s.name) for s in sheets)}.",
        )
    visible = [s for s in sheets if not s.hidden]
    return (visible or sheets)[0]


def _iter_cells(ws):
    """Yield (sheet_row_number, [cell, ...]) with trailing empty cells removed."""
    ws.reset_dimensions()  # Ignore the stored "used range"; it is often stale
    for row_number, row in enumerate(ws.iter_rows(), start=1):
        cells = list(row)
        while cells and cells[-1].value is None:
            cells.pop()
        yield row_number, cells


def read_xlsx(data: bytes, sheet: str | None, skip_rows: int) -> RawTable:
    check_archive(data)
    sheets = list_sheets(data)
    chosen = _choose_sheet(sheets, sheet)
    notes: list[LoadNote] = []

    records: list[list[Any]] = []
    rows: list[int] = []
    blank_rows: list[int] = []
    row_limit = MAX_ROWS + 1 + skip_rows
    cells_seen = 0

    wb = _open(data, data_only=True)
    try:
        ws = wb[chosen.name]
        for row_number, cells in _iter_cells(ws):
            if not cells:
                blank_rows.append(row_number)
                continue
            values = [
                ExcelErrorValue(c.value) if c.data_type == "e" and c.value is not None else c.value
                for c in cells
            ]
            records.append(values)
            rows.append(row_number)
            # Early stop: non-empty data cells can never exceed the final
            # rows × columns, so passing the limit here is conclusive.
            if len(records) > skip_rows + 1:
                cells_seen += sum(v is not None and v != "" for v in values)
            if cells_seen > MAX_CELLS:
                raise LoadError(
                    "too_many_cells",
                    f"The sheet has more than {MAX_CELLS:,} cells, the current limit. Remove "
                    "rows or columns you do not need, then upload it again.",
                )
            if len(records) > row_limit:
                raise LoadError(
                    "too_many_rows",
                    f"The sheet has more than {MAX_ROWS:,} data rows, the current limit. "
                    "Remove rows you do not need or split the data, then upload it again.",
                )
    finally:
        wb.close()

    # Second, formula-aware pass over the same sheet: count formula cells and
    # find those whose saved result is missing (loaded as empty above).
    formulas, missing_results = _formula_counts(data, chosen.name, dict(zip(rows, records)))

    if len(sheets) > 1:
        others = [s.name for s in sheets if s.name != chosen.name]
        notes.append(LoadNote(
            "other_sheets", "info",
            f"The workbook has {plural(len(sheets), 'sheet')}; loaded {quote(chosen.name)}. "
            f"Other sheets: {', '.join(quote(n) for n in others)}. Choose a different sheet under "
            "parsing options.",
        ))
    if chosen.hidden:
        notes.append(LoadNote(
            "hidden_sheet", "warning",
            f"Sheet {quote(chosen.name)} is hidden in the workbook. Check it is the sheet you meant to load.",
        ))
    if formulas:
        notes.append(LoadNote(
            "formulas_cached", "info",
            f"{plural(formulas, 'cell')} {verb(formulas, 'contains a formula', 'contain formulas')}. The results as last saved in "
            "Excel were loaded; formulas are not recalculated.",
        ))
    if missing_results:
        notes.append(LoadNote(
            "formula_no_result", "warning",
            f"{plural(missing_results, 'formula cell')} {verb(missing_results, 'has', 'have')} no saved "
            "result (the workbook was probably written by a program that does not calculate "
            f"formulas) and {verb(missing_results, 'was', 'were')} loaded as empty. Open and save the file in Excel to store the results.",
        ))

    if blank_rows:
        # Rows after the last record are only formatting; report the rest.
        last = rows[-1] if rows else 0
        inner = [r for r in blank_rows if r < last]
        if inner:
            notes.append(LoadNote(
                "blank_rows_skipped", "info",
                f"Skipped {plural(len(inner), 'completely empty row')} "
                f"({example_list(inner, 'row')}).",
                rows=tuple(inner[:20]),
            ))

    return RawTable(
        records=records,
        record_rows=rows,
        row_unit="row",
        ragged_is_error=False,
        notes=notes,
        meta={"sheet_name": chosen.name, "sheets": tuple(sheets)},
    )


def _has_formulas(data: bytes) -> bool:
    """Cheap check: does any worksheet part contain a formula element?

    Conservative (looks at every sheet), so a False answer is reliable and
    the slower formula-aware pass can be skipped.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                with archive.open(info) as part:
                    tail = b""
                    while chunk := part.read(1 << 20):
                        block = tail + chunk
                        if b"<f>" in block or b"<f " in block or b"<f/>" in block:
                            return True
                        tail = block[-3:]
    return False


def _formula_counts(data: bytes, sheet_name: str, values_by_row: dict[int, list[Any]]) -> tuple[int, int]:
    if not _has_formulas(data):
        return 0, 0
    wb = _open(data, data_only=False)
    formulas = missing = 0
    try:
        ws = wb[sheet_name]
        for row_number, cells in _iter_cells(ws):
            for col, cell in enumerate(cells):
                if cell.data_type != "f":
                    continue
                formulas += 1
                saved = values_by_row.get(row_number, [])
                if col >= len(saved) or saved[col] is None:
                    missing += 1
    finally:
        wb.close()
    return formulas, missing
