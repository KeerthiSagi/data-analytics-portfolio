"""Before-and-after comparison of the original and current tables. Read-only.

Entry point:

    compare_datasets(original, current, row_ids=...) -> DatasetComparison

Nothing here changes either table, and nothing is copied whole: columns
are read one at a time, and only the rows that survived cleaning are
taken from the original column being compared.

Definitions are not new. They come from the existing modules, so the
Compare page agrees with the Profile & Quality page:

* Missing cells, missing percentages, duplicate rows and the numeric and
  categorical statistics come from `profile_dataset` (refine/profile.py).
  Missing means no value or empty text; duplicates are rows that repeat an
  earlier row exactly in every column, the first occurrence not counted.
* Quality findings come from `assess_quality` (refine/quality.py), run
  with the same validity rules on both tables. There is no overall score.
* Column types are the storage kinds from refine/conversion.py.

Row matching
------------
Cells are compared only through row lineage: `row_ids[i]` is the 0-based
position, in the original table, of current row i. CleaningSession keeps
this for every state. Without valid lineage there is no cell comparison;
the result says why, and only the aggregate comparisons are given. Equal
row counts are never taken as proof that rows correspond, and the tables'
pandas index labels are never used to match rows.

Lineage is valid when there is one id per current row, every id is a
position in the original table, and no id repeats. REFINE operations only
keep or remove rows, so the current table never has rows that are not in
the original.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from refine.conversion import KIND_LABELS, canonical_text, column_kind, dtype_name
from refine.missing import missing_mask
from refine.profile import ColumnProfile, DatasetProfile, NumericStats, ValueCount, profile_dataset
from refine.quality import QualityIssue, QualityReport, ValidityRule, assess_quality
from refine.source import TEXT_DTYPE

COMPARE_VERSION = "1"
CHANGE_EXAMPLES = 5            # Changed cells kept per column, as examples
TOP_VALUES_COMPARED = 5        # Most frequent values compared per column
PARAM_TEXT_LIMIT = 60          # Characters of one setting shown in the history summary
PARAM_LIST_LIMIT = 5           # Items of a list setting shown in the history summary

Direction = Literal["increase", "decrease", "no change", "not available"]
ColumnStatus = Literal["retained", "added", "removed"]
IssueStatus = Literal["unchanged", "changed", "new", "no longer detected"]

# Column kinds grouped as the overview counts them.
KIND_GROUPS = {"integer": "numeric", "decimal": "numeric", "text": "text", "date": "datetime",
               "datetime": "datetime", "boolean": "boolean", "other": "other"}
GROUP_LABELS = {"numeric": "Numeric columns", "text": "Text columns", "datetime": "Date and time columns",
                "boolean": "True / false columns", "other": "Other columns"}


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------

def _direction(original: float | None, current: float | None) -> Direction:
    if original is None or current is None:
        return "not available"
    if current > original:
        return "increase"
    if current < original:
        return "decrease"
    return "no change"


def _diff(original: float | None, current: float | None) -> float | None:
    if original is None or current is None:
        return None
    return current - original


@dataclass(frozen=True)
class MetricChange:
    """One number before and after. The direction says which way it moved, not whether that is good."""

    key: str
    label: str
    original: float | None
    current: float | None
    unit: str = "count"                  # "count" or "percent"

    @property
    def difference(self) -> float | None:
        return _diff(self.original, self.current)

    @property
    def direction(self) -> Direction:
        return _direction(self.original, self.current)


@dataclass(frozen=True)
class ColumnMissingChange:
    """Missing cells in one column. Each percentage uses its own table's row count."""

    name: str
    status: ColumnStatus
    original_missing: int | None         # None: the column is not in that table
    current_missing: int | None
    original_rows: int
    current_rows: int

    @property
    def original_pct(self) -> float | None:
        return _pct_or_none(self.original_missing, self.original_rows)

    @property
    def current_pct(self) -> float | None:
        return _pct_or_none(self.current_missing, self.current_rows)

    @property
    def difference(self) -> int | None:
        return _diff(self.original_missing, self.current_missing)

    @property
    def direction(self) -> Direction:
        return _direction(self.original_missing, self.current_missing)

    @property
    def changed(self) -> bool:
        return self.status != "retained" or self.original_missing != self.current_missing


