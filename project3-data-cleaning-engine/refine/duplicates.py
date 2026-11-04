"""Duplicate detection. Read-only: matching never changes a stored value.

Rows are compared on a set of columns (all columns, or chosen key
columns). Two optional normalizations apply to the comparison only:

* ignore_case: "ANN@X.COM" matches "ann@x.com" (compared lower-cased).
* normalize_whitespace: surrounding whitespace is ignored and runs of
  whitespace inside a value count as one space, so " New  York" matches
  "New York".

A cell with no value (pd.NA) matches another cell with no value. It does
not match empty text "", and normalization does not change that, so
standardize missing values first if they should match.

Vocabulary (used on the Clean page too):

* Duplicate group: a set of two or more rows that match each other.
* Duplicate rows (extra copies): rows in a group beyond the one that is
  kept. Removing duplicates removes exactly these rows.
* Rows in duplicate groups: every row in a group, including the one kept.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Keep = Literal["first", "last"]
_SPACE_RUN = re.compile(r"\s+")


def normalize_text(value: str, *, ignore_case: bool, normalize_whitespace: bool) -> str:
    if normalize_whitespace:
        value = _SPACE_RUN.sub(" ", value).strip()
    if ignore_case:
        value = value.lower()
    return value


def _column_codes(series: pd.Series, ignore_case: bool, normalize_whitespace: bool) -> np.ndarray:
    """An integer per row; equal integers mean the cells match. No value is -1."""
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    codes = np.asarray(codes, dtype=np.int64)
    if not (ignore_case or normalize_whitespace) or len(uniques) == 0:
        return codes
    values = list(uniques)
    keys = [normalize_text(v, ignore_case=ignore_case, normalize_whitespace=normalize_whitespace)
            if isinstance(v, str) else v for v in values]
    # Map each distinct value to the code of its normalized form.
    first: dict[object, int] = {}
    remap = np.fromiter((first.setdefault(k, len(first)) for k in keys), dtype=np.int64, count=len(keys))
    out = np.full(len(codes), -1, dtype=np.int64)
    present = codes >= 0
    out[present] = remap[codes[present]]
    return out


def group_ids(frame: pd.DataFrame, columns: Sequence[str], *, ignore_case: bool = False,
              normalize_whitespace: bool = False) -> np.ndarray:
    """A group number per row (0, 1, 2, ... in order of first appearance)."""
    n = len(frame)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if not columns:
        return np.zeros(n, dtype=np.int64)
    codes = pd.DataFrame({i: _column_codes(frame[c], ignore_case, normalize_whitespace)
                          for i, c in enumerate(columns)})
    return codes.groupby(list(codes.columns), sort=False).ngroup().to_numpy(dtype=np.int64)


def removal_mask(groups: np.ndarray, keep: Keep) -> np.ndarray:
    """True for rows that would be removed: every row of a group except the first (or last)."""
    s = pd.Series(groups)
    return s.duplicated(keep=keep).to_numpy(dtype=bool)


@dataclass(frozen=True)
class DuplicateGroup:
    number: int                    # 1-based, in order of first appearance
    positions: tuple[int, ...]     # 0-based table positions, in table order


@dataclass(frozen=True)
class DuplicateReport:
    rows: int
    columns: tuple[str, ...]
    all_columns: bool
    ignore_case: bool
    normalize_whitespace: bool
    groups: int                     # Duplicate groups (2+ matching rows)
    duplicate_rows: int             # Extra copies: rows a removal would delete
    rows_in_groups: int             # Every row that belongs to a duplicate group
    examples: tuple[DuplicateGroup, ...]

    @property
    def duplicate_pct(self) -> float:
        return 100.0 * self.duplicate_rows / self.rows if self.rows else 0.0

    @property
    def rows_in_groups_pct(self) -> float:
        return 100.0 * self.rows_in_groups / self.rows if self.rows else 0.0

    @property
    def rows_after(self) -> int:
        return self.rows - self.duplicate_rows


def find_duplicates(frame: pd.DataFrame, columns: Sequence[str] | None = None, *, ignore_case: bool = False,
                    normalize_whitespace: bool = False, example_groups: int = 5) -> DuplicateReport:
    """Count duplicate groups and rows. The frame is only read."""
    all_columns = columns is None
    cols = tuple(frame.columns) if columns is None else tuple(columns)
    groups = group_ids(frame, cols, ignore_case=ignore_case, normalize_whitespace=normalize_whitespace)
    sizes = np.bincount(groups) if len(groups) else np.zeros(0, dtype=np.int64)
    repeated = np.flatnonzero(sizes > 1)
    examples = []
    if len(repeated):
        wanted = repeated[:example_groups]          # Group numbers follow first appearance
        in_example = np.isin(groups, wanted)
        positions = np.flatnonzero(in_example)
        for rank, g in enumerate(wanted, start=1):
            examples.append(DuplicateGroup(rank, tuple(int(p) for p in positions[groups[positions] == g])))
    return DuplicateReport(
        rows=len(frame), columns=cols, all_columns=all_columns, ignore_case=ignore_case,
        normalize_whitespace=normalize_whitespace, groups=len(repeated),
        duplicate_rows=int((sizes[repeated] - 1).sum()), rows_in_groups=int(sizes[repeated].sum()),
        examples=tuple(examples),
    )


__all__ = ["DuplicateGroup", "DuplicateReport", "Keep", "find_duplicates", "group_ids", "normalize_text",
           "removal_mask"]
