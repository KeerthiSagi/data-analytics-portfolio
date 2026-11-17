"""Downloads of the current cleaning state. Read-only; never imports Streamlit.

    csv_export(frame, escape_formulas=False)   -> ExportFile   (the table as CSV)
    xlsx_export(frame)                         -> ExportFile   (the table as .xlsx, see xlsx_writer.py)
    history_export(session, source)            -> ExportFile   (the audit log and active steps, JSON)
    recipe_export(session)                     -> ExportFile   (session.recipe(), unchanged, JSON)
    inspect_export(frame)                      -> ExportCheck  (what each format writes differently)

The quality and comparison reports are in refine/reports.py.

Nothing here changes a table or a session. Functions read the frame they
are given (CleaningSession.data is already a copy) and build new bytes.
Nothing is cached and nothing is written to disk.

CSV rules
---------
* UTF-8 without a byte order mark, comma-separated, CRLF line endings
  (RFC 4180), a header row, no index column. Fields are quoted only when
  they contain a comma, a quote or a line break.
* Every value is written as REFINE writes it as text (the same text
  "Convert data type → Text" gives): text unchanged, whole numbers as
  digits, decimals in their shortest exact form ("76.0", "0.1"), true /
  false, ISO dates "2026-01-15" and date-times "2026-01-15 14:30:00",
  with "+00:00" for UTC columns. So a CSV keeps every value exactly but
  not the column types.
* Missing cells and empty text are both written as an empty field; CSV
  cannot tell them apart.
* Formula-like text: spreadsheet apps run a cell that starts with "=",
  "+", "-", "@", a tab or a carriage return as a formula. By default the
  CSV holds the values exactly, and the page says how many cells look
  like formulas. With escape_formulas=True each such text cell (and
  header) is written with a leading apostrophe, which spreadsheet apps
  show as text; that changes those values, and the result says how many.
  Plain numbers such as "-12.5" or "+3", and a lone "-" or "+", are not
  formula-like. The Excel
  download needs no escaping: it never writes a cell as a formula.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from refine.config import ENGINE_NAME, ENGINE_VERSION
from refine.conversion import canonical_text, column_kind
from refine.missing import is_plain_number
from refine.xlsx_writer import XLSX_MIME, ExcelAdjustments, excel_adjustments, write_xlsx

if TYPE_CHECKING:
    from refine.session import CleaningSession
    from refine.source import SourceInfo

CSV_MIME = "text/csv"
JSON_MIME = "application/json"
HISTORY_FORMAT = "refine-history"
HISTORY_VERSION = 1
FORMULA_PREFIX = "'"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# The plain-number rule of refine/missing.py, in RE2 syntax, to skip numbers before the exact check.
_PLAIN_NUMBER_RE2 = r"^\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$"
CSV_ROW_CHUNK = 20_000
CSV_CHUNK_CELLS = 200_000
FILE_STEM_LIMIT = 60
DEFAULT_STEM = "dataset"


# ------------------------------------------------------------------
# Results
# ------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class ExportFile:
    """Bytes ready to download, and what they hold."""

    data: bytes = field(repr=False)
    file_name: str
    mime: str
    rows: int | None = None
    columns: int | None = None
    escaped_cells: int = 0               # CSV: formula-like cells written with a leading apostrophe
    adjustments: ExcelAdjustments | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class ExportCheck:
    """What the dataset downloads would write differently from the stored values. Read-only."""

    rows: int
    columns: int
    formula_like_cells: int              # Text cells (and header names) a spreadsheet would read as formulas
    formula_like_columns: tuple[str, ...]
    excel: ExcelAdjustments
    kinds: dict[str, int]                # Columns by kind (text, integer, decimal, ...)


# ------------------------------------------------------------------
# File names
# ------------------------------------------------------------------

def file_stem(name: str | None) -> str:
    """A safe file-name stem from an uploaded file's name: ASCII letters, digits, '-', '_' only.

    The upload's name is never used as a path: directories, the extension
    and anything else are dropped.
    """
    if not name:
        return DEFAULT_STEM
    base = re.split(r"[\\/]", str(name))[-1]
    base = base.rsplit(".", 1)[0] if "." in base.strip(".") else base
    ascii_text = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_text).strip("_-")[:FILE_STEM_LIMIT].strip("_-")
    return stem or DEFAULT_STEM


def export_file_name(source_name: str | None, part: str, extension: str) -> str:
    """'messy_customers_cleaned.csv'. `part` and `extension` are fixed strings from REFINE."""
    return f"{file_stem(source_name)}_{part}.{extension}"


# ------------------------------------------------------------------
# Text view of a column
# ------------------------------------------------------------------

def _is_missing(value: Any) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def text_values(series: pd.Series) -> list[str | None]:
    """Each cell as REFINE writes it as text; None for a cell with no value.

    Typed values use canonical_text (refine/conversion.py), worked out
    once per distinct value. Empty text stays "".
    """
    kind = column_kind(series.dtype)
    if kind == "other":
        return [None if _is_missing(v) else str(v) for v in series.to_numpy(dtype=object)]
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    if kind == "text":
        texts = [str(v) for v in uniques]
    elif kind == "decimal":
        texts = [None if math.isnan(float(v)) else
                 ("inf" if v > 0 else "-inf") if math.isinf(float(v)) else canonical_text(v, kind) for v in uniques]
    else:
        texts = [canonical_text(v, kind) for v in uniques]
    texts.append(None)                       # Code -1: missing
    return [texts[c] for c in np.asarray(codes, dtype=np.int64).tolist()]


def is_formula_like(text: str) -> bool:
    """True for text a spreadsheet app would run as a formula.

    Starts with "=", "@", a tab or a carriage return; or with "+" or "-"
    followed by more text that is not a plain number ("-12.5" is a number,
    and a lone "-" or "+" cannot form a formula).
    """
    if not text or text[0] not in FORMULA_START:
        return False
    if text[0] in "+-":
        return len(text) > 1 and not is_plain_number(text)
    return not is_plain_number(text)


def _formula_like_count(series: pd.Series) -> int:
    kind = column_kind(series.dtype)
    if kind == "other":
        return sum(1 for v in text_values(series) if v is not None and is_formula_like(v))
    if kind != "text":
        return 0                             # Typed values are numbers, dates or true/false: never formulas
    try:                                     # Only values starting with a formula character need a closer look
        arr = pa.array(series.array, from_pandas=True)
        starts = pc.fill_null(pc.match_substring_regex(arr, r"^[=+\-@\t\r]"), False)
        number = pc.fill_null(pc.match_substring_regex(arr, _PLAIN_NUMBER_RE2), False)
        keep = pc.and_(starts, pc.invert(number))
        candidates = series[np.asarray(keep.to_numpy(zero_copy_only=False), dtype=bool)]
    except (pa.ArrowException, TypeError, ValueError):
        candidates = series
    if not len(candidates):
        return 0
    counts = candidates.value_counts(dropna=True)
    return int(sum(int(n) for value, n in counts.items() if is_formula_like(str(value))))


# ------------------------------------------------------------------
# Dataset exports
# ------------------------------------------------------------------

def inspect_export(frame: pd.DataFrame) -> ExportCheck:
    """Counts behind the Export page's notes. Reads only."""
    rows, columns = frame.shape
    formula_cells, formula_columns = 0, []
    kinds: dict[str, int] = {}
    for j in range(columns):
        series = frame.iloc[:, j]
        kind = column_kind(series.dtype)
        kinds[kind] = kinds.get(kind, 0) + 1
        n = _formula_like_count(series) + (1 if is_formula_like(str(frame.columns[j])) else 0)
        if n:
            formula_cells += n
            formula_columns.append(str(frame.columns[j]))
    return ExportCheck(rows, columns, formula_cells, tuple(formula_columns), excel_adjustments(frame), kinds)