@dataclass(frozen=True)
class DuplicateChange:
    """Exact duplicate rows as the profile counts them (rows repeating an earlier row in every column)."""

    original_rows: int
    current_rows: int
    original_duplicates: int | None      # None: not assessed (reason in *_error)
    current_duplicates: int | None
    original_groups: int | None
    current_groups: int | None
    original_error: str | None
    current_error: str | None

    @property
    def original_pct(self) -> float | None:
        return _pct_or_none(self.original_duplicates, self.original_rows)

    @property
    def current_pct(self) -> float | None:
        return _pct_or_none(self.current_duplicates, self.current_rows)

    @property
    def difference(self) -> int | None:
        return _diff(self.original_duplicates, self.current_duplicates)

    @property
    def direction(self) -> Direction:
        return _direction(self.original_duplicates, self.current_duplicates)


@dataclass(frozen=True)
class ColumnTypeChange:
    name: str
    original_dtype: str
    current_dtype: str
    original_kind: str
    current_kind: str
    original_position: int               # 1-based
    current_position: int

    @property
    def changed(self) -> bool:
        return self.original_dtype != self.current_dtype

    @property
    def original_label(self) -> str:
        return KIND_LABELS.get(self.original_kind, "Other")

    @property
    def current_label(self) -> str:
        return KIND_LABELS.get(self.current_kind, "Other")


@dataclass(frozen=True)
class ColumnSummary:
    """A column present in only one of the tables."""

    name: str
    dtype: str
    kind: str
    position: int


@dataclass(frozen=True)
class ColumnChanges:
    retained: tuple[ColumnTypeChange, ...]   # In current-table order
    added: tuple[ColumnSummary, ...]
    removed: tuple[ColumnSummary, ...]
    order_changed: bool                      # Retained columns appear in a different order

    @property
    def type_changed(self) -> tuple[ColumnTypeChange, ...]:
        return tuple(c for c in self.retained if c.changed)


@dataclass(frozen=True)
class IssueChange:
    """One quality finding (check × column) in either table."""

    check: str
    category: str
    column: str | None
    original: QualityIssue | None
    current: QualityIssue | None

    @property
    def scope(self) -> str:
        return self.column if self.column is not None else "Dataset"

    @property
    def status(self) -> IssueStatus:
        if self.original is None:
            return "new"
        if self.current is None:
            return "no longer detected"
        same = (self.original.affected_count, self.original.denominator) == \
               (self.current.affected_count, self.current.denominator)
        return "unchanged" if same else "changed"


@dataclass(frozen=True)
class QualityComparison:
    original_severity: dict[str, int]
    current_severity: dict[str, int]
    original_not_assessed: int
    current_not_assessed: int
    issues: tuple[IssueChange, ...]
    rules: tuple[ValidityRule, ...]

    @property
    def changed_issues(self) -> tuple[IssueChange, ...]:
        return tuple(i for i in self.issues if i.status != "unchanged")


@dataclass(frozen=True)
class CellExample:
    original_row: int                    # 1-based row of the original table
    before: str
    after: str


@dataclass(frozen=True)
class ColumnCellChanges:
    """Cell-by-cell result for one retained column, over the rows both tables hold."""

    name: str
    compared_as: Literal["values", "text"]   # "text" when the column's type changed
    cells: int
    unchanged: int
    modified: int                        # A value replaced by a different value
    filled: int                          # Missing before, has a value now
    made_missing: int                    # Had a value, missing now
    examples: tuple[CellExample, ...] = ()

    @property
    def changed(self) -> int:
        return self.modified + self.filled + self.made_missing


