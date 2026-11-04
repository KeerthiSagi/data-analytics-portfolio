"""Data models describing a loaded file: options, metadata, notes, result.

These are plain dataclasses with no Streamlit dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

Severity = Literal["info", "warning"]
FileFormat = Literal["csv", "xlsx"]

# Every loaded column uses this dtype: text with pd.NA for "no value".
# Arrow-backed storage keeps memory low and behaves the same on pandas 2
# and pandas 3.
TEXT_DTYPE = pd.StringDtype("pyarrow")


@dataclass(frozen=True)
class LoadOptions:
    """User-controllable parsing choices. None means "detect"."""

    encoding: str | None = None        # CSV only
    delimiter: str | None = None       # CSV only
    sheet: str | None = None           # XLSX only; None = first visible sheet
    skip_rows: int = 0                 # Non-empty rows above the header to ignore


@dataclass(frozen=True)
class LoadNote:
    """One thing the loader did or noticed while reading the file.

    `info` notes describe an interpretation or adjustment that does not
    change any cell value. `warning` notes describe something the user
    should check, because values may not be what the file intended.
    """

    code: str
    severity: Severity
    message: str
    columns: tuple[str, ...] = ()
    rows: tuple[int, ...] = ()          # Source line (CSV) or sheet row (XLSX) examples


@dataclass(frozen=True)
class SheetInfo:
    name: str
    hidden: bool


@dataclass(frozen=True)
class SourceInfo:
    """Where the table came from and how it was read."""

    file_name: str
    file_format: FileFormat
    size_bytes: int
    sha256: str
    options: LoadOptions
    n_rows: int
    n_columns: int
    columns: tuple[str, ...]
    # The header cell text as found in the file, one per loaded column
    # (None where the header cell was empty).
    header_labels: tuple[str | None, ...]
    # CSV
    encoding: str | None = None
    encoding_detected: bool = False
    delimiter: str | None = None
    delimiter_detected: bool = False
    # XLSX
    sheet_name: str | None = None
    sheets: tuple[SheetInfo, ...] = ()


@dataclass(frozen=True, eq=False)
class LoadedDataset:
    """The result of loading one file.

    * `data` holds the parsed cell values as text (dtype TEXT_DTYPE), in
      file order, with a default RangeIndex.
    * `source_rows[i]` is the line (CSV) or sheet row (XLSX) where data
      row i starts, so later phases can point users back to the file.
    * `raw_bytes` is the exact uploaded file, kept for re-parsing with
      different options and for checksum verification.
    """

    data: pd.DataFrame
    source: SourceInfo
    notes: tuple[LoadNote, ...]
    source_rows: np.ndarray = field(repr=False)
    raw_bytes: bytes | None = field(default=None, repr=False)

    @property
    def raw_bytes_retained(self) -> bool:
        return self.raw_bytes is not None

    @property
    def warnings(self) -> tuple[LoadNote, ...]:
        return tuple(n for n in self.notes if n.severity == "warning")

    def note(self, code: str) -> LoadNote | None:
        """First note with this code, or None."""
        return next((n for n in self.notes if n.code == code), None)
