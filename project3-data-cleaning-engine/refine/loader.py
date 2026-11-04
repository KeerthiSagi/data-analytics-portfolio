"""Load an uploaded CSV or XLSX file into a text table, with full reporting.

Public entry points:

    load_file(file_name, data, options=None) -> LoadedDataset
    list_sheets(data) -> list[SheetInfo]
    detect_format(file_name, data) -> "csv" | "xlsx"

What "preserved" means here
---------------------------
Two different things are kept, and the UI says which is which:

1. The original file: the uploaded bytes are kept unchanged in memory
   (`LoadedDataset.raw_bytes`) with their SHA-256 checksum.
2. The parsed cell values: every cell is stored as text, exactly as the
   parser read it. Nothing is trimmed, no type is inferred, and markers
   such as "NA" or "null" stay as text. Parsing still *interprets* the
   file (encoding, delimiter, quoting, Excel's typed values), so the table
   is not a byte-for-byte copy of the file. Every interpretation that is
   not obvious, and every structural adjustment (for example renaming a
   duplicate column), is recorded as a LoadNote.

"No value" is pd.NA: an empty Excel cell, or a CSV row that ends before
the header does. An empty CSV field ("a,,b") is the empty string "",
because that is what the file contains.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePath
from typing import Any, Callable

import numpy as np
import pandas as pd

from refine.config import (
    MAX_CELLS,
    MAX_COLUMNS,
    MAX_RETAINED_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    SUPPORTED_EXTENSIONS,
)
from refine.csv_reader import read_csv
from refine.errors import LoadError
from refine.notes import example_list, plural, quote, verb
from refine.raw_table import RawTable
from refine.source import (
    TEXT_DTYPE,
    FileFormat,
    LoadedDataset,
    LoadNote,
    LoadOptions,
    SheetInfo,
    SourceInfo,
)
from refine.xlsx_reader import excel_value_to_text, list_sheets as _xlsx_sheets, read_xlsx

ZIP_SIGNATURE = b"PK\x03\x04"
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # Legacy .xls, or an encrypted workbook

PRESERVATION_SUMMARY = (
    "The uploaded file is kept unchanged in memory (verified by its SHA-256 checksum). "
    "The table shows the parsed cell values as text: nothing is trimmed, converted or filled in. "
    "Parsing still interprets the file (encoding, delimiter, quotes, Excel's stored values), so "
    "the table is not a byte-for-byte copy of the file; the load notes list every adjustment."
)


# ------------------------------------------------------------------
# Format checks
# ------------------------------------------------------------------

def detect_format(file_name: str, data: bytes) -> FileFormat:
    """Validate the extension and the file's content signature."""
    suffix = PurePath(file_name or "").suffix.lower()
    supported = " or ".join(SUPPORTED_EXTENSIONS)
    if suffix == ".xls":
        raise LoadError(
            "legacy_xls",
            "Legacy Excel .xls files are not supported. Open the file in Excel and save it as "
            ".xlsx (Excel Workbook) or .csv, then upload it again.",
        )
    if suffix not in SUPPORTED_EXTENSIONS:
        shown = f"'{suffix}'" if suffix else "no extension"
        raise LoadError(
            "unsupported_type",
            f"Files with {shown} are not supported. Upload a {supported} file.",
        )
    fmt: FileFormat = "csv" if suffix == ".csv" else "xlsx"
    if fmt == "csv" and data.startswith(ZIP_SIGNATURE):
        raise LoadError(
            "content_mismatch",
            "This file has a .csv name but its content is a zip archive, which usually means an "
            "Excel workbook. Rename it to .xlsx, or export it from Excel as CSV.",
        )
    if data.startswith(OLE_SIGNATURE):
        raise LoadError(
            "legacy_or_encrypted",
            "This file is a legacy Excel (.xls) workbook or a password-protected workbook. Neither "
            "is supported: remove the password and save it as .xlsx or .csv.",
        )
    if fmt == "xlsx" and not data.startswith(ZIP_SIGNATURE):
        raise LoadError(
            "invalid_xlsx",
            "The file has an .xlsx name but is not an Excel workbook. If it is a CSV, rename it to .csv.",
        )
    return fmt