@dataclass(frozen=True)
class CellComparison:
    """Exact cell-level comparison through row lineage, or the reason it is unavailable."""

    available: bool
    reason: str | None                   # Why it is unavailable
    original_rows: int
    current_rows: int
    rows_kept: int = 0
    rows_removed: int = 0
    rows_added: int = 0                  # Always 0 for valid lineage (see module docstring)
    order_preserved: bool = True
    columns: tuple[ColumnCellChanges, ...] = ()

    @property
    def cells_compared(self) -> int:
        return sum(c.cells for c in self.columns)

    @property
    def cells_changed(self) -> int:
        return sum(c.changed for c in self.columns)

    def column(self, name: str) -> ColumnCellChanges | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class ColumnDetail:
    """Profile statistics of one column before and after (either side may be absent)."""

    name: str
    status: ColumnStatus
    original: ColumnProfile | None = field(repr=False)
    current: ColumnProfile | None = field(repr=False)

    @property
    def has_numeric(self) -> bool:
        return any(p is not None and p.numeric_stats is not None for p in (self.original, self.current))

    def numeric_changes(self) -> tuple[MetricChange, ...]:
        """Count, min, max, mean, median and standard deviation; None where a side has no statistics."""
        a = self.original.numeric_stats if self.original is not None else None
        b = self.current.numeric_stats if self.current is not None else None
        rows = []
        for key, label in (("count", "Values used"), ("minimum", "Minimum"), ("maximum", "Maximum"),
                           ("mean", "Mean"), ("median", "Median"), ("std", "Standard deviation")):
            rows.append(MetricChange(key, label, _stat(a, key), _stat(b, key), "count" if key == "count" else "value"))
        return tuple(rows)

    def categorical_changes(self) -> tuple[MetricChange, ...]:
        a, b = self.original, self.current
        return (
            MetricChange("distinct", "Distinct values", a.distinct_count if a else None,
                         b.distinct_count if b else None),
            MetricChange("missing", "Missing cells", a.missing_count if a else None, b.missing_count if b else None),
            MetricChange("non_missing", "Cells with a value", a.non_missing_count if a else None,
                         b.non_missing_count if b else None),
        )

    def top_values(self) -> tuple[tuple[ValueCount, ...], tuple[ValueCount, ...]]:
        a = self.original.top_values[:TOP_VALUES_COMPARED] if self.original else ()
        b = self.current.top_values[:TOP_VALUES_COMPARED] if self.current else ()
        return a, b


@dataclass(frozen=True)
class HistoryItem:
    """One applied step, for display. No fingerprints or frozen payloads."""

    number: int
    operation: str
    label: str
    summary: str
    columns: tuple[str, ...]
    settings: tuple[tuple[str, str], ...]
    counts: tuple[tuple[str, int], ...]
    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int


@dataclass(frozen=True, eq=False)
class DatasetComparison:
    overview: tuple[MetricChange, ...]
    missing: tuple[ColumnMissingChange, ...]
    duplicates: DuplicateChange
    columns: ColumnChanges
    quality: QualityComparison
    cells: CellComparison
    details: tuple[ColumnDetail, ...]
    # The profiles and reports the comparison was built from, so a caller can reuse them.
    original_profile: DatasetProfile = field(repr=False)
    current_profile: DatasetProfile = field(repr=False)
    original_quality: QualityReport = field(repr=False)
    current_quality: QualityReport = field(repr=False)
    version: str = COMPARE_VERSION

    @property
    def identical(self) -> bool:
        """Same columns, dtypes and rows, and no cell differs (needs lineage)."""
        c = self.columns
        return (self.cells.available and not c.added and not c.removed and not c.type_changed
                and not c.order_changed and self.cells.rows_removed == 0 and self.cells.cells_changed == 0)

    def metric(self, key: str) -> MetricChange:
        return next(m for m in self.overview if m.key == key)

    def column_missing(self, name: str) -> ColumnMissingChange:
        return next(m for m in self.missing if m.name == name)

    def detail(self, name: str) -> ColumnDetail:
        return next(d for d in self.details if d.name == name)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pct_or_none(part: int | None, whole: int) -> float | None:
    if part is None:
        return None
    return 100.0 * part / whole if whole else 0.0


def _stat(stats: NumericStats | None, key: str) -> float | None:
    if stats is None:
        return None
    value = getattr(stats, key)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value)


def _kind_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts = dict.fromkeys(GROUP_LABELS, 0)
    for dtype in frame.dtypes:
        counts[KIND_GROUPS[column_kind(dtype)]] += 1
    return counts


# ------------------------------------------------------------------
# Aggregate comparisons (from the profiles)
# ------------------------------------------------------------------

def _overview(original: pd.DataFrame, current: pd.DataFrame, a: DatasetProfile, b: DatasetProfile) -> tuple:
    ka, kb = _kind_counts(original), _kind_counts(current)
    rows = [
        MetricChange("rows", "Rows", a.n_rows, b.n_rows),
        MetricChange("columns", "Columns", a.n_columns, b.n_columns),
        MetricChange("cells", "Cells", a.n_cells, b.n_cells),
        MetricChange("missing_cells", "Missing cells", a.missing_cells, b.missing_cells),
        MetricChange("missing_pct", "Missing cells (% of cells)", a.missing_pct, b.missing_pct, "percent"),
        MetricChange("duplicate_rows", "Duplicate rows", a.duplicate_rows, b.duplicate_rows),
    ]
    for group in ("numeric", "text", "datetime", "boolean", "other"):
        if group in ("boolean", "other") and not (ka[group] or kb[group]):
            continue                      # Shown only when either table has such columns
        rows.append(MetricChange(f"{group}_columns", GROUP_LABELS[group], ka[group], kb[group]))
    return tuple(rows)


