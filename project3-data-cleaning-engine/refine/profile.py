"""Dataset and column profiling. Pure Python and pandas; never imports Streamlit.

Entry point:

    profile_dataset(frame) -> DatasetProfile

The profiler only reads. It never changes, converts or copies the input
table; every test it runs works on the column's distinct values, weighted
by how often each occurs, so cost grows with the number of distinct
values rather than the number of rows.

Vocabulary used throughout (and on the Profile & Quality page)
---------------------------------------------------------------
* Missing: the cell has no value. That is either pd.NA (an empty Excel
  cell, or a CSV row that ended early) or empty text "" (an empty CSV
  field such as "a,,b"). Both mean the file holds nothing there.
* Whitespace-only: text made only of spaces, tabs or line breaks. Looks
  empty but is not; counted separately, never as missing.
* Null-like token: text such as "NA", "N/A", "null", "None" or "-" that
  often stands for a missing value. Counted separately, never as missing,
  because only the user can confirm what it means.
* Values checked: non-missing cells that are neither whitespace-only nor
  a null-like token. Type inference and parsing indicators use these.
* Confirmed vs inferred type: a column stored as numbers, dates or
  booleans has a confirmed type (the storage says so). Every column REFINE
  loads is stored as text, so its semantic type is only inferred from the
  values: a suggestion, with the evidence for it. Columns converted with
  "Convert data type" (Int64, Float64, boolean, Arrow date32, datetime64)
  have a confirmed type.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from refine.source import TEXT_DTYPE

PROFILE_VERSION = "3"     # 2: dotted dates can be ambiguous; 3: typed columns from Convert data type

TOP_VALUES = 10                 # Most frequent values kept per column
TYPE_SHARE_STRONG = 0.90        # Share of values checked needed to suggest a type
TYPE_SHARE_MIXED = 0.50         # Below this, no dominant type: plain text
MIN_VALUES_FOR_TYPE = 5         # Fewer values checked: type checks are not assessed
CATEGORICAL_MAX_DISTINCT = 50
CATEGORICAL_MAX_RATIO = 0.5
IDENTIFIER_MIN_RATIO = 0.9
CASE_GROUP_EXAMPLES = 10

# Compared after trimming and lower-casing. "unknown" is deliberately not
# listed: it is often a legitimate category.
NULL_TOKENS = (
    "na", "n/a", "n.a.", "#n/a", "null", "#null!", "none", "nan", "nil",
    "-", "--", "?", "missing", "undefined",
)
BOOLEAN_WORDS = frozenset({"true", "false", "yes", "no", "y", "n", "t", "f"})
BOOLEAN_DIGITS = frozenset({"1", "0"})

SemanticType = Literal[
    "empty", "unknown", "boolean", "integer", "decimal", "date", "datetime",
    "email", "identifier", "categorical", "text", "mixed",
]
TypeStatus = Literal["confirmed", "inferred"]
Confidence = Literal["high", "medium", "low"]

SEMANTIC_LABELS = {
    "empty": "Empty", "unknown": "Unknown", "boolean": "Boolean", "integer": "Integer",
    "decimal": "Decimal", "date": "Date", "datetime": "Date & time", "email": "Email",
    "identifier": "Identifier", "categorical": "Categorical", "text": "Text", "mixed": "Mixed",
}

# All patterns use only syntax shared by Python's re and RE2 (pyarrow), and
# [0-9] rather than \d so both engines accept the same digits.
_PLAIN_NUMBER = r"^\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$"
_FORMAT_CHARS = r"[\s$€£¥%()]"
_GROUPED_NUMBER = r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$"
_LETTER = r"[A-Za-zÀ-ÖØ-öø-ÿ]"
_EMAIL = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PREFILTER = (
    r"^\s*(?:[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}|[A-Za-z]{3,9}\.? [0-9]{1,2},? [0-9]{4}"
    r"|[0-9]{1,2} [A-Za-z]{3,9}\.? [0-9]{4})"
)

# Date formats recognised, in reporting order: (label, regex, field order).
# "ambiguous" formats can be read month-first or day-first.
_TIME = r"(?:[ T]([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]+)?)?)?"
_DATE_FORMATS: tuple[tuple[str, re.Pattern, str], ...] = (
    ("YYYY-MM-DD", re.compile(rf"^([0-9]{{4}})-([0-9]{{1,2}})-([0-9]{{1,2}}){_TIME}$"), "ymd"),
    ("YYYY/MM/DD", re.compile(rf"^([0-9]{{4}})/([0-9]{{1,2}})/([0-9]{{1,2}}){_TIME}$"), "ymd"),
    ("MM/DD/YYYY or DD/MM/YYYY", re.compile(rf"^([0-9]{{1,2}})/([0-9]{{1,2}})/([0-9]{{4}}){_TIME}$"), "ambiguous"),
    ("MM/DD/YY or DD/MM/YY", re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{2})$"), "ambiguous2"),
    ("MM-DD-YYYY or DD-MM-YYYY", re.compile(rf"^([0-9]{{1,2}})-([0-9]{{1,2}})-([0-9]{{4}}){_TIME}$"), "ambiguous"),
    # Dotted dates are usually day-first (05.01.2024 = 5 January in much of
    # Europe), but month-first dotted dates exist too, so they are treated
    # like slashed dates: flagged as ambiguous when both readings are valid
    # and differ. Nothing is converted.
    ("DD.MM.YYYY or MM.DD.YYYY", re.compile(rf"^([0-9]{{1,2}})\.([0-9]{{1,2}})\.([0-9]{{4}}){_TIME}$"), "ambiguous"),
    ("Mon D, YYYY", re.compile(rf"^({_MONTHS})[a-z]*\.? ([0-9]{{1,2}}),? ([0-9]{{4}})$", re.I), "mdy_name"),
    ("D Mon YYYY", re.compile(rf"^([0-9]{{1,2}}) ({_MONTHS})[a-z]*\.? ([0-9]{{4}})$", re.I), "dmy_name"),
)
_MONTH_NUMBER = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


# ------------------------------------------------------------------
# Result models
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ValueCount:
    value: str
    count: int
    share: float            # Of all rows in the column


@dataclass(frozen=True)
class NumericStats:
    """Statistics over the values that parse as plain numbers.

    Values with currency symbols, thousands separators or percent signs are
    counted (`ColumnProfile.numeric_formatted_count`) but not included here,
    because reading them needs an assumption (for example whether "1,234"
    is one thousand or one point two).
    """

    count: int
    minimum: float
    maximum: float
    mean: float
    median: float
    std: float | None        # None with a single value
    p25: float
    p75: float
    zeros: int
    negatives: int
    all_integral: bool


@dataclass(frozen=True)
class LengthStats:
    minimum: int
    maximum: int
    mean: float
    median: float


@dataclass(frozen=True)
class DateFormatCount:
    label: str
    count: int


@dataclass(frozen=True)
class CaseVariantGroup:
    """Values that are the same text apart from capitalization (and spacing)."""

    variants: tuple[ValueCount, ...]     # Most frequent first
    total: int

    @property
    def minority_count(self) -> int:
        return self.total - self.variants[0].count


@dataclass(frozen=True)
class TypeInference:
    semantic_type: SemanticType
    status: TypeStatus
    confidence: Confidence
    evidence: str
    dominant_type: SemanticType | None = None    # Set when semantic_type == "mixed"
    matching_count: int = 0                      # Values checked that fit the suggested type

    @property
    def label(self) -> str:
        base = SEMANTIC_LABELS[self.semantic_type]
        if self.semantic_type == "mixed" and self.dominant_type:
            return f"Mixed (mostly {SEMANTIC_LABELS[self.dominant_type].lower()})"
        return base


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    position: int                    # 1-based
    storage_dtype: str
    n_rows: int
    # Missing and missing-like
    null_count: int                  # pd.NA / None / NaN
    empty_text_count: int            # ""
    whitespace_only_count: int
    null_token_count: int
    null_tokens: tuple[ValueCount, ...]
    # Distinct values (exact text, excluding missing)
    distinct_count: int
    top_values: tuple[ValueCount, ...]
    # Text
    text_values: bool                # False for confirmed numeric/date/boolean storage
    length: LengthStats | None
    leading_whitespace_count: int
    trailing_whitespace_count: int
    edge_whitespace_count: int       # Leading or trailing (or both), each cell counted once
    repeated_space_count: int        # Two or more consecutive spaces inside the value
    whitespace_examples: tuple[ValueCount, ...]   # Values with edge or repeated whitespace
    case_styles: dict[str, int]      # upper / lower / title counts among values with letters
    case_variant_groups: tuple[CaseVariantGroup, ...]
    case_variant_group_count: int
    case_variant_cells: int          # Cells not written like the most frequent variant
    # Parsing indicators, over values checked
    values_checked: int
    numeric_plain_count: int
    numeric_formatted_count: int
    numeric_stats: NumericStats | None
    non_numeric_examples: tuple[ValueCount, ...]
    date_count: int
    date_invalid_count: int          # Looks like a date but is not a calendar date
    date_with_time_count: int
    date_ambiguous_count: int        # Readable month-first and day-first, giving different dates
    date_formats: tuple[DateFormatCount, ...]
    non_date_examples: tuple[ValueCount, ...]
    boolean_count: int
    email_count: int
    python_types: tuple[str, ...]    # Only for object storage holding non-text values
    inference: TypeInference

    @property
    def missing_count(self) -> int:
        return self.null_count + self.empty_text_count

    @property
    def missing_pct(self) -> float:
        return _pct(self.missing_count, self.n_rows)

    @property
    def non_missing_count(self) -> int:
        return self.n_rows - self.missing_count

    @property
    def distinct_pct(self) -> float:
        return _pct(self.distinct_count, self.non_missing_count)

    @property
    def is_empty(self) -> bool:
        """No cell holds anything but missing values or whitespace."""
        return self.missing_count + self.whitespace_only_count == self.n_rows

    @property
    def numeric_count(self) -> int:
        return self.numeric_plain_count + self.numeric_formatted_count


@dataclass(frozen=True)
class DatasetProfile:
    n_rows: int
    n_columns: int
    missing_cells: int
    null_token_cells: int
    whitespace_only_cells: int
    duplicate_rows: int | None          # Rows repeating an earlier row; None if not assessed
    duplicate_groups: int | None        # Distinct rows that occur more than once
    duplicate_examples: tuple[int, ...]  # 0-based positions of the first repeated rows
    duplicate_error: str | None
    memory_bytes: int
    columns: tuple[ColumnProfile, ...]
    profile_seconds: float = field(default=0.0, compare=False)
    version: str = PROFILE_VERSION

    @property
    def n_cells(self) -> int:
        return self.n_rows * self.n_columns

    @property
    def missing_pct(self) -> float:
        return _pct(self.missing_cells, self.n_cells)

    @property
    def duplicate_pct(self) -> float | None:
        return None if self.duplicate_rows is None else _pct(self.duplicate_rows, self.n_rows)

    @property
    def empty_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_empty)

    @property
    def storage_type_counts(self) -> dict[str, int]:
        return dict(Counter(c.storage_dtype for c in self.columns))

    @property
    def semantic_type_counts(self) -> dict[str, int]:
        return dict(Counter(c.inference.semantic_type for c in self.columns))

    def column(self, name: str) -> ColumnProfile:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def _storage_kind(series: pd.Series) -> str:
    dtype = series.dtype
    if isinstance(dtype, pd.ArrowDtype) and pa.types.is_date(dtype.pyarrow_dtype):
        return "date"        # Before datetime: pandas treats Arrow dates as datetime64-like
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_numeric_dtype(dtype):
        return "integer" if pd.api.types.is_integer_dtype(dtype) else "decimal"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if isinstance(dtype, pd.StringDtype):
        return "string"
    return "object"


def _value_counts(series: pd.Series) -> pd.Series:
    """Counts of each distinct non-null value, most frequent first, ties in
    text order, so results never depend on hash order."""
    vc = series.value_counts(dropna=True, sort=False)
    if vc.empty:
        return vc
    return vc.sort_index(kind="stable").sort_values(ascending=False, kind="stable")


def _text_view(series: pd.Series) -> tuple[pd.Series, tuple[str, ...]]:
    """The column as text for analysis, without changing the column.

    String columns are used as they are (no copy). Object columns holding
    other Python values are read with str(); the types found are reported.
    """
    if isinstance(series.dtype, pd.StringDtype):
        return series, ()
    types: set[str] = set()

    def as_text(v):
        if v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT:
            return None
        if not isinstance(v, str):
            types.add(type(v).__name__)
            return str(v)
        return v

    view = pd.Series([as_text(v) for v in series.tolist()], dtype=TEXT_DTYPE, name=series.name)
    return view, tuple(sorted(types))


def _arrow_text(values: pd.Index | pd.Series) -> pd.Series:
    """Distinct values as an Arrow-backed text Series (vectorised RE2 matching)."""
    s = pd.Series(values, copy=False)
    if s.dtype != TEXT_DTYPE:
        s = s.astype(TEXT_DTYPE)
    return s.reset_index(drop=True)


def _arrow(values: pd.Series) -> pa.Array:
    """The Arrow array behind a text Series (no copy for Arrow-backed data)."""
    return pa.array(values.array, type=pa.large_string()) if len(values) else pa.array([], type=pa.large_string())


def _np(result) -> np.ndarray:
    return np.asarray(result.to_numpy(zero_copy_only=False))


def _weighted(mask: pd.Series | np.ndarray, counts: np.ndarray) -> int:
    m = np.asarray(mask, dtype=bool)
    return int(counts[m].sum())


def _examples(values: pd.Series, counts: np.ndarray, mask: np.ndarray, n_rows: int, limit: int = 5) -> tuple[ValueCount, ...]:
    idx = np.flatnonzero(mask)[:limit]
    return tuple(ValueCount(str(values.iloc[i]), int(counts[i]), _pct(int(counts[i]), n_rows)) for i in idx)


def _valid_date(y: int, m: int, d: int) -> bool:
    try:
        dt.date(y, m, d)
        return True
    except ValueError:
        return False


def _valid_time(groups: tuple) -> bool:
    hh, mm, ss = groups
    if hh is None:
        return True
    return int(hh) < 24 and int(mm) < 60 and (ss is None or int(ss) < 60)


def classify_date(text: str) -> tuple[str | None, bool, bool, bool]:
    """(format label or None, valid calendar date, ambiguous, has time) for one value."""
    value = text.strip()
    for label, pattern, order in _DATE_FORMATS:
        m = pattern.match(value)
        if not m:
            continue
        g = m.groups()
        time_ok = _valid_time(g[3:6]) if len(g) >= 6 else True
        has_time = len(g) >= 6 and g[3] is not None
        if order == "ymd":
            return label, time_ok and _valid_date(int(g[0]), int(g[1]), int(g[2])), False, has_time
        if order == "dmy":
            return label, time_ok and _valid_date(int(g[2]), int(g[1]), int(g[0])), False, has_time
        if order in ("ambiguous", "ambiguous2"):
            a, b, y = int(g[0]), int(g[1]), int(g[2])
            if order == "ambiguous2":
                y += 2000
            month_first, day_first = _valid_date(y, a, b), _valid_date(y, b, a)
            return label, time_ok and (month_first or day_first), month_first and day_first and a != b, has_time
        if order == "mdy_name":
            return label, _valid_date(int(g[2]), _MONTH_NUMBER[g[0].lower()[:3]], int(g[1])), False, False
        if order == "dmy_name":
            return label, _valid_date(int(g[2]), _MONTH_NUMBER[g[1].lower()[:3]], int(g[0])), False, False
    return None, False, False, False


def _numeric_stats(values: np.ndarray, counts: np.ndarray) -> NumericStats | None:
    finite = np.isfinite(values)
    values, counts = values[finite], counts[finite]
    total = int(counts.sum())
    if total == 0:
        return None
    full = np.repeat(values, counts)
    std = float(np.std(full, ddof=1)) if total > 1 else None
    p25, median, p75 = (float(q) for q in np.percentile(full, [25, 50, 75]))
    return NumericStats(
        count=total,
        minimum=float(values.min()),
        maximum=float(values.max()),
        mean=float(np.average(values, weights=counts)),
        median=median,
        std=std,
        p25=p25,
        p75=p75,
        zeros=_weighted(values == 0, counts),
        negatives=_weighted(values < 0, counts),
        all_integral=bool(np.all(np.mod(values, 1) == 0)),
    )


# ------------------------------------------------------------------
# Column profile
# ------------------------------------------------------------------

def profile_column(series: pd.Series, position: int = 1) -> ColumnProfile:
    """Profile one column. The series is read, never modified."""
    n_rows = len(series)
    storage = str(series.dtype)
    kind = _storage_kind(series)
    name = str(series.name)

    if kind in ("integer", "decimal", "boolean", "date", "datetime"):
        return _profile_typed(series, name, position, storage, kind)

    text, python_types = _text_view(series)
    null_count = int(text.isna().sum())
    vc = _value_counts(text)
    values = _arrow_text(vc.index)
    counts = vc.to_numpy(dtype=np.int64)

    is_empty_text = (values == "").to_numpy(dtype=bool)
    stripped = values.str.strip()
    is_ws_only = ((stripped == "") & ~(values == "")).to_numpy(dtype=bool)
    lowered = stripped.str.lower()
    is_token = lowered.isin(NULL_TOKENS).to_numpy(dtype=bool) & ~is_ws_only & ~is_empty_text
    checked = ~is_empty_text & ~is_ws_only & ~is_token
    present = ~is_empty_text               # Non-missing distinct values

    lengths = values.str.len().to_numpy(dtype=np.int64)
    length = None
    if present.any():
        full_len = np.repeat(lengths[present], counts[present])
        length = LengthStats(int(full_len.min()), int(full_len.max()),
                             float(full_len.mean()), float(np.median(full_len)))

    leading = values.str.match(r"^\s").to_numpy(dtype=bool) & present & ~is_ws_only
    trailing = values.str.contains(r"\s$").to_numpy(dtype=bool) & present & ~is_ws_only
    repeated = stripped.str.contains(r"  ").to_numpy(dtype=bool) & present

    # Numbers
    plain = values.str.match(_PLAIN_NUMBER).to_numpy(dtype=bool) & checked
    compact = values.str.replace(_FORMAT_CHARS, "", regex=True)
    has_symbol = values.str.contains(r"[$€£¥%(),]").to_numpy(dtype=bool)
    grouped_ok = (~compact.str.contains(",", regex=False) | compact.str.match(_GROUPED_NUMBER)).to_numpy(dtype=bool)
    formatted = (compact.str.replace(",", "", regex=False).str.match(_PLAIN_NUMBER).to_numpy(dtype=bool)
                 & has_symbol & grouped_ok & checked & ~plain)
    numeric_values = pd.to_numeric(stripped[plain], errors="coerce").to_numpy(dtype=float)
    numeric_stats = _numeric_stats(numeric_values, counts[plain]) if plain.any() else None

    # Dates: a vectorised pre-filter, then calendar checks on candidates only
    candidates = np.flatnonzero(values.str.match(_DATE_PREFILTER).to_numpy(dtype=bool) & checked)
    date_ok = np.zeros(len(values), dtype=bool)
    date_bad = np.zeros(len(values), dtype=bool)
    ambiguous = np.zeros(len(values), dtype=bool)
    with_time = np.zeros(len(values), dtype=bool)
    formats: Counter[str] = Counter()
    for i, text_value in zip(candidates, values.iloc[candidates].tolist()):
        label, valid, amb, has_time = classify_date(text_value)
        if label is None:
            continue
        if valid:
            date_ok[i] = True
            ambiguous[i] = amb
            with_time[i] = has_time
            formats[label] += int(counts[i])
        else:
            date_bad[i] = True
    order = {label: k for k, (label, _, _) in enumerate(_DATE_FORMATS)}
    date_formats = tuple(DateFormatCount(label, n) for label, n in
                         sorted(formats.items(), key=lambda kv: (-kv[1], order[kv[0]])))

    # Booleans: words always count; 1/0 only alongside words
    is_bool_word = lowered.isin(BOOLEAN_WORDS).to_numpy(dtype=bool) & checked
    is_bool_digit = lowered.isin(BOOLEAN_DIGITS).to_numpy(dtype=bool) & checked
    boolean = is_bool_word | (is_bool_digit if is_bool_word.any() else False)

    email = values.str.match(_EMAIL).to_numpy(dtype=bool) & checked

    # Capitalization
    has_letter = values.str.contains(_LETTER).to_numpy(dtype=bool) & checked
    upper = (values.str.upper() == values).to_numpy(dtype=bool) & has_letter
    lower = (values.str.lower() == values).to_numpy(dtype=bool) & has_letter & ~upper
    # pyarrow directly: pandas 2.2 runs str.title() through Python objects.
    raw = _arrow(values)
    title = _np(pc.equal(pc.utf8_title(raw), raw)).astype(bool) & has_letter & ~upper & ~lower
    case_styles = {"upper": _weighted(upper, counts), "lower": _weighted(lower, counts),
                   "title": _weighted(title, counts)}
    groups, group_count, variant_cells = _case_groups(values, counts, has_letter, n_rows)

    values_checked = _weighted(checked, counts)
    profile = ColumnProfile(
        name=name, position=position, storage_dtype=storage, n_rows=n_rows,
        null_count=null_count,
        empty_text_count=_weighted(is_empty_text, counts),
        whitespace_only_count=_weighted(is_ws_only, counts),
        null_token_count=_weighted(is_token, counts),
        null_tokens=_examples(values, counts, is_token, n_rows, limit=len(NULL_TOKENS)),
        distinct_count=int(present.sum()),
        top_values=_examples(values, counts, present, n_rows, limit=TOP_VALUES),
        text_values=True,
        length=length,
        leading_whitespace_count=_weighted(leading, counts),
        trailing_whitespace_count=_weighted(trailing, counts),
        edge_whitespace_count=_weighted(leading | trailing, counts),
        repeated_space_count=_weighted(repeated, counts),
        whitespace_examples=_examples(values, counts, leading | trailing | repeated, n_rows, limit=10),
        case_styles=case_styles,
        case_variant_groups=groups,
        case_variant_group_count=group_count,
        case_variant_cells=variant_cells,
        values_checked=values_checked,
        numeric_plain_count=_weighted(plain, counts),
        numeric_formatted_count=_weighted(formatted, counts),
        numeric_stats=numeric_stats,
        non_numeric_examples=_examples(values, counts, checked & ~plain & ~formatted, n_rows),
        date_count=_weighted(date_ok, counts),
        date_invalid_count=_weighted(date_bad, counts),
        date_with_time_count=_weighted(with_time, counts),
        date_ambiguous_count=_weighted(ambiguous, counts),
        date_formats=date_formats,
        non_date_examples=_examples(values, counts, checked & ~date_ok, n_rows),
        boolean_count=_weighted(boolean, counts),
        email_count=_weighted(email, counts),
        python_types=python_types,
        inference=TypeInference("unknown", "inferred", "low", ""),   # Replaced below
    )
    distinct_checked = int(checked.sum())
    has_space = bool((stripped.str.contains(r"\s").to_numpy(dtype=bool) & checked).any())
    has_digit = bool((values.str.contains(r"[0-9]").to_numpy(dtype=bool) & checked).any())
    inference = _infer_text_type(profile, distinct_checked, has_space, has_digit)
    # Numeric statistics only where the values are mostly numbers; a yes/no
    # column holding some 1/0 values does not get a mean.
    numeric_like = {inference.semantic_type, inference.dominant_type} & {"integer", "decimal"}
    return replace(profile, inference=inference,
                   numeric_stats=profile.numeric_stats if numeric_like else None)


def _case_groups(values: pd.Series, counts: np.ndarray, has_letter: np.ndarray,
                 n_rows: int) -> tuple[tuple[CaseVariantGroup, ...], int, int]:
    """Groups of values equal apart from capitalization (spacing normalised)."""
    idx = np.flatnonzero(has_letter)
    if len(idx) < 2:
        return (), 0, 0
    # pyarrow compute throughout, so pandas 2.2 does not fall back to Python objects.
    spaced = pc.replace_substring_regex(pc.utf8_trim_whitespace(_arrow(values.iloc[idx])), r"\s+", " ")
    keys = pc.utf8_lower(spaced)
    # Only values whose lower-cased form occurs more than once can form a
    # group; filtering first keeps high-cardinality columns cheap.
    key_codes = pc.dictionary_encode(keys).indices.to_numpy(zero_copy_only=False)
    shared = np.bincount(key_codes)[key_codes] > 1
    if not shared.any():
        return (), 0, 0
    keep = pa.array(shared)
    frame = pd.DataFrame({
        "key": keys.filter(keep).to_pylist(),
        "variant": spaced.filter(keep).to_pylist(),
        "count": counts[idx][shared],
    })
    per_variant = frame.groupby(["key", "variant"], sort=True)["count"].sum().reset_index()
    sizes = per_variant.groupby("key", sort=True)["variant"].size()
    multi = sizes[sizes > 1].index
    if len(multi) == 0:
        return (), 0, 0
    per_variant = per_variant[per_variant["key"].isin(multi)]
    groups: list[CaseVariantGroup] = []
    cells = 0
    for _, g in per_variant.groupby("key", sort=True):
        g = g.sort_values(["count", "variant"], ascending=[False, True], kind="stable")
        total = int(g["count"].sum())
        variants = tuple(ValueCount(v, int(c), _pct(int(c), n_rows)) for v, c in zip(g["variant"], g["count"]))
        group = CaseVariantGroup(variants, total)
        cells += group.minority_count
        groups.append(group)
    groups.sort(key=lambda g: (-g.minority_count, g.variants[0].value))
    return tuple(groups[:CASE_GROUP_EXAMPLES]), len(groups), cells


def _infer_text_type(p: ColumnProfile, distinct_checked: int, has_space: bool, has_digit: bool) -> TypeInference:
    n = p.values_checked
    if p.is_empty:
        return TypeInference("empty", "inferred", "high", "Every cell is missing or whitespace-only.")
    if n == 0:
        return TypeInference("unknown", "inferred", "low",
                             "The only values are null-like tokens, so no type can be suggested.")

    integral = p.numeric_stats is None or p.numeric_stats.all_integral
    numeric_type: SemanticType = "integer" if integral else "decimal"
    date_type: SemanticType = "datetime" if p.date_with_time_count else "date"
    scores: list[tuple[SemanticType, int, str]] = [
        ("boolean", p.boolean_count, "are yes/no style values"),
        (numeric_type, p.numeric_count, "parse as numbers"),
        (date_type, p.date_count, "are valid dates"),
        ("email", p.email_count, "look like email addresses"),
    ]
    best_type, best_count, phrase = max(scores, key=lambda s: s[1])   # First wins ties
    share = best_count / n
    evidence = f"{best_count:,} of {n:,} values checked ({100 * share:.1f}%) {phrase}"
    if p.numeric_formatted_count and best_type == numeric_type:
        evidence += f", {p.numeric_formatted_count:,} of them only after removing symbols or separators"
    evidence += "."
    small = n < MIN_VALUES_FOR_TYPE
    if share == 1.0 and best_count:
        conf: Confidence = "low" if small else "high"
        return TypeInference(best_type, "inferred", conf, evidence, matching_count=best_count)
    if share >= TYPE_SHARE_STRONG:
        return TypeInference(best_type, "inferred", "low" if small else "medium", evidence,
                             matching_count=best_count)
    if share >= TYPE_SHARE_MIXED:
        return TypeInference("mixed", "inferred", "low", evidence, dominant_type=best_type,
                             matching_count=best_count)

    ratio = distinct_checked / n
    if not has_space and has_digit and ratio >= IDENTIFIER_MIN_RATIO and n >= 10:
        return TypeInference("identifier", "inferred", "low",
                             f"{distinct_checked:,} distinct values for {n:,} values checked, "
                             "short codes without spaces that contain digits.",
                             matching_count=n)
    if distinct_checked <= CATEGORICAL_MAX_DISTINCT and ratio <= CATEGORICAL_MAX_RATIO:
        return TypeInference("categorical", "inferred", "low" if small else "medium",
                             f"{distinct_checked:,} distinct values repeat across {n:,} values checked.",
                             matching_count=n)
    return TypeInference("text", "inferred", "low" if small else "medium",
                         "No number, date, boolean or email pattern covers most values.",
                         matching_count=n)


def _typed_text(value, kind: str) -> str:
    """Typed values as REFINE writes them (true/false, ISO dates), not Python's repr."""
    if kind == "boolean":
        return "true" if bool(value) else "false"
    if kind == "date":
        return value.isoformat()
    if kind == "datetime":
        return pd.Timestamp(value).isoformat(sep=" ")
    if kind == "decimal":
        f = float(value)
        return f"{f:.1f}" if f.is_integer() and abs(f) < 1e16 else repr(f)
    return str(value)