def check_size(data: bytes) -> None:
    if len(data) == 0:
        raise LoadError("empty_file", "The file is empty (0 bytes).")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LoadError(
            "file_too_large",
            f"The file is {len(data) / 1024 / 1024:,.1f} MB, above the {MAX_UPLOAD_MB} MB limit.",
        )


def list_sheets(data: bytes) -> list[SheetInfo]:
    """Worksheets of an .xlsx workbook, in order."""
    check_size(data)
    if not data.startswith(ZIP_SIGNATURE):
        raise LoadError("invalid_xlsx", "The file is not an .xlsx workbook.")
    return _xlsx_sheets(data)


# ------------------------------------------------------------------
# Header and shape
# ------------------------------------------------------------------

def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _header_text(value: Any) -> str | None:
    text, _ = excel_value_to_text(value)
    return None if text is None or text.strip() == "" else text


def _unique_names(labels: list[str | None], positions: list[int]) -> tuple[list[str], list[LoadNote]]:
    """Name every column, left to right, without changing any non-blank,
    non-duplicate header. Blank headers become column_<position>; repeats
    become <name>_2, <name>_3, ... skipping names already in use."""
    notes: list[LoadNote] = []
    taken = {label for label in labels if label is not None}
    names: list[str] = []
    seen: dict[str, int] = {}
    blanks: list[tuple[int, str]] = []
    renamed: list[tuple[int, str, str]] = []

    def fresh(base: str, start: int) -> str:
        k = start
        while f"{base}_{k}" in taken:
            k += 1
        return f"{base}_{k}"

    for label, pos in zip(labels, positions):
        if label is None:
            name = f"column_{pos}" if f"column_{pos}" not in taken else fresh(f"column_{pos}", 2)
            blanks.append((pos, name))
        elif label in seen:
            seen[label] += 1
            name = fresh(label, seen[label])
            renamed.append((pos, label, name))
        else:
            seen[label] = 1
            name = label
        taken.add(name)
        names.append(name)

    if blanks:
        detail = ", ".join(f"position {p} → {quote(n)}" for p, n in blanks)
        notes.append(LoadNote(
            "blank_header", "warning",
            f"{plural(len(blanks), 'column')} had an empty header and {verb(len(blanks), 'was', 'were')} "
            f"given a name: {detail}.",
            columns=tuple(n for _, n in blanks),
        ))
    if renamed:
        detail = ", ".join(f"{quote(old)} at position {p} → {quote(new)}" for p, old, new in renamed)
        notes.append(LoadNote(
            "duplicate_header", "warning",
            f"{plural(len(renamed), 'column')} repeated an earlier header and "
            f"{verb(len(renamed), 'was', 'were')} renamed so every column has a unique name: {detail}. The first column with each name keeps it.",
            columns=tuple(n for _, _, n in renamed),
        ))
    return names, notes


