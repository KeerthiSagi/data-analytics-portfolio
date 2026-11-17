"""Write a table to an .xlsx workbook, in memory. Never imports Streamlit.

Entry points:

    excel_adjustments(frame) -> ExcelAdjustments   # what the workbook will hold differently
    write_xlsx(frame) -> bytes                       # the workbook

Why not openpyxl or pandas.to_excel: both write each worksheet to a
temporary file on disk before zipping it, and REFINE never writes
uploaded data to disk. The workbook here is a plain zip of a few XML
parts built in memory with the standard library. openpyxl is still what
the tests use to read the result back.

Cell rules
----------
* Text is written as inline text. A cell is never written as a formula,
  so "=SUM(A1:A9)" or "@cmd" stays text and is not run when opened.
  Control characters other than tab, line feed and carriage return
  cannot be stored in an .xlsx file. They are replaced with U+FFFD (the
  replacement character) and counted. Excel reads text such as "_x0041_"
  as an escape for another character, so the underscore of such text is
  escaped and Excel shows it as written. (openpyxl, and pandas.read_excel,
  do not decode these escapes and show "_x005F_x0041_" instead.)
* Whole numbers (Int64) are numbers. Excel keeps 15 significant digits,
  so integers of 16 digits or more are written as text to keep every digit.
* Decimals (Float64) are numbers, written in the shortest form that reads
  back exactly. Excel itself keeps 15 significant digits, so values that
  need more are rounded by Excel; they are counted. Infinite values are
  written as the text "inf" / "-inf".
* True / false values are Excel booleans.
* Dates and date-times are Excel dates with a yyyy-mm-dd format. Excel
  has no time zones: UTC columns are written as UTC times. Excel shows
  times to the millisecond. Dates before 1900-03-01, which Excel's date
  system cannot hold correctly, are written as ISO text.
* Other storage types are written as text.
* Text over 32,767 characters (Excel's cell limit) is cut to that length.
* Missing cells are empty cells.

The stored table is never changed: values are converted only while the
XML is written.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from refine.conversion import canonical_text, column_kind

EXCEL_MAX_ROWS = 1_048_576           # Including the header row
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_CELL_CHARS = 32_767        # UTF-16 code units, as Excel counts them
EXCEL_EXACT_DIGITS = 15
EXCEL_MAX_SHEET_NAME = 31
DEFAULT_SHEET_NAME = "Cleaned Data"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_INTEGER_TEXT_FROM = 10 ** EXCEL_EXACT_DIGITS       # 16 digits or more
_SERIAL_EPOCH_DAYS = 25_569                          # 1970-01-01 as an Excel serial (1900 system)
_FIRST_SAFE_SERIAL = 61                              # 1900-03-01; earlier serials hit Excel's 1900 leap-year bug
_MICROS_PER_DAY = 86_400_000_000
_ROW_CHUNK = 5_000
_CHUNK_CELLS = 50_000
_INVALID_SHEET_CHARS = set("[]:*?/\\")
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)                    # Fixed, so the same table gives the same bytes

# Characters XML 1.0 cannot hold, and an underscore that starts an _xHHHH_ sequence.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_ILLEGAL_RE2 = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x{FFFE}\x{FFFF}]"     # Arrow text is valid UTF-8: no surrogates
_ESCAPE_LIKE = re.compile("_(?=x[0-9A-Fa-f]{4}_)")
REPLACEMENT_CHAR = "\ufffd"

# Cell styles (indexes into cellXfs in styles.xml).
_STYLE_HEADER, _STYLE_DATE, _STYLE_DATETIME, _STYLE_DATETIME_MS = 1, 2, 3, 4


class ExcelLimitError(ValueError):
    """The table does not fit in one Excel worksheet."""


@dataclass(frozen=True)
class ExcelAdjustments:
    """Where the workbook holds a value differently from the stored table. Counts are cells."""

    rows: int
    columns: int
    truncated_text: int = 0              # Text cut to 32,767 characters
    control_characters: int = 0          # Text with control characters replaced by U+FFFD
    integers_as_text: int = 0            # 16+ digit whole numbers written as text
    decimals_rounded: int = 0            # Need more than 15 significant digits; Excel rounds them
    infinite_as_text: int = 0            # inf / -inf written as text
    dates_as_text: int = 0               # Before 1900-03-01, written as ISO text
    times_below_ms: int = 0              # Date-times with microseconds Excel does not show
    utc_columns: tuple[str, ...] = ()    # Written as UTC times without a zone
    text_columns_other: tuple[str, ...] = ()   # Storage types Excel has no type for, written as text
    limit_error: str | None = None       # Set when the table does not fit in a worksheet

    @property
    def fits(self) -> bool:
        return self.limit_error is None

    @property
    def changed_cells(self) -> int:
        return (self.truncated_text + self.control_characters + self.integers_as_text + self.infinite_as_text
                + self.dates_as_text)


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def check_sheet_name(name: str) -> str:
    """The name, if Excel accepts it as a worksheet name; ValueError otherwise."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("A worksheet name cannot be empty.")
    if len(name) > EXCEL_MAX_SHEET_NAME:
        raise ValueError(f"A worksheet name can have at most {EXCEL_MAX_SHEET_NAME} characters.")
    if _INVALID_SHEET_CHARS & set(name):
        raise ValueError("A worksheet name cannot contain [ ] : * ? / or \\.")
    if name.startswith("'") or name.endswith("'"):
        raise ValueError("A worksheet name cannot start or end with an apostrophe.")
    if name.lower() == "history":
        raise ValueError("Excel reserves the worksheet name 'History'.")
    return name


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def limit_error(rows: int, columns: int) -> str | None:
    if rows + 1 > EXCEL_MAX_ROWS:
        return (f"The table has {rows:,} rows. An Excel worksheet holds at most {EXCEL_MAX_ROWS - 1:,} rows "
                "below the header row.")
    if columns > EXCEL_MAX_COLUMNS:
        return f"The table has {columns:,} columns. An Excel worksheet holds at most {EXCEL_MAX_COLUMNS:,}."
    return None


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate(text: str) -> tuple[str, bool]:
    """Text cut to Excel's cell limit, never splitting a surrogate pair."""
    if len(text) <= EXCEL_MAX_CELL_CHARS // 2 or _utf16_len(text) <= EXCEL_MAX_CELL_CHARS:
        return text, False
    units, end = 0, 0
    for end, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > EXCEL_MAX_CELL_CHARS:
            break
    return text[:end], True