def _profile_typed(series: pd.Series, name: str, position: int, storage: str, kind: str) -> ColumnProfile:
    """Columns already stored as numbers, booleans or datetimes: type confirmed."""
    n_rows = len(series)
    null_count = int(series.isna().sum())
    vc = _value_counts(series)
    counts = vc.to_numpy(dtype=np.int64)
    present_n = n_rows - null_count
    top = tuple(ValueCount(_typed_text(v, kind), int(c), _pct(int(c), n_rows))
                for v, c in zip(vc.index[:TOP_VALUES], counts[:TOP_VALUES]))
    numeric_stats = None
    if kind in ("integer", "decimal") and len(vc):
        numeric_stats = _numeric_stats(np.asarray(vc.index, dtype=float), counts)
    semantic: SemanticType = {"integer": "integer", "decimal": "decimal", "boolean": "boolean",
                              "date": "date", "datetime": "datetime"}[kind]
    if present_n == 0:
        inference = TypeInference("empty", "confirmed", "high",
                                  f"Stored as {storage}; every cell is missing.")
    else:
        inference = TypeInference(semantic, "confirmed", "high", f"Stored as {storage}.",
                                  matching_count=present_n)
    return ColumnProfile(
        name=name, position=position, storage_dtype=storage, n_rows=n_rows,
        null_count=null_count, empty_text_count=0, whitespace_only_count=0,
        null_token_count=0, null_tokens=(), distinct_count=len(vc), top_values=top,
        text_values=False, length=None, leading_whitespace_count=0,
        trailing_whitespace_count=0, edge_whitespace_count=0, repeated_space_count=0,
        whitespace_examples=(), case_styles={},
        case_variant_groups=(), case_variant_group_count=0, case_variant_cells=0,
        values_checked=present_n,
        numeric_plain_count=present_n if kind in ("integer", "decimal") else 0,
        numeric_formatted_count=0, numeric_stats=numeric_stats, non_numeric_examples=(),
        date_count=present_n if kind in ("date", "datetime") else 0, date_invalid_count=0,
        date_with_time_count=0, date_ambiguous_count=0, date_formats=(), non_date_examples=(),
        boolean_count=present_n if kind == "boolean" else 0, email_count=0,
        python_types=(), inference=inference,
    )