def _missing(a: DatasetProfile, b: DatasetProfile) -> tuple[ColumnMissingChange, ...]:
    before = {c.name: c for c in a.columns}
    after = {c.name: c for c in b.columns}
    out = []
    for c in b.columns:                   # Current order, then removed columns in original order
        o = before.get(c.name)
        out.append(ColumnMissingChange(c.name, "retained" if o else "added", o.missing_count if o else None,
                                       c.missing_count, a.n_rows, b.n_rows))
    for c in a.columns:
        if c.name not in after:
            out.append(ColumnMissingChange(c.name, "removed", c.missing_count, None, a.n_rows, b.n_rows))
    return tuple(out)


def _duplicates(a: DatasetProfile, b: DatasetProfile) -> DuplicateChange:
    return DuplicateChange(a.n_rows, b.n_rows, a.duplicate_rows, b.duplicate_rows, a.duplicate_groups,
                           b.duplicate_groups, a.duplicate_error, b.duplicate_error)


def _columns(original: pd.DataFrame, current: pd.DataFrame) -> ColumnChanges:
    before = {name: i for i, name in enumerate(original.columns)}
    after = {name: i for i, name in enumerate(current.columns)}
    retained, added = [], []
    for name, j in after.items():
        dtype = current.dtypes.iloc[j]
        if name in before:
            i = before[name]
            odt = original.dtypes.iloc[i]
            retained.append(ColumnTypeChange(name, dtype_name(odt), dtype_name(dtype), column_kind(odt),
                                             column_kind(dtype), i + 1, j + 1))
        else:
            added.append(ColumnSummary(name, dtype_name(dtype), column_kind(dtype), j + 1))
    removed = [ColumnSummary(name, dtype_name(original.dtypes.iloc[i]), column_kind(original.dtypes.iloc[i]), i + 1)
               for name, i in before.items() if name not in after]
    kept_in_original_order = [n for n in original.columns if n in after]
    order_changed = kept_in_original_order != [c.name for c in retained]
    return ColumnChanges(tuple(retained), tuple(added), tuple(removed), order_changed)


def _quality(a: QualityReport, b: QualityReport) -> QualityComparison:
    def key(i: QualityIssue) -> tuple:
        return (i.check, i.column)

    before = {key(i): i for i in a.issues}
    after = {key(i): i for i in b.issues}
    keys = list(before)
    keys += [k for k in after if k not in before]
    issues = []
    for k in keys:
        ref = before.get(k) or after[k]
        issues.append(IssueChange(ref.check, ref.category, ref.column, before.get(k), after.get(k)))
    return QualityComparison(a.severity_counts(), b.severity_counts(), len(a.not_assessed), len(b.not_assessed),
                             tuple(issues), b.rules)


def _details(a: DatasetProfile, b: DatasetProfile) -> tuple[ColumnDetail, ...]:
    before = {c.name: c for c in a.columns}
    after = {c.name: c for c in b.columns}
    out = [ColumnDetail(c.name, "retained" if c.name in before else "added", before.get(c.name), c)
           for c in b.columns]
    out += [ColumnDetail(c.name, "removed", c, None) for c in a.columns if c.name not in after]
    return tuple(out)


# ------------------------------------------------------------------
# Cell-level comparison (through row lineage)
# ------------------------------------------------------------------

def check_lineage(row_ids: Any, original_rows: int, current_rows: int) -> tuple[np.ndarray | None, str | None]:
    """(ids as int64, None) when lineage can be trusted, else (None, reason)."""
    if row_ids is None:
        return None, ("Row lineage is not available, so rows of the two tables cannot be matched. "
                      "Equal row counts alone do not show which rows correspond.")
    try:
        ids = np.asarray(row_ids)
    except Exception:  # noqa: BLE001 - any unreadable input means no lineage
        return None, "The row lineage could not be read."
    if ids.ndim != 1 or len(ids) != current_rows:
        return None, "The row lineage does not have one entry per current row."
    if len(ids) and not np.issubdtype(ids.dtype, np.integer):
        return None, "The row lineage does not hold whole-number row positions."
    ids = ids.astype(np.int64, copy=False)
    if len(ids) and (ids.min() < 0 or ids.max() >= original_rows):
        return None, "The row lineage refers to rows that are not in the original table."
    if len(np.unique(ids)) != len(ids):
        return None, "The row lineage refers to the same original row more than once."
    return ids, None