def csv_bytes(frame: pd.DataFrame, *, escape_formulas: bool = False) -> tuple[bytes, int]:
    """(UTF-8 CSV, number of cells escaped). A table without columns gives an empty file."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("csv_bytes expects a pandas DataFrame.")
    rows, columns = frame.shape
    if columns == 0:
        return b"", 0
    escaped = 0

    def guard(text: str | None) -> str | None:
        nonlocal escaped
        if escape_formulas and text is not None and is_formula_like(text):
            escaped += 1
            return FORMULA_PREFIX + text
        return text

    header = [guard(str(name)) for name in frame.columns]
    series = [frame.iloc[:, j] for j in range(columns)]
    textual = [column_kind(s.dtype) in ("text", "other") for s in series]
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(out, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    # Rows are converted one chunk at a time, so memory stays bounded for tall and wide tables.
    step = max(1, min(CSV_ROW_CHUNK, CSV_CHUNK_CELLS // columns))
    for start in range(0, rows, step):
        cols = []
        for s, is_text in zip(series, textual):
            values = text_values(s.iloc[start:start + step])
            if escape_formulas and is_text:
                values = [guard(v) for v in values]
            cols.append(values)
        writer.writerows(zip(*cols))          # None is written as an empty field, the same as empty text
    out.flush()
    data = buffer.getvalue()
    out.detach()
    return data, escaped


def csv_export(frame: pd.DataFrame, *, source_name: str | None = None, cleaned: bool = True,
               escape_formulas: bool = False) -> ExportFile:
    data, escaped = csv_bytes(frame, escape_formulas=escape_formulas)
    part = "cleaned" if cleaned else "original"
    return ExportFile(data, export_file_name(source_name, part, "csv"), CSV_MIME, frame.shape[0], frame.shape[1],
                      escaped_cells=escaped)


def xlsx_export(frame: pd.DataFrame, *, source_name: str | None = None, cleaned: bool = True) -> ExportFile:
    """Raises ExcelLimitError when the table does not fit in a worksheet."""
    data = write_xlsx(frame)
    part = "cleaned" if cleaned else "original"
    return ExportFile(data, export_file_name(source_name, part, "xlsx"), XLSX_MIME, frame.shape[0], frame.shape[1],
                      adjustments=excel_adjustments(frame))


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------

def json_ready(value: Any) -> Any:
    """A deep copy that json.dumps(allow_nan=False) accepts.

    Mappings get string keys, tuples become lists, numpy scalars become
    Python numbers, dates become ISO text. NaN, infinity, pd.NA and NaT
    become null. Any other type is an error, so nothing is written as
    an unreadable object.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, Mapping):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, np.ndarray):
        return [json_ready(v) for v in value.tolist()]
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    raise TypeError(f"Cannot write a value of type {type(value).__name__} to JSON.")