def build_dataset(raw: RawTable, skip_rows: int) -> tuple[pd.DataFrame, list[int], list[str | None], list[LoadNote]]:
    """Apply skip_rows and the header to raw records; return the text frame."""
    notes: list[LoadNote] = []
    unit = raw.row_unit
    records, rows = raw.records, raw.record_rows

    if skip_rows < 0:
        raise LoadError("bad_option", "Rows to skip cannot be negative.")
    if not records:
        raise LoadError("empty_file", "The file has no rows with content.")
    if skip_rows >= len(records):
        raise LoadError(
            "skip_too_many",
            f"Skipping {plural(skip_rows, 'row')} leaves no header row: the file has only "
            f"{plural(len(records), 'non-empty row')}.",
        )
    if skip_rows:
        notes.append(LoadNote(
            "rows_skipped", "info",
            f"Ignored {plural(skip_rows, 'row')} above the header, as requested "
            f"({example_list(rows[:skip_rows], unit)}).",
            rows=tuple(rows[:skip_rows]),
        ))

    header = records[skip_rows]
    data_records = records[skip_rows + 1:]
    data_rows = rows[skip_rows + 1:]
    if not data_records:
        raise LoadError(
            "header_only",
            f"The file has a header row ({unit} {rows[skip_rows]:,}) but no data rows below it.",
        )

    header_len = len(header)
    width = max(header_len, max(len(r) for r in data_records))

    # CSV: values beyond the header would have no column (and usually mean
    # an unquoted delimiter shifted the row), so refuse rather than guess.
    if raw.ragged_is_error and width > header_len:
        over = [n for r, n in zip(data_records, data_rows)
                if any(not _is_empty(v) for v in r[header_len:])]
        if over:
            raise LoadError(
                "ragged_rows",
                f"{plural(len(over), 'row')} {verb(len(over), 'has', 'have')} more values than the header has columns "
                f"({header_len}), for example {example_list(over, unit)}. This usually means a value "
                "contains the delimiter without quotes, or the delimiter is wrong. Check those "
                "lines or choose a different delimiter.",
            )
        extra = [n for r, n in zip(data_records, data_rows) if len(r) > header_len]
        notes.append(LoadNote(
            "extra_empty_fields", "info",
            f"{plural(len(extra), 'row')} {verb(len(extra), 'ends', 'end')} with extra empty fields beyond the header "
            f"({example_list(extra, unit)}); these empty fields were ignored.",
            rows=tuple(extra[:20]),
        ))
        width = header_len

    # Every column is kept, including columns with no header and no values
    # (typically a trailing delimiter, or empty Excel columns to the left
    # of the table). They are named and reported, never removed.
    labels_all = [_header_text(header[i]) if i < header_len else None for i in range(width)]
    keep = list(range(width))
    empty_unnamed = [
        i for i in range(width)
        if labels_all[i] is None and all(i >= len(r) or _is_empty(r[i]) for r in data_records)
    ]
    if len(empty_unnamed) == width:
        raise LoadError("no_values", "The file has rows, but every cell is empty.")

    if len(keep) > MAX_COLUMNS:
        raise LoadError(
            "too_many_columns",
            f"The file has {len(keep):,} columns, above the {MAX_COLUMNS:,} column limit.",
        )
    n_cells = len(keep) * len(data_records)
    if n_cells > MAX_CELLS:
        raise LoadError(
            "too_many_cells",
            f"The file has {n_cells:,} cells ({len(data_records):,} rows × {len(keep):,} columns), "
            f"above the {MAX_CELLS:,} cell limit.",
        )

    # CSV rows that stop before the last kept column: the missing cells
    # are recorded as "no value" (pd.NA), not as empty text.
    last_needed = keep[-1] + 1
    if raw.ragged_is_error:
        short = [n for r, n in zip(data_records, data_rows) if len(r) < last_needed]
        if short:
            notes.append(LoadNote(
                "short_rows_padded", "warning",
                f"{plural(len(short), 'row')} {verb(len(short), 'has', 'have')} fewer values than the header "
                f"({example_list(short, unit)}). The missing cells at the end of "
                f"{verb(len(short), 'that row', 'those rows')} were loaded as empty (no value).",
                rows=tuple(short[:20]),
            ))

    labels = [labels_all[i] for i in keep]
    names, name_notes = _unique_names(labels, [i + 1 for i in keep])
    notes.extend(name_notes)
    if empty_unnamed:
        shown = ", ".join(quote(names[i]) for i in empty_unnamed)
        notes.append(LoadNote(
            "empty_unnamed_columns", "info",
            f"{plural(len(empty_unnamed), 'column')} {verb(len(empty_unnamed), 'has', 'have')} no header "
            f"and no values in any row ({example_list([i + 1 for i in empty_unnamed], 'column')}). "
            f"{verb(len(empty_unnamed), 'It was', 'They were')} kept as {shown}, not removed, and "
            "the Profile & Quality page reports empty columns.",
            columns=tuple(names[i] for i in empty_unnamed),
        ))

    to_text: Callable[[Any], Any]
    kinds: dict[str, int] = {}
    if raw.ragged_is_error:      # CSV cells are already text
        def to_text(v):
            return v
    else:
        def to_text(v):
            text, kind = excel_value_to_text(v)
            kinds[kind] = kinds.get(kind, 0) + 1
            return text

    columns = {}
    for name, i in zip(names, keep):
        values = [to_text(r[i]) if i < len(r) else None for r in data_records]
        columns[name] = pd.array(values, dtype=TEXT_DTYPE)
    frame = pd.DataFrame(columns)

    if not raw.ragged_is_error:
        notes.extend(_excel_conversion_notes(kinds))

    # The header label as written in the file, for display (None if blank).
    raw_labels = [labels_all[i] for i in keep]
    return frame, data_rows, raw_labels, notes


