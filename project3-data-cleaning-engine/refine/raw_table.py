"""The intermediate result both readers produce before header handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from refine.source import LoadNote


@dataclass
class RawTable:
    """Non-blank records from the file, before the header is applied.

    * `records` are lists of cell values. CSV cells are always str; XLSX
      cells are the Python values openpyxl returns (str, int, float, bool,
      datetime, ...) or None for an empty cell.
    * `record_rows[i]` is the line (CSV) or sheet row (XLSX) where
      records[i] starts.
    * `ragged_is_error`: in CSV a row with more fields than the header
      usually means an unquoted delimiter, so values would shift columns.
      In XLSX rows simply stop at their last non-empty cell.
    """

    records: list[list[Any]]
    record_rows: list[int]
    row_unit: str                       # "line" or "row", used in messages
    ragged_is_error: bool
    notes: list[LoadNote] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
