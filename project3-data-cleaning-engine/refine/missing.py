"""Missing values: one definition, read-only scans, and plain-number parsing.

REFINE's definition of a missing cell is the same everywhere (profile,
quality checks and cleaning operations):

* Missing: the cell has no value (pd.NA) or holds empty text "".
* Whitespace-only text and markers such as "NA" or "null" are *not*
  missing. They become missing only when the user selects them in
  "Standardize missing values" and confirms the step.

Everything in this module only reads. Matching works on a column's
distinct values, so its cost grows with the number of distinct values,
not the number of rows.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from refine.profile import NULL_TOKENS

# Offered on the Clean page. None is selected until the user picks it.
PRESET_TOKENS = ("NA", "N/A", "null", "None")

# Same rule as the profile: an optional sign, digits with an optional
# decimal point, an optional exponent; surrounding whitespace allowed.
# "$1,234", "1,234", "12%" and "(5)" are not plain numbers.
_PLAIN_NUMBER = re.compile(r"\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*")


# ------------------------------------------------------------------
# Missing cells
# ------------------------------------------------------------------

def missing_mask(series: pd.Series) -> np.ndarray:
    """True where the cell is missing: pd.NA / None / NaN, or empty text."""
    if not isinstance(series, pd.Series):
        series = pd.Series(series, copy=False)
    mask = series.isna().to_numpy(dtype=bool)
    if isinstance(series.dtype, pd.StringDtype) or series.dtype == object:
        if not mask.all():
            empty = (series == "")
            mask = mask | empty.fillna(False).to_numpy(dtype=bool)
    return mask


def missing_count(series: pd.Series) -> int:
    return int(missing_mask(series).sum())


@dataclass(frozen=True)
class ColumnMissing:
    name: str
    position: int          # 1-based
    missing: int           # pd.NA or empty text
    no_value: int          # pd.NA only
    empty_text: int        # "" only
    rows: int

    @property
    def pct(self) -> float:
        return 100.0 * self.missing / self.rows if self.rows else 0.0


def missing_summary(frame: pd.DataFrame) -> tuple[ColumnMissing, ...]:
    """Missing-cell counts for every column, in table order."""
    out = []
    for i in range(frame.shape[1]):
        s = frame.iloc[:, i]
        na = s.isna().to_numpy(dtype=bool)
        total = missing_mask(s)
        out.append(ColumnMissing(str(frame.columns[i]), i + 1, int(total.sum()), int(na.sum()),
                                 int(total.sum() - na.sum()), len(frame)))
    return tuple(out)


def missing_pct(count: int, rows: int) -> float:
    return 100.0 * count / rows if rows else 0.0


# ------------------------------------------------------------------
# Missing-value markers
# ------------------------------------------------------------------

EMPTY_TEXT = "empty text"
WHITESPACE = "whitespace-only text"


def _norm(text: str, case_sensitive: bool) -> str:
    text = text.strip()
    return text if case_sensitive else text.lower()


def is_whitespace_only(value: str) -> bool:
    return value != "" and value.strip() == ""


def marker_matcher(tokens: Sequence[str], case_sensitive: bool, empty_text: bool, whitespace_only: bool):
    """A function telling which marker (by its display label) a text value matches, or None.

    Tokens are compared after removing surrounding whitespace, so " NA "
    matches "NA". With case_sensitive=False, "na" and "Na" also match.
    """
    lookup: dict[str, str] = {}
    for token in tokens:
        lookup.setdefault(_norm(token, case_sensitive), token)

    def match(value: str) -> str | None:
        if value == "":
            return EMPTY_TEXT if empty_text else None
        if value.strip() == "":
            return WHITESPACE if whitespace_only else None
        return lookup.get(_norm(value, case_sensitive))

    return match


def _distinct(series: pd.Series) -> pd.Series:
    """Counts of each distinct non-null value, in order of first appearance."""
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    if len(uniques) == 0:
        return pd.Series([], dtype="int64")
    counts = np.bincount(codes[codes >= 0], minlength=len(uniques))
    return pd.Series(counts, index=pd.Index(list(uniques), dtype=object))


def marker_matches(series: pd.Series, matcher) -> dict[str, dict[str, int]]:
    """{marker label: {exact value found: count}} for one text column."""
    found: dict[str, dict[str, int]] = {}
    for value, count in _distinct(series).items():
        if not isinstance(value, str):
            continue
        label = matcher(value)
        if label is not None:
            found.setdefault(label, {})[value] = int(count)
    return found


@dataclass(frozen=True)
class MarkerCount:
    column: str
    marker: str            # The token as configured, or EMPTY_TEXT / WHITESPACE
    value: str             # The exact text found in the column
    count: int


def scan_markers(frame: pd.DataFrame, columns: Iterable[str], tokens: Sequence[str], *,
                 case_sensitive: bool, empty_text: bool, whitespace_only: bool) -> tuple[MarkerCount, ...]:
    """Every cell the configured markers would match, grouped by exact text. Changes nothing."""
    matcher = marker_matcher(tokens, case_sensitive, empty_text, whitespace_only)
    out = []
    for name in columns:
        for marker, values in marker_matches(frame[name], matcher).items():
            for value, count in values.items():
                out.append(MarkerCount(name, marker, value, count))
    return tuple(out)


def suggest_markers(frame: pd.DataFrame, columns: Iterable[str]) -> tuple[MarkerCount, ...]:
    """Values that often stand for "missing", found in the columns, with exact counts.

    Uses the profile's null-like list (compared trimmed, ignoring case),
    empty text and whitespace-only text. These are suggestions to review:
    nothing is selected or changed.
    """
    known = set(NULL_TOKENS)
    out = []
    for name in columns:
        for value, count in _distinct(frame[name]).items():
            if not isinstance(value, str):
                continue
            if value == "":
                marker = EMPTY_TEXT
            elif value.strip() == "":
                marker = WHITESPACE
            elif value.strip().lower() in known:
                marker = value.strip()
            else:
                continue
            out.append(MarkerCount(name, marker, value, int(count)))
    return tuple(out)


def looks_like_marker(value: str) -> bool:
    return value.strip() == "" or value.strip().lower() in set(NULL_TOKENS)


# ------------------------------------------------------------------
# Plain numbers (for mean and median fills)
# ------------------------------------------------------------------

def is_plain_number(text: str) -> bool:
    return _PLAIN_NUMBER.fullmatch(text) is not None


@dataclass(frozen=True)
class NumericParse:
    """How the present (non-missing) values of a column read as plain numbers."""

    values: np.ndarray                  # float64, one per plain-number cell, in table order
    plain: int                          # Present cells that are plain numbers
    other: int                          # Present cells that are not
    other_examples: tuple[str, ...]     # Up to 5 distinct non-numeric values, most frequent first
    all_integral: bool


def parse_plain_numbers(series: pd.Series) -> NumericParse:
    """Read a column's present values as numbers, without changing the column.

    Only plain numbers count (see _PLAIN_NUMBER). The column itself stays text.
    """
    if not isinstance(series, pd.Series):
        series = pd.Series(series, copy=False)
    if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
        values = series.dropna().to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        return NumericParse(values, len(values), 0, (), bool(np.all(values == np.round(values))))
    present = series[~missing_mask(series)]
    distinct = _distinct(present)
    parsed: dict[object, float] = {}
    others: list[tuple[int, str]] = []
    for value, count in distinct.items():
        text = value if isinstance(value, str) else str(value)
        number = float(text) if is_plain_number(text) else math.nan
        if math.isfinite(number):
            parsed[value] = number
        else:
            others.append((int(count), text))
    numbers = present.map(parsed).to_numpy(dtype=float, na_value=np.nan)
    numbers = numbers[~np.isnan(numbers)]
    others.sort(key=lambda t: -t[0])          # Stable: ties keep first-appearance order
    return NumericParse(
        values=numbers,
        plain=len(numbers),
        other=sum(c for c, _ in others),
        other_examples=tuple(t for _, t in others[:5]),
        all_integral=bool(len(numbers)) and bool(np.all(numbers == np.floor(numbers))),
    )


def exact_mean(values: np.ndarray) -> float:
    """Mean with exactly rounded summation, so it never depends on summation order."""
    return math.fsum(values.tolist()) / len(values)


def exact_median(values: np.ndarray) -> float:
    ordered = np.sort(values, kind="stable")
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def format_number(value: float, decimals: int | None) -> str:
    """Text for a calculated fill value.

    decimals=None: whole numbers without a decimal point ("42"), others in
    their shortest exact form ("42.125"). Otherwise rounded to that many
    decimal places ("42.13").
    """
    if decimals is not None:
        text = f"{value:.{decimals}f}"
        return text.lstrip("-") if float(text) == 0 else text        # Never "-0.00"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value)) if value != 0 else "0"
    return repr(float(value))


__all__ = [
    "PRESET_TOKENS", "EMPTY_TEXT", "WHITESPACE", "ColumnMissing", "MarkerCount", "NumericParse",
    "exact_mean", "exact_median", "format_number", "is_plain_number", "is_whitespace_only",
    "looks_like_marker", "marker_matcher", "marker_matches", "missing_count", "missing_mask",
    "missing_pct", "missing_summary", "parse_plain_numbers", "scan_markers", "suggest_markers",
]