def _excel_conversion_notes(kinds: dict[str, int]) -> list[LoadNote]:
    notes = []
    parts = []
    if kinds.get("number"):
        parts.append(plural(kinds["number"], "number"))
    if kinds.get("datetime"):
        parts.append(plural(kinds["datetime"], "date/time value"))
    if kinds.get("boolean"):
        parts.append(plural(kinds["boolean"], "TRUE/FALSE value"))
    if parts:
        notes.append(LoadNote(
            "excel_values_as_text", "info",
            "Excel stores some cells as typed values. They were written as text using fixed rules: "
            f"{', '.join(parts)}. Numbers keep their full stored precision; dates use YYYY-MM-DD "
            "(with HH:MM:SS when there is a time). Excel display formatting such as currency "
            "symbols, thousands separators and rounding is not stored with the value, so it is "
            "not reproduced.",
        ))
    if kinds.get("error"):
        notes.append(LoadNote(
            "excel_errors", "warning",
            f"{plural(kinds['error'], 'cell')} {verb(kinds['error'], 'contains an Excel error value', 'contain Excel error values')} "
            "(such as #DIV/0! or #N/A), loaded as that text.",
        ))
    return notes


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def load_file(file_name: str, data: bytes, options: LoadOptions | None = None) -> LoadedDataset:
    """Parse an uploaded file. Raises LoadError with a user-facing message.

    `data` is never modified. The same bytes and options always produce
    the same table and the same notes.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise LoadError("bad_input", "Expected the file's content as bytes.")
    data = bytes(data)
    options = options or LoadOptions()
    check_size(data)
    fmt = detect_format(file_name, data)

    if fmt == "csv":
        if options.sheet is not None:
            raise LoadError("bad_option", "A sheet can only be chosen for .xlsx files.")
        raw = read_csv(data, options.encoding, options.delimiter, options.skip_rows)
    else:
        if options.encoding is not None or options.delimiter is not None:
            raise LoadError("bad_option", "Encoding and delimiter only apply to .csv files.")
        raw = read_xlsx(data, options.sheet, options.skip_rows)

    frame, data_rows, raw_labels, shape_notes = build_dataset(raw, options.skip_rows)
    meta = raw.meta
    source = SourceInfo(
        file_name=PurePath(file_name).name,
        file_format=fmt,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        options=options,
        n_rows=len(frame),
        n_columns=frame.shape[1],
        columns=tuple(frame.columns),
        header_labels=tuple(raw_labels),
        encoding=meta.get("encoding"),
        encoding_detected=meta.get("encoding_detected", False),
        delimiter=meta.get("delimiter"),
        delimiter_detected=meta.get("delimiter_detected", False),
        sheet_name=meta.get("sheet_name"),
        sheets=meta.get("sheets", ()),
    )
    return LoadedDataset(
        data=frame,
        source=source,
        notes=tuple(raw.notes + shape_notes),
        source_rows=np.asarray(data_rows, dtype=np.int64),
        raw_bytes=data if len(data) <= MAX_RETAINED_BYTES else None,
    )


def verify_raw_bytes(dataset: LoadedDataset) -> bool:
    """True when the retained bytes still match the checksum taken at load."""
    if dataset.raw_bytes is None:
        return False
    return hashlib.sha256(dataset.raw_bytes).hexdigest() == dataset.source.sha256