def json_bytes(document: Any) -> bytes:
    """Indented UTF-8 JSON with a final newline. Never emits NaN or Infinity."""
    return (json.dumps(json_ready(document), indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_summary(source: SourceInfo | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {"file_name": source.file_name, "file_format": source.file_format, "sha256": source.sha256,
            "rows": source.n_rows, "columns": source.n_columns}


def state_summary(session: CleaningSession) -> dict[str, Any]:
    """The state an export was made from (no data)."""
    recipe = session.recipe()
    return {
        "steps_applied": len(session.steps),
        "original": {"rows": recipe["source"]["rows"], "columns": len(recipe["source"]["columns"]),
                     "fingerprint": session.original_fingerprint},
        "current": {"rows": recipe["result"]["rows"], "columns": len(recipe["result"]["columns"]),
                    "fingerprint": session.fingerprint},
        "fingerprint_version": recipe["fingerprint_version"],
    }


def document_header(kind: str, version_key: str, version: int, *, exported_at: str | None = None) -> dict[str, Any]:
    return {"format": kind, version_key: version, "engine": ENGINE_NAME, "engine_version": ENGINE_VERSION,
            "exported_at": exported_at or _utc_now()}


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------

def _event_status(entry, active: set[int], undone: dict[int, int]) -> str | None:
    if entry.event != "apply":
        return None
    if entry.sequence in active:
        return "active"
    if entry.sequence in undone:
        return "undone"
    return "discarded by reset"


def history_document(session: CleaningSession, *, source: SourceInfo | None = None,
                     exported_at: str | None = None) -> dict[str, Any]:
    """The session's audit log and active steps, from its own records.

    Holds what each step was asked to do (operation, settings, columns),
    what it did (counts, rows and columns before and after), its warnings
    and when it happened. Frozen resolved values and fingerprints are in
    the recipe; table contents, cached states and the uploaded file are
    never included.
    """
    log = session.audit_log
    steps = session.steps
    active = {s.audit_sequence for s in steps}
    undone = {e.reverts_sequence: e.sequence for e in log if e.event == "undo" and e.reverts_sequence is not None}
    audit = {e.sequence: e for e in log}
    active_steps = []
    for number, step in enumerate(steps, start=1):
        entry = audit[step.audit_sequence]
        active_steps.append({
            "step": number, "operation": step.plan.operation, "label": step.plan.label,
            "summary": step.plan.summary, "columns": list(step.plan.affected_columns),
            "params": step.plan.step.params, "counts": dict(step.counts), "warnings": list(step.plan.warnings),
            "rows_before": entry.rows_before, "rows_after": entry.rows_after,
            "columns_before": entry.columns_before, "columns_after": entry.columns_after,
            "applied_at": entry.timestamp, "audit_sequence": entry.sequence,
        })
    events = []
    for e in log:
        events.append({
            "sequence": e.sequence, "timestamp": e.timestamp, "event": e.event,
            "status": _event_status(e, active, undone),
            "undone_by": undone.get(e.sequence) if e.event == "apply" else None,
            "step_number": e.step_number, "reverts_sequence": e.reverts_sequence,
            "operation": e.operation, "label": e.label, "summary": e.summary,
            "columns": list(e.affected_columns), "params": e.params, "counts": dict(e.counts),
            "warnings": list(e.warnings), "rows_before": e.rows_before, "rows_after": e.rows_after,
            "columns_before": e.columns_before, "columns_after": e.columns_after,
        })
    return {
        **document_header(HISTORY_FORMAT, "history_version", HISTORY_VERSION, exported_at=exported_at),
        "source": _source_summary(source),
        "state": state_summary(session),
        "active_steps": active_steps,
        "events": events,
        "notes": [
            "active_steps are the steps that make up the current data, in order.",
            "events is the full audit log, including undone steps and resets. Times are UTC.",
            "Frozen resolved values and fingerprints for each step are in the recipe.",
        ],
    }


def history_export(session: CleaningSession, *, source: SourceInfo | None = None,
                   exported_at: str | None = None) -> ExportFile:
    doc = history_document(session, source=source, exported_at=exported_at)
    name = export_file_name(source.file_name if source else None, "history", "json")
    return ExportFile(json_bytes(doc), name, JSON_MIME)


# ------------------------------------------------------------------
# Recipe
# ------------------------------------------------------------------

def recipe_bytes(session: CleaningSession) -> bytes:
    """session.recipe() as indented JSON, unchanged: the same text the History page shows.

    No field is added, removed or recomputed, so replay_recipe() on the
    parsed file reproduces the current data exactly (it checks the source
    fingerprint and every step's fingerprints).
    """
    text = session.recipe_json(indent=2)
    return (text + "\n").encode("utf-8")


def recipe_export(session: CleaningSession, *, source: SourceInfo | None = None) -> ExportFile:
    name = export_file_name(source.file_name if source else None, "recipe", "json")
    return ExportFile(recipe_bytes(session), name, JSON_MIME)


__all__ = [
    "CSV_MIME", "ExportCheck", "ExportFile", "FORMULA_PREFIX", "HISTORY_FORMAT", "HISTORY_VERSION", "JSON_MIME",
    "XLSX_MIME", "csv_bytes", "csv_export", "document_header", "export_file_name", "file_stem", "history_document",
    "history_export", "inspect_export", "is_formula_like", "json_bytes", "json_ready", "recipe_bytes",
    "recipe_export", "state_summary", "text_values", "xlsx_export",
]