def _as_text(series: pd.Series) -> pd.Series:
    """Values as REFINE writes them as text (typed values in canonical form); missing stays missing."""
    kind = column_kind(series.dtype)
    if kind == "text":
        return series
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    texts = [canonical_text(v, kind) if kind != "other" else str(v) for v in list(uniques)]
    mapped = np.empty(len(codes), dtype=object)
    present = codes >= 0
    mapped[~present] = None
    if len(texts):
        mapped[present] = np.asarray(texts, dtype=object)[codes[present]]
    return pd.Series(mapped, dtype=TEXT_DTYPE)


def _equal(a: pd.Series, b: pd.Series) -> np.ndarray:
    """True where both cells hold the same stored value. Two cells with no value are equal;
    no value and empty text are not."""
    a_na = a.isna().to_numpy(dtype=bool)
    b_na = b.isna().to_numpy(dtype=bool)
    out = a_na & b_na
    both = ~a_na & ~b_na
    if not both.any():
        return out
    try:
        eq = np.array((a.reset_index(drop=True) == b.reset_index(drop=True)).to_numpy(dtype=bool, na_value=False))
    except (TypeError, ValueError):
        av, bv = a.to_numpy(dtype=object), b.to_numpy(dtype=object)
        eq = np.zeros(len(a), dtype=bool)
        for i in np.flatnonzero(both):
            try:
                eq[i] = bool(av[i] == bv[i]) and type(av[i]) is type(bv[i])
            except (TypeError, ValueError):
                eq[i] = False
    if a.dtype == object or b.dtype == object:
        # 1 == 1.0 == True in Python; object cells of different types are different values.
        av, bv = a.to_numpy(dtype=object), b.to_numpy(dtype=object)
        idx = np.flatnonzero(both & eq)
        if len(idx):
            same_type = np.fromiter((type(av[i]) is type(bv[i]) for i in idx), dtype=bool, count=len(idx))
            eq[idx[~same_type]] = False
    return out | (both & eq)