# ------------------------------------------------------------------
# Dataset profile
# ------------------------------------------------------------------

def _duplicates(frame: pd.DataFrame) -> tuple[int | None, int | None, tuple[int, ...], str | None]:
    if frame.shape[1] == 0:
        return None, None, (), "The table has no columns."
    try:
        repeated = frame.duplicated(keep="first")
        any_dup = frame.duplicated(keep=False)
    except TypeError as exc:      # Unhashable cell values (lists, dicts) in object columns
        return None, None, (), f"Rows contain values that cannot be compared ({exc})."
    n_repeated = int(repeated.sum())
    groups = int(any_dup.sum()) - n_repeated
    examples = tuple(int(i) for i in np.flatnonzero(repeated.to_numpy())[:10])
    return n_repeated, groups, examples, None


def profile_dataset(frame: pd.DataFrame) -> DatasetProfile:
    """Profile a whole table. The frame is read, never modified or copied."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("profile_dataset expects a pandas DataFrame.")
    if not frame.columns.is_unique:
        raise ValueError("Column names must be unique to profile a table.")
    start = time.perf_counter()
    columns = tuple(profile_column(frame.iloc[:, i], position=i + 1) for i in range(frame.shape[1]))
    dup, groups, examples, dup_error = _duplicates(frame)
    return DatasetProfile(
        n_rows=len(frame),
        n_columns=frame.shape[1],
        missing_cells=sum(c.missing_count for c in columns),
        null_token_cells=sum(c.null_token_count for c in columns),
        whitespace_only_cells=sum(c.whitespace_only_count for c in columns),
        duplicate_rows=dup,
        duplicate_groups=groups,
        duplicate_examples=examples,
        duplicate_error=dup_error,
        memory_bytes=int(frame.memory_usage(deep=True, index=True).sum()),
        columns=columns,
        profile_seconds=time.perf_counter() - start,
    )