def _cell_text(text: str) -> tuple[str, bool, bool]:
    """(text Excel can hold, was it truncated, were control characters replaced)."""
    text, cut = _truncate(text)
    replaced = _ILLEGAL.search(text) is not None
    if replaced:
        text = _ILLEGAL.sub(REPLACEMENT_CHAR, text)
    return text, cut, replaced


def _escape_text(text: str) -> str:
    """XML for text that _cell_text has made storable."""
    escaped = _ESCAPE_LIKE.sub("_x005F_", text)
    # XML parsers turn a literal carriage return into a line feed; a character reference survives.
    return _xml_escape(escaped).replace("\r", "&#13;")


def _text_tail(text: str, style: int = 0) -> str:
    """Everything after `<c r="A1"` for a text cell."""
    space = ' xml:space="preserve"' if text and (text[0].isspace() or text[-1].isspace()) else ""
    s = f' s="{style}"' if style else ""
    return f'{s} t="inlineStr"><is><t{space}>{_escape_text(text)}</t></is></c>'


def _number_tail(literal: str, style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    return f"{s}><v>{literal}</v></c>"


def _needs_more_digits(x: float) -> bool:
    return float(f"{x:.{EXCEL_EXACT_DIGITS}g}") != x


# ------------------------------------------------------------------
# Per-column cell encoding
# ------------------------------------------------------------------

@dataclass
class _Counts:
    truncated_text: int = 0
    control_characters: int = 0
    integers_as_text: int = 0
    decimals_rounded: int = 0
    infinite_as_text: int = 0
    dates_as_text: int = 0
    times_below_ms: int = 0


def _text_tails(texts: list[str], weights: np.ndarray, counts: _Counts) -> list[str]:
    tails = []
    for k, value in enumerate(texts):
        text, cut, replaced = _cell_text(value)
        counts.truncated_text += int(weights[k]) if cut else 0
        counts.control_characters += int(weights[k]) if replaced else 0
        tails.append(_text_tail(text))
    return tails


def _factorize(series: pd.Series) -> tuple[np.ndarray, Any, np.ndarray]:
    """(codes with -1 for missing, distinct values, how many rows hold each)."""
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    codes = np.asarray(codes, dtype=np.int64)
    weights = np.bincount(codes[codes >= 0], minlength=len(uniques)) if len(uniques) else np.zeros(0, np.int64)
    return codes, uniques, weights


def _datetime_micros(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(microseconds since 1970 in UTC or as stored, missing mask) for a date-time column."""
    if isinstance(series.dtype, pd.DatetimeTZDtype):
        series = series.dt.tz_convert("UTC").dt.tz_localize(None)
    mask = series.isna().to_numpy(dtype=bool)
    micros = series.to_numpy(dtype="datetime64[us]").view(np.int64)
    return np.where(mask, 0, micros), mask


def _date_days(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(days since 1970, missing mask) for an Arrow date column."""
    arr = pa.array(series.array, from_pandas=True)
    if isinstance(arr, pa.ChunkedArray):
        arr = pa.concat_arrays(arr.chunks) if arr.num_chunks else pa.array([], pa.date32())
    mask = np.asarray(arr.is_null().to_numpy(zero_copy_only=False), dtype=bool)
    days = pc.fill_null(pc.cast(pc.cast(arr, pa.date32()), pa.int32()), 0).to_numpy(zero_copy_only=False)
    return np.asarray(days, dtype=np.int64), mask


def _datetime_style(series: pd.Series) -> int:
    """Seconds format, or milliseconds when any value of the column has a fraction of a second."""
    micros, mask = _datetime_micros(series)
    return _STYLE_DATETIME_MS if bool(np.any((micros % 1_000_000 != 0) & ~mask)) else _STYLE_DATETIME


def _column_cells(series: pd.Series, counts: _Counts, style: int = _STYLE_DATETIME) -> tuple[np.ndarray, list[str]]:
    """(codes, tails): codes[i] indexes tails for row i, -1 for an empty cell.

    Work is per distinct value, so cost follows distinct values, not rows.
    `style` is the date-time format, chosen once for the whole column.
    """
    kind = column_kind(series.dtype)
    if kind == "other":
        # Each cell as text first: factorizing mixed objects would merge 1, 1.0 and True.
        values = series.to_numpy(dtype=object)
        series = pd.Series([None if _is_missing(v) else str(v) for v in values], dtype=object)
        kind = "text"
    codes, uniques, weights = _factorize(series)
    tails: list[str] = []
    if kind == "text":
        tails = _text_tails([str(v) for v in uniques], weights, counts)
    elif kind == "integer":
        for k, v in enumerate(uniques):
            v = int(v)
            if abs(v) >= _INTEGER_TEXT_FROM:
                counts.integers_as_text += int(weights[k])
                tails.append(_text_tail(str(v)))
            else:
                tails.append(_number_tail(str(v)))
    elif kind == "decimal":
        for k, v in enumerate(uniques):
            x = float(v)
            if math.isnan(x):                    # NaN that is not NA: treated as missing
                tails.append("")
            elif math.isinf(x):
                counts.infinite_as_text += int(weights[k])
                tails.append(_text_tail("inf" if x > 0 else "-inf"))
            else:
                if _needs_more_digits(x):
                    counts.decimals_rounded += int(weights[k])
                tails.append(_number_tail(repr(x)))
    elif kind == "boolean":
        tails = [f' t="b"><v>{1 if bool(v) else 0}</v></c>' for v in uniques]
    elif kind == "date":
        for k, v in enumerate(uniques):
            day = pd.Timestamp(v).date() if not isinstance(v, dt.date) else v
            serial = (day - dt.date(1970, 1, 1)).days + _SERIAL_EPOCH_DAYS
            if serial < _FIRST_SAFE_SERIAL:
                counts.dates_as_text += int(weights[k])
                tails.append(_text_tail(day.isoformat()))
            else:
                tails.append(_number_tail(str(serial), _STYLE_DATE))
    elif kind == "datetime":
        stamps = pd.DatetimeIndex(uniques)
        if stamps.tz is not None:
            stamps = stamps.tz_convert("UTC").tz_localize(None)
        micros = stamps.as_unit("us").asi8 if len(stamps) else np.zeros(0, np.int64)
        for k, m in enumerate(micros.tolist()):
            if m % 1000:
                counts.times_below_ms += int(weights[k])
            serial = m / _MICROS_PER_DAY + _SERIAL_EPOCH_DAYS
            if serial < _FIRST_SAFE_SERIAL:
                counts.dates_as_text += int(weights[k])
                tails.append(_text_tail(canonical_text(uniques[k], "datetime")))
            else:
                tails.append(_number_tail(repr(serial), style))
    return codes, tails


def _is_missing(value: Any) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):          # Array-like cells are values
        return False


# ------------------------------------------------------------------
# Adjustments (read-only, before writing)
# ------------------------------------------------------------------

def _arrow_text(series: pd.Series) -> pa.Array:
    arr = pa.array(series.array, from_pandas=True)
    if isinstance(arr, pa.ChunkedArray):
        arr = pa.concat_arrays(arr.chunks) if arr.num_chunks else pa.array([], pa.string())
    return arr


def _count_column(series: pd.Series, counts: _Counts) -> None:
    """Add one column's adjustments to `counts`, with the writer's rules but without building any XML."""
    kind = column_kind(series.dtype)
    if kind == "text":
        try:
            arr = _arrow_text(series)
            lengths = np.asarray(pc.fill_null(pc.utf8_length(arr), 0).to_numpy(zero_copy_only=False))
            illegal = pc.fill_null(pc.match_substring_regex(arr, _ILLEGAL_RE2), False)
            flagged = (lengths > EXCEL_MAX_CELL_CHARS // 2) | np.asarray(
                illegal.to_numpy(zero_copy_only=False), dtype=bool)
            rows = np.flatnonzero(flagged)
        except (pa.ArrowException, TypeError, ValueError):
            rows = np.arange(len(series))
        if len(rows):
            _column_cells(series.iloc[rows], counts)
    elif kind == "other":
        _column_cells(series, counts)
    elif kind == "integer":
        mask = series.isna().to_numpy(dtype=bool)
        unsigned = getattr(series.dtype, "kind", "") == "u"
        values = series.to_numpy(dtype="uint64" if unsigned else "int64", na_value=0)
        big = values >= _INTEGER_TEXT_FROM if unsigned else (values >= _INTEGER_TEXT_FROM) | (values <= -_INTEGER_TEXT_FROM)
        counts.integers_as_text += int((big & ~mask).sum())
    elif kind == "decimal":
        x = series.to_numpy(dtype="float64", na_value=np.nan)
        counts.infinite_as_text += int(np.isinf(x).sum())
        finite = x[np.isfinite(x)]
        if len(finite):
            values, n = np.unique(finite, return_counts=True)
            counts.decimals_rounded += int(sum(int(c) for v, c in zip(values.tolist(), n) if _needs_more_digits(v)))
    elif kind == "date":
        days, mask = _date_days(series)
        counts.dates_as_text += int(((days + _SERIAL_EPOCH_DAYS < _FIRST_SAFE_SERIAL) & ~mask).sum())
    elif kind == "datetime":
        micros, mask = _datetime_micros(series)
        early = micros < (_FIRST_SAFE_SERIAL - _SERIAL_EPOCH_DAYS) * _MICROS_PER_DAY
        counts.dates_as_text += int((early & ~mask).sum())
        counts.times_below_ms += int(((micros % 1000 != 0) & ~mask).sum())


def _adjustments(rows: int, columns: int, counts: _Counts, frame: pd.DataFrame) -> ExcelAdjustments:
    kinds = [column_kind(frame.iloc[:, j].dtype) for j in range(columns)]
    names = [str(c) for c in frame.columns]
    return ExcelAdjustments(
        rows, columns, truncated_text=counts.truncated_text, control_characters=counts.control_characters,
        integers_as_text=counts.integers_as_text, decimals_rounded=counts.decimals_rounded,
        infinite_as_text=counts.infinite_as_text, dates_as_text=counts.dates_as_text,
        times_below_ms=counts.times_below_ms,
        utc_columns=tuple(n for j, n in enumerate(names)
                          if kinds[j] == "datetime" and isinstance(frame.iloc[:, j].dtype, pd.DatetimeTZDtype)),
        text_columns_other=tuple(n for n, k in zip(names, kinds) if k == "other"),
    )


def excel_adjustments(frame: pd.DataFrame) -> ExcelAdjustments:
    """What write_xlsx will write differently from the stored values. Reads only; builds no XML."""
    rows, columns = frame.shape
    error = limit_error(rows, columns)
    if error:
        return ExcelAdjustments(rows, columns, limit_error=error)
    counts = _Counts()
    for j in range(columns):
        _count_column(frame.iloc[:, j], counts)
    return _adjustments(rows, columns, counts, frame)


# ------------------------------------------------------------------
# Workbook parts
# ------------------------------------------------------------------

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_CONTENT_TYPES = (
    _XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    _XML_HEAD + f'<Relationships xmlns="{_PKG_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_WORKBOOK_RELS = (
    _XML_HEAD + f'<Relationships xmlns="{_PKG_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'styles" Target="styles.xml"/></Relationships>'
)
_STYLES = (
    _XML_HEAD + f'<styleSheet xmlns="{_MAIN_NS}">'
    '<numFmts count="3"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
    '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm:ss"/>'
    '<numFmt numFmtId="166" formatCode="yyyy-mm-dd hh:mm:ss.000"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="5">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _workbook_xml(sheet_name: str) -> str:
    return (_XML_HEAD + f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">'
            f'<sheets><sheet name="{_xml_escape(sheet_name, {chr(34): "&quot;"})}" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>")


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _sheet_head(rows: int, columns: int) -> str:
    ref = f"A1:{column_letter(columns - 1)}{rows + 1}" if columns else "A1"
    pane = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>') if columns else ""
    return (_XML_HEAD + f'<worksheet xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><dimension ref="{ref}"/>{pane}'
            '<sheetFormatPr defaultRowHeight="15"/><sheetData>')


def _chunk_rows(columns: int) -> int:
    """Rows per chunk, so one chunk holds about _CHUNK_CELLS cells whatever the table's shape."""
    return max(1, min(_ROW_CHUNK, _CHUNK_CELLS // max(columns, 1)))


def _write(frame: pd.DataFrame, sheet_name: str) -> tuple[bytes, _Counts]:
    rows, columns = frame.shape
    counts = _Counts()
    letters = [column_letter(j) for j in range(columns)]
    series = [frame.iloc[:, j] for j in range(columns)]
    styles = [_datetime_style(s) if column_kind(s.dtype) == "datetime" else 0 for s in series]
    step = _chunk_rows(columns)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_info("[Content_Types].xml"), _CONTENT_TYPES)
        zf.writestr(_info("_rels/.rels"), _ROOT_RELS)
        zf.writestr(_info("xl/workbook.xml"), _workbook_xml(sheet_name))
        zf.writestr(_info("xl/_rels/workbook.xml.rels"), _WORKBOOK_RELS)
        zf.writestr(_info("xl/styles.xml"), _STYLES)
        with zf.open(_info("xl/worksheets/sheet1.xml"), "w") as sheet:
            sheet.write(_sheet_head(rows, columns).encode("utf-8"))
            if columns:
                header = "".join(f'<c r="{letter}1"{_text_tail(_cell_text(str(name))[0], _STYLE_HEADER)}'
                                 for letter, name in zip(letters, frame.columns))
                sheet.write(f'<row r="1">{header}</row>'.encode("utf-8"))
            # Cells are encoded one chunk of rows at a time, so memory stays bounded for tall and wide tables.
            for start in range(0, rows if columns else 0, step):
                stop = min(rows, start + step)
                chunk = []
                for s, style in zip(series, styles):
                    codes, tails = _column_cells(s.iloc[start:stop], counts, style or _STYLE_DATETIME)
                    tails.append("")                  # Code -1 (missing) picks this empty tail
                    chunk.append([tails[c] for c in codes.tolist()])
                parts = []
                for offset, row_tails in enumerate(zip(*chunk)):
                    rn = start + offset + 2
                    cells = "".join(f'<c r="{letter}{rn}"{tail}'
                                    for letter, tail in zip(letters, row_tails) if tail)
                    parts.append(f'<row r="{rn}">{cells}</row>')
                sheet.write("".join(parts).encode("utf-8"))
            sheet.write(b"</sheetData></worksheet>")
    return out.getvalue(), counts


def write_xlsx(frame: pd.DataFrame, sheet_name: str = DEFAULT_SHEET_NAME) -> bytes:
    """The table as an .xlsx workbook with one worksheet: a header row, then every row in order.

    The pandas index is not written. Raises ExcelLimitError when the table
    does not fit in a worksheet. The frame is only read.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("write_xlsx expects a pandas DataFrame.")
    check_sheet_name(sheet_name)
    error = limit_error(*frame.shape)
    if error:
        raise ExcelLimitError(error)
    return _write(frame, sheet_name)[0]


__all__ = [
    "REPLACEMENT_CHAR", "DEFAULT_SHEET_NAME", "EXCEL_EXACT_DIGITS", "EXCEL_MAX_CELL_CHARS", "EXCEL_MAX_COLUMNS", "EXCEL_MAX_ROWS",
    "ExcelAdjustments", "ExcelLimitError", "XLSX_MIME", "check_sheet_name", "column_letter", "excel_adjustments",
    "limit_error", "write_xlsx",
]