def _shown(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "(no value)"
    if isinstance(value, str):
        if value == "":
            return "(empty text)"
        text = value if len(value) <= 80 else value[:77] + "..."
        return '"' + text.replace("\t", "\\t").replace("\n", "\\n") + '"'
    return str(value)


def _column_cells(name: str, before: pd.Series, after: pd.Series, ids: np.ndarray) -> ColumnCellChanges:
    """before: the original column at the matched rows; after: the current column."""
    as_text = dtype_name(before.dtype) != dtype_name(after.dtype)
    a = _as_text(before) if as_text else before
    b = _as_text(after) if as_text else after
    equal = _equal(a, b)
    a_missing = missing_mask(before)
    b_missing = missing_mask(after)
    changed = ~equal
    filled = changed & a_missing & ~b_missing
    made_missing = changed & ~a_missing & b_missing
    modified = changed & ~filled & ~made_missing
    examples = []
    positions = np.flatnonzero(changed)[:CHANGE_EXAMPLES]
    if len(positions):
        bv = before.iloc[positions].tolist()
        av = after.iloc[positions].tolist()
        examples = [CellExample(int(ids[p]) + 1, _shown(x), _shown(y)) for p, x, y in zip(positions, bv, av)]
    n = len(after)
    return ColumnCellChanges(name, "text" if as_text else "values", n, int(equal.sum()), int(modified.sum()),
                             int(filled.sum()), int(made_missing.sum()), tuple(examples))


def compare_cells(original: pd.DataFrame, current: pd.DataFrame, row_ids: Any) -> CellComparison:
    """Compare every retained column cell by cell, matching rows only through `row_ids`."""
    n_orig, n_cur = len(original), len(current)
    ids, reason = check_lineage(row_ids, n_orig, n_cur)
    if ids is None:
        return CellComparison(False, reason, n_orig, n_cur)
    before_pos = {name: i for i, name in enumerate(original.columns)}
    columns = []
    for j, name in enumerate(current.columns):
        if name not in before_pos:
            continue
        before = original.iloc[:, before_pos[name]].take(ids).reset_index(drop=True)
        after = current.iloc[:, j].reset_index(drop=True)
        columns.append(_column_cells(name, before, after, ids))
    order = bool(np.all(np.diff(ids) > 0)) if len(ids) > 1 else True
    return CellComparison(True, None, n_orig, n_cur, rows_kept=n_cur, rows_removed=n_orig - n_cur,
                          rows_added=0, order_preserved=order, columns=tuple(columns))


# ------------------------------------------------------------------
# Cleaning history (from the recorded steps; never reconstructed)
# ------------------------------------------------------------------

def _setting_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        shown = [_setting_text(v) for v in value[:PARAM_LIST_LIMIT]]
        more = f", and {len(value) - PARAM_LIST_LIMIT} more" if len(value) > PARAM_LIST_LIMIT else ""
        return ", ".join(shown) + more if shown else "(none)"
    if isinstance(value, Mapping):
        items = list(value.items())
        shown = [f"{k}: {_setting_text(v)}" for k, v in items[:PARAM_LIST_LIMIT]]
        more = f", and {len(items) - PARAM_LIST_LIMIT} more" if len(items) > PARAM_LIST_LIMIT else ""
        return "; ".join(shown) + more if shown else "(none)"
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    return text if len(text) <= PARAM_TEXT_LIMIT else text[:PARAM_TEXT_LIMIT - 3] + "..."


def history_summary(steps: Sequence[Any], audit_log: Sequence[Any]) -> tuple[HistoryItem, ...]:
    """The active steps of a CleaningSession, in order, from its own records."""
    audit = {e.sequence: e for e in audit_log}
    items = []
    for number, step in enumerate(steps, start=1):
        plan = step.plan
        entry = audit.get(step.audit_sequence)
        settings = tuple((str(k), _setting_text(v)) for k, v in sorted(plan.step.params.items()))
        counts = tuple((str(k), int(v)) for k, v in step.counts.items())
        items.append(HistoryItem(
            number=number, operation=plan.operation, label=plan.label, summary=plan.summary,
            columns=tuple(plan.affected_columns), settings=settings, counts=counts,
            rows_before=entry.rows_before if entry else 0, rows_after=entry.rows_after if entry else 0,
            columns_before=entry.columns_before if entry else 0, columns_after=entry.columns_after if entry else 0,
        ))
    return tuple(items)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def compare_datasets(
    original: pd.DataFrame,
    current: pd.DataFrame,
    *,
    row_ids: Any = None,
    original_profile: DatasetProfile | None = None,
    current_profile: DatasetProfile | None = None,
    original_quality: QualityReport | None = None,
    current_quality: QualityReport | None = None,
    rules: Sequence[ValidityRule] = (),
) -> DatasetComparison:
    """Compare two tables. Neither is modified or copied whole.

    Profiles and quality reports are computed when not given; pass the
    cached ones to avoid profiling again. Given reports must have been
    made from these tables. Without `row_ids` there is no cell comparison.
    """
    if not isinstance(original, pd.DataFrame) or not isinstance(current, pd.DataFrame):
        raise TypeError("compare_datasets expects two pandas DataFrames.")
    rules = tuple(rules)
    a = original_profile if original_profile is not None else profile_dataset(original)
    b = current_profile if current_profile is not None else profile_dataset(current)
    qa = original_quality if original_quality is not None else assess_quality(original, a, rules)
    qb = current_quality if current_quality is not None else assess_quality(current, b, rules)
    return DatasetComparison(
        overview=_overview(original, current, a, b),
        missing=_missing(a, b),
        duplicates=_duplicates(a, b),
        columns=_columns(original, current),
        quality=_quality(qa, qb),
        cells=compare_cells(original, current, row_ids),
        details=_details(a, b),
        original_profile=a, current_profile=b, original_quality=qa, current_quality=qb,
    )


__all__ = [
    "COMPARE_VERSION", "CellComparison", "CellExample", "ColumnCellChanges", "ColumnChanges", "ColumnDetail",
    "ColumnMissingChange", "ColumnSummary", "ColumnTypeChange", "DatasetComparison", "DuplicateChange",
    "HistoryItem", "IssueChange", "MetricChange", "QualityComparison", "check_lineage", "compare_cells",
    "compare_datasets", "history_summary",
]
