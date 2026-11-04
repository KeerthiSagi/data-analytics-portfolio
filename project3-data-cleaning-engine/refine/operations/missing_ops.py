"""Missing-value operations.

"Missing" means no value (pd.NA) or empty text, as everywhere in REFINE
(refine/missing.py). Text such as "NA" or whitespace-only text is not
missing until "Standardize missing values" turns it into no value.

Every operation follows the framework in base.py: validate the settings,
resolve and freeze every value the change depends on (fill values,
matched text, expected counts), and apply only what was frozen. apply()
checks the recorded counts, so a replay on data that does not match the
recorded step is refused instead of producing something different.

Every column keeps its dtype. In text columns fill values are written as
text; a mean or median is calculated from the values that read as plain
numbers, but the column is never converted. Typed columns (from Convert
data type) are filled with a value of their own type: the fill value is
frozen as canonical text ("42", "2026-01-15") and read back with the
column's dtype, so a whole-number column stays Int64 and a date column
stays a date column.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from refine.conversion import KIND_LABELS, canonical_text, column_kind, parse_fill_value
from refine.errors import CleaningError
from refine.missing import (
    EMPTY_TEXT,
    WHITESPACE,
    exact_mean,
    exact_median,
    format_number,
    is_plain_number,
    looks_like_marker,
    marker_matcher,
    marker_matches,
    missing_mask,
    missing_pct,
    parse_plain_numbers,
    suggest_markers,
)
from refine.notes import plural, quote, verb
from refine.source import TEXT_DTYPE
from refine.operations.base import Operation, OperationResult
from refine.operations.params import (
    check_keys,
    choice,
    column_list,
    expect,
    flag,
    integer,
    invalid,
    number,
    one_column,
    text_columns_only,
    unique_label,
)
from refine.operations.registry import register

MAX_TOKENS = 50
MAX_TOKEN_LENGTH = 100
MAX_NAME_LENGTH = 200
INDICATOR_TRUE, INDICATOR_FALSE = "true", "false"


def no_change(message: str) -> CleaningError:
    return CleaningError("no_change", message)


def _ids(row_ids) -> np.ndarray:
    return np.array(row_ids, dtype=np.int64, copy=True)


def _replace(series: pd.Series, mask: np.ndarray, values) -> pd.Series:
    """A new column: `values` where mask is True (None = no value), the original elsewhere. Same dtype."""
    if column_kind(series.dtype) not in ("text", "other"):
        return _replace_typed(series, mask, values)
    out = series.to_numpy(dtype=object, na_value=None).copy()
    out[mask] = values
    return pd.Series(pd.array(out, dtype=series.dtype), name=series.name)


def _replace_typed(series: pd.Series, mask: np.ndarray, values) -> pd.Series:
    """_replace for typed columns, using pandas so the dtype is kept exactly.

    `values` is None (no value), a typed scalar, or a typed Series aligned
    with `series` (for forward and backward fill).
    """
    if values is None:
        out = series.mask(mask)
    else:
        out = series.mask(mask, values)
    if out.dtype != series.dtype:          # Never silently change a column's type
        raise CleaningError("invalid_params", f"Filling {quote(str(series.name))} would change its type from "
                                              f"{series.dtype} to {out.dtype}.")
    return out.rename(series.name)


def _pct(part: int, whole: int) -> str:
    return f"{missing_pct(part, whole):.1f}%"


def _before_after(data: pd.DataFrame, positions: np.ndarray, columns: list[str],
                  after: dict[str, pd.Series]) -> pd.DataFrame:
    """Sample table: each column's value before and after, for the given rows."""
    taken = set(data.columns)
    parts = {}
    for c in columns:
        parts[c] = data[c].iloc[positions].to_numpy(dtype=object, na_value=None)
        label = unique_label(f"{c} (after)", taken)
        taken.add(label)
        parts[label] = after[c].iloc[positions].to_numpy(dtype=object, na_value=None)
    return pd.DataFrame(parts, index=pd.Index(positions, dtype=np.int64))


# ------------------------------------------------------------------
# A. Standardize missing-value markers
# ------------------------------------------------------------------

class StandardizeMissing(Operation):
    op_id = "standardize_missing"
    label = "Standardize missing values"
    description = ("Turn text you choose, such as NA, null or blank text, into missing values in the columns "
                   "you select. Nothing is treated as missing unless you pick it.")

    def validate(self, params, data):
        check_keys(params, {"columns", "tokens", "case_sensitive", "empty_text", "whitespace_only"}, self.label)
        columns = column_list(params.get("columns"), data, what="columns to check")
        text_columns_only(columns, data, self.label)
        tokens = params.get("tokens", [])
        if isinstance(tokens, str) or not isinstance(tokens, (list, tuple)):
            raise invalid("Give the missing-value markers as a list of text values.")
        if not all(isinstance(t, str) for t in tokens):
            raise invalid("Missing-value markers must be text.")
        cleaned: list[str] = []
        for token in tokens:
            t = token.strip()
            if not t:
                raise invalid("A marker cannot be blank. Use the empty-text or whitespace-only options instead.")
            if len(t) > MAX_TOKEN_LENGTH:
                raise invalid(f"Markers can be at most {MAX_TOKEN_LENGTH} characters.")
            if t not in cleaned:
                cleaned.append(t)
        if len(cleaned) > MAX_TOKENS:
            raise invalid(f"Choose at most {MAX_TOKENS} markers.")
        empty_text = flag(params, "empty_text", False)
        whitespace_only = flag(params, "whitespace_only", False)
        if not (cleaned or empty_text or whitespace_only):
            raise invalid("Choose at least one marker to treat as missing.")
        return {"columns": columns, "tokens": cleaned, "case_sensitive": flag(params, "case_sensitive", True),
                "empty_text": empty_text, "whitespace_only": whitespace_only}

    def resolve(self, params, data):
        matcher = marker_matcher(params["tokens"], params["case_sensitive"], params["empty_text"],
                                 params["whitespace_only"])
        values: dict[str, dict[str, int]] = {}
        matches: dict[str, int] = {}
        by_marker: dict[str, int] = {}
        for c in params["columns"]:
            found = marker_matches(data[c], matcher)
            exact = {v: n for group in found.values() for v, n in group.items()}
            if exact:
                values[c] = dict(sorted(exact.items()))
                matches[c] = sum(exact.values())
            for marker, group in found.items():
                by_marker[marker] = by_marker.get(marker, 0) + sum(group.values())
        total = sum(matches.values())
        if not total:
            raise no_change("No cell in the selected columns matches the chosen markers, so nothing would change.")
        warnings = []
        if not params["case_sensitive"]:
            variants = sorted({v.strip() for group in values.values() for v in group}
                              - set(params["tokens"]) - {""})
            if variants:
                shown = ", ".join(map(quote, variants[:6])) + (" and more" if len(variants) > 6 else "")
                warnings.append(f"Case is ignored, so other spellings also match: {shown}.")
        for c, n in matches.items():
            present = int((~missing_mask(data[c])).sum())
            if present and n * 2 > present:
                warnings.append(
                    f"{plural(n, 'cell')} in {quote(c)} would become missing: more than half of its "
                    f"{present:,} values. Check that these markers really mean \"no value\" in this column."
                )
        resolved = {
            **params,
            "values": values,              # Exact text matched, per column: frozen for replay
            "matches": matches,
            "by_marker": dict(sorted(by_marker.items())),
        }
        return resolved, tuple(matches), tuple(warnings)

    def _masks(self, data, resolved):
        for c, exact in resolved["values"].items():
            if c not in data.columns:
                raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(c)}.")
            mask = data[c].isin(list(exact)).fillna(False).to_numpy(dtype=bool)
            expect(int(mask.sum()), resolved["matches"][c], f"matching cells in {quote(c)}", self.label)
            yield c, mask

    def apply(self, data, row_ids, resolved):
        result = data.copy(deep=True)
        total = 0
        for c, mask in self._masks(data, resolved):
            result[c] = _replace(data[c], mask, None)
            total += int(mask.sum())
        return OperationResult(result, _ids(row_ids),
                               {"cells_changed": total, "columns_changed": len(resolved["values"])})

    def sample(self, data, resolved, limit):
        masks = dict(self._masks(data, resolved))
        any_mask = np.logical_or.reduce(list(masks.values()))
        positions = np.flatnonzero(any_mask)[:limit]
        after = {c: _replace(data[c], m, None) for c, m in masks.items()}
        return _before_after(data, positions, list(masks), after)

    def summarize(self, resolved):
        markers = [quote(t) for t in resolved["tokens"]]
        if resolved["empty_text"]:
            markers.append(EMPTY_TEXT)
        if resolved["whitespace_only"]:
            markers.append(WHITESPACE)
        total = sum(resolved["matches"].values())
        case = "" if resolved["case_sensitive"] else ", ignoring case"
        return (f"Mark {plural(total, 'cell')} as missing in {plural(len(resolved['matches']), 'column')} "
                f"({', '.join(markers)}{case}).")


# ------------------------------------------------------------------
# B. Drop rows with missing values
# ------------------------------------------------------------------

def _row_missing(data: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """Number of missing cells per row among the columns."""
    counts = np.zeros(len(data), dtype=np.int64)
    for c in columns:
        counts += missing_mask(data[c])
    return counts


def _drop_rows_mask(data, columns, how, min_present) -> np.ndarray:
    missing = _row_missing(data, columns)
    if how == "any":
        return missing > 0
    if how == "all":
        return missing == len(columns)
    return (len(columns) - missing) < min_present


def _marker_warning(data, columns) -> str | None:
    found = suggest_markers(data, columns)
    found = [m for m in found if m.marker != EMPTY_TEXT]
    if not found:
        return None
    total = sum(m.count for m in found)
    shown = ", ".join(sorted({quote(m.value) for m in found})[:5])
    return (f"{plural(total, 'cell')} in these columns hold text such as {shown} that is not treated as "
            "missing. Use Standardize missing values first if it means \"no value\".")


class DropMissingRows(Operation):
    op_id = "drop_missing_rows"
    label = "Drop rows with missing values"
    description = ("Remove rows that have missing values in the columns you select: when any is missing, when "
                   "all are missing, or when fewer than a minimum number have a value.")

    def validate(self, params, data):
        check_keys(params, {"columns", "how", "min_present"}, self.label)
        columns = column_list(params.get("columns"), data, what="columns to check")
        how = choice(params, "how", ("any", "all", "min_present"), "any")
        out = {"columns": columns, "how": how, "min_present": None}
        if how == "min_present":
            out["min_present"] = integer(params.get("min_present"), "min_present", 1, len(columns))
        elif params.get("min_present") is not None:
            raise invalid("A minimum number of values is only used with the 'min_present' rule.")
        return out

    def resolve(self, params, data):
        remove = _drop_rows_mask(data, params["columns"], params["how"], params["min_present"])
        n = int(remove.sum())
        rows = len(data)
        if not n:
            raise no_change("No row matches this rule, so no row would be removed.")
        if n == rows:
            raise invalid(f"This would remove all {rows:,} rows. At least one row must remain.")
        warnings = [f"{plural(n, 'row')} of {rows:,} ({_pct(n, rows)}) would be removed from the working "
                    "table. The uploaded file is not changed, and undo or reset brings the rows back."]
        if n * 2 >= rows:
            warnings.append("This removes at least half of the rows.")
        marker = _marker_warning(data, params["columns"])
        if marker:
            warnings.append(marker)
        return {**params, "rows_removed": n, "rows": rows}, tuple(params["columns"]), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        absent = [c for c in resolved["columns"] if c not in data.columns]
        if absent:
            raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(absent[0])}.")
        remove = _drop_rows_mask(data, resolved["columns"], resolved["how"], resolved["min_present"])
        expect(int(remove.sum()), resolved["rows_removed"], "rows to remove", self.label)
        keep = ~remove
        result = data.loc[keep].reset_index(drop=True).copy(deep=True)
        return OperationResult(result, _ids(np.asarray(row_ids)[keep]),
                               {"rows_removed": int(remove.sum()), "rows_kept": int(keep.sum())})

    def sample(self, data, resolved, limit):
        remove = _drop_rows_mask(data, resolved["columns"], resolved["how"], resolved["min_present"])
        positions = np.flatnonzero(remove)[:limit]
        cols = list(resolved["columns"]) + [c for c in data.columns if c not in resolved["columns"]]
        return data.iloc[positions][cols].copy()

    def summarize(self, resolved):
        cols = resolved["columns"]
        rule = {
            "any": "any of",
            "all": "all of",
        }.get(resolved["how"])
        n = resolved["rows_removed"]
        if rule:
            what = (f"missing a value in {quote(cols[0])}" if len(cols) == 1
                    else f"missing values in {rule} {', '.join(map(quote, cols))}")
        else:
            what = (f"fewer than {resolved['min_present']:,} values in "
                    f"{', '.join(map(quote, cols))}")
        return f"Remove {plural(n, 'row')} with {what}."


# ------------------------------------------------------------------
# C. Drop columns by missing share
# ------------------------------------------------------------------

class DropMissingColumns(Operation):
    op_id = "drop_missing_columns"
    label = "Drop mostly-missing columns"
    description = "Remove columns where at least the share of missing values you set is missing."

    def validate(self, params, data):
        check_keys(params, {"threshold", "columns"}, self.label)
        threshold = number(params.get("threshold"), "threshold", 0.0, 100.0)
        if threshold <= 0:
            raise invalid("The threshold must be more than 0%; 0% would drop every column.")
        scope = params.get("columns")
        columns = None if scope is None else column_list(scope, data, what="columns to consider")
        return {"threshold": threshold, "columns": columns}

    def resolve(self, params, data):
        rows = len(data)
        scope = params["columns"] if params["columns"] is not None else list(data.columns)
        missing = {c: int(missing_mask(data[c]).sum()) for c in scope}
        threshold = params["threshold"]
        chosen = [c for c in scope if rows and 100.0 * missing[c] / rows >= threshold]
        if not chosen:
            if not rows:
                raise no_change("The table has no rows, so no column has missing values.")
            top = max(scope, key=lambda c: missing[c])
            raise no_change(f"No column has {threshold:g}% or more missing values. The highest is "
                            f"{quote(top)} at {_pct(missing[top], rows)}.")
        if len(chosen) >= data.shape[1]:
            raise invalid(f"Every column has {threshold:g}% or more missing values. At least one column must "
                          "remain; raise the threshold or limit the columns considered.")
        values = sum(rows - missing[c] for c in chosen)
        warnings = []
        if values:
            warnings.append(f"{plural(values, 'value')} that {verb(values, 'is', 'are')} not missing will be "
                            "removed with these columns. Undo or reset brings them back.")
        if len(chosen) * 2 >= data.shape[1]:
            warnings.append(f"This removes {len(chosen):,} of {data.shape[1]:,} columns, at least half the table.")
        resolved = {
            "threshold": threshold, "scope": scope, "rows": rows,
            "columns": chosen,
            "positions": [data.columns.get_loc(c) + 1 for c in chosen],
            "missing": {c: missing[c] for c in chosen},
            "missing_pct": {c: round(missing_pct(missing[c], rows), 4) for c in chosen},
        }
        return resolved, tuple(chosen), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        columns = list(resolved["columns"])
        absent = [c for c in columns if c not in data.columns]
        if absent:
            raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(absent[0])}.")
        values = 0
        for c in columns:
            n = int(missing_mask(data[c]).sum())
            expect(n, resolved["missing"][c], f"missing cells in {quote(c)}", self.label)
            values += len(data) - n
        result = data.drop(columns=columns).copy(deep=True)
        return OperationResult(result, _ids(row_ids), {
            "columns_removed": len(columns), "cells_removed": len(columns) * len(data), "values_removed": values,
        })

    def sample(self, data, resolved, limit):
        return data[list(resolved["columns"])].head(limit).copy()

    def summarize(self, resolved):
        cols = resolved["columns"]
        return (f"Drop {plural(len(cols), 'column')} with {resolved['threshold']:g}% or more missing values: "
                f"{', '.join(map(quote, cols))}.")


# ------------------------------------------------------------------
# D. Fill missing values
# ------------------------------------------------------------------

FILL_METHODS = ("constant", "mean", "median", "mode", "ffill", "bfill")
METHOD_LABELS = {
    "constant": "a fixed value", "mean": "the mean", "median": "the median", "mode": "the most frequent value",
    "ffill": "the previous value (forward fill)", "bfill": "the next value (backward fill)",
}
ORDER_WARNING = ("{name} copies values from neighbouring rows, so the result depends on the current row order. "
                 "Sort or remove rows first if the order matters.")


def _directional(mask: np.ndarray, forward: bool) -> np.ndarray:
    """For each row, the position of the value to copy (-1: none available)."""
    n = len(mask)
    idx = np.where(~mask, np.arange(n), -1 if forward else n)
    if forward:
        source = np.maximum.accumulate(idx) if n else idx
    else:
        source = np.minimum.accumulate(idx[::-1])[::-1] if n else idx
        source = np.where(source == n, -1, source)
    return np.where(mask, source, -1)


def _typed_fill_value(series: pd.Series, text: str):
    """The fill value for the column's dtype (text columns: the text itself)."""
    value, problem = parse_fill_value(text, series.dtype)
    if problem:
        raise invalid(f"{quote(str(series.name))} is stored as {series.dtype}: {problem}.")
    return value


def _value_fill(series: pd.Series, value: str) -> tuple[pd.Series, np.ndarray]:
    mask = missing_mask(series)
    return _replace(series, mask, _typed_fill_value(series, value)), mask


def _directional_fill(series: pd.Series, forward: bool) -> tuple[pd.Series, np.ndarray, int]:
    mask = missing_mask(series)
    source = _directional(mask, forward)
    fill = source >= 0
    if column_kind(series.dtype) not in ("text", "other"):
        donors = series.iloc[np.where(fill, source, 0)].reset_index(drop=True)
        out = _replace_typed(series.reset_index(drop=True), fill, donors)
        return out, fill, int((mask & ~fill).sum())
    values = series.to_numpy(dtype=object, na_value=None)
    out = _replace(series, fill, values[source[fill]])
    return out, fill, int((mask & ~fill).sum())


def _mode(series: pd.Series) -> tuple[str, int, list[str]]:
    """Most frequent present value; ties go to the value that appears first in the table."""
    present = series[~missing_mask(series)]
    codes, uniques = pd.factorize(present, use_na_sentinel=True)
    counts = np.bincount(codes, minlength=len(uniques))
    top = int(counts.max())
    kind = column_kind(series.dtype)
    as_text = str if kind in ("text", "other") else (lambda v: canonical_text(v, kind))
    tied = [as_text(uniques[i]) for i in np.flatnonzero(counts == top)]      # In order of first appearance
    return tied[0], top, tied


class FillMissing(Operation):
    op_id = "fill_missing"
    label = "Fill missing values"
    description = ("Fill missing cells with a fixed value, the mean, median or most frequent value, or the "
                   "previous or next value. Other cells are not changed and every column keeps its type.")

    def validate(self, params, data):
        check_keys(params, {"columns", "method", "value", "decimals", "non_numeric"}, self.label)
        columns = column_list(params.get("columns"), data, what="columns to fill")
        kinds = {c: column_kind(data[c].dtype) for c in columns}
        unsupported = [c for c, k in kinds.items() if k == "other"]
        if unsupported:
            raise invalid(f"{self.label} works on text and typed columns; {quote(unsupported[0])} is stored as "
                          f"{data[unsupported[0]].dtype}.")
        method = choice(params, "method", FILL_METHODS)
        if method in ("mean", "median"):
            other = [c for c, k in kinds.items() if k not in ("text", "integer", "decimal")]
            if other:
                raise invalid(f"The {method} needs numbers, but {quote(other[0])} is stored as "
                              f"{KIND_LABELS[kinds[other[0]]].lower()} ({data[other[0]].dtype}). Choose another "
                              "method.")
        out: dict[str, Any] = {"columns": columns, "method": method}
        if method == "constant":
            value = params.get("value")
            if not isinstance(value, str) or value == "":
                raise invalid("Enter the value to fill with. Empty text would still be missing.")
            for c, k in kinds.items():
                if k != "text":
                    _typed_fill_value(data[c], value)
            out["value"] = value
        elif params.get("value") is not None:
            raise invalid("A fill value is only used with the 'constant' method.")
        if method in ("mean", "median"):
            decimals = params.get("decimals")
            out["decimals"] = None if decimals is None else integer(decimals, "decimals", 0, 10)
            out["non_numeric"] = choice(params, "non_numeric", ("reject", "exclude"), "reject")
        else:
            for key in ("decimals", "non_numeric"):
                if params.get(key) is not None:
                    raise invalid(f"The setting '{key}' is only used with the mean and median.")
        return out

    def _numeric_fill(self, c, series, params, warnings) -> str:
        method = params["method"]
        parsed = parse_plain_numbers(series)
        if parsed.plain == 0:
            if parsed.other == 0:
                raise invalid(f"{quote(c)} has no values, so there is nothing to calculate the {method} from.")
            raise invalid(
                f"The {method} needs numbers, but no value in {quote(c)} is a plain number (for example "
                f"{', '.join(map(quote, parsed.other_examples[:3]))}). Choose another method."
            )
        if parsed.other:
            examples = ", ".join(map(quote, parsed.other_examples))
            if params["non_numeric"] == "reject":
                raise invalid(
                    f"The {method} is calculated from values that are plain numbers, and "
                    f"{plural(parsed.other, 'value')} in {quote(c)} {verb(parsed.other, 'is', 'are')} not "
                    f"(for example {examples}). Standardize missing-value markers first, or choose to "
                    "calculate from the plain numbers only. The column is never converted to numbers."
                )
            warnings.append(
                f"{quote(c)}: the {method} uses {plural(parsed.plain, 'plain number')} and ignores "
                f"{plural(parsed.other, 'value')} that {verb(parsed.other, 'is', 'are')} not plain numbers "
                f"({examples}). Those values are left as they are."
            )
        stat = exact_mean(parsed.values) if method == "mean" else exact_median(parsed.values)
        text = format_number(stat, params["decimals"])
        if column_kind(series.dtype) == "integer" and not text.lstrip("+-").isdigit():
            raise invalid(f"{quote(c)} holds whole numbers ({series.dtype}), but its {method} is {text}. Round to "
                          "0 decimal places to fill it with a whole number.")
        if parsed.all_integral and not text.lstrip("+-").isdigit():
            warnings.append(f"Every number in {quote(c)} is a whole number, but the fill value {quote(text)} "
                            "is not. Round to 0 decimals if the column holds counts.")
        return text

    def resolve(self, params, data):
        method = params["method"]
        rows = len(data)
        warnings: list[str] = []
        fills: dict[str, str] = {}
        cells: dict[str, int] = {}
        left: dict[str, int] = {}
        for c in params["columns"]:
            s = data[c]
            n_missing = int(missing_mask(s).sum())
            if method in ("ffill", "bfill"):
                _, filled, unfilled = _directional_fill(s, method == "ffill")
                cells[c] = int(filled.sum())
                if unfilled:
                    left[c] = unfilled
                continue
            cells[c] = n_missing
            if not n_missing:
                continue
            if method == "constant":
                fills[c] = params["value"]
            elif method in ("mean", "median"):
                fills[c] = self._numeric_fill(c, s, params, warnings)
            else:
                if n_missing == rows:
                    raise invalid(f"{quote(c)} has no values, so it has no most frequent value.")
                value, top, tied = _mode(s)
                fills[c] = value
                if len(tied) > 1:
                    shown = ", ".join(map(quote, tied[:5])) + (" and more" if len(tied) > 5 else "")
                    warnings.append(f"{quote(c)}: {len(tied):,} values are equally frequent ({top:,} times each: "
                                    f"{shown}). Using {quote(value)}, the first of them in the table.")
                if looks_like_marker(value):
                    warnings.append(f"{quote(c)}: the most frequent value is {quote(value)}, which looks like a "
                                    "missing-value marker. Standardize missing values first if it means "
                                    "\"no value\".")
        total = sum(cells.values())
        if not total:
            if method in ("ffill", "bfill") and left:
                raise no_change("The missing cells have no value to copy from in that direction, so nothing "
                                "would change.")
            raise no_change("The selected columns have no missing values, so nothing would change.")
        if method == "constant":
            value = params["value"]
            if looks_like_marker(value):
                warnings.append(f"The fill value {quote(value)} looks like a missing-value marker.")
            for c in fills:
                parsed = parse_plain_numbers(data[c])
                if parsed.plain and parsed.plain >= 0.9 * (parsed.plain + parsed.other) and not is_plain_number(value):
                    warnings.append(f"{quote(c)} holds mostly numbers, but {quote(value)} is not a number.")
        if method in ("ffill", "bfill"):
            name = "Forward fill" if method == "ffill" else "Backward fill"
            warnings.insert(0, ORDER_WARNING.format(name=name))
            for c, n in left.items():
                where = "before the first value" if method == "ffill" else "after the last value"
                warnings.append(f"{quote(c)}: {plural(n, 'missing cell')} {where} cannot be filled and "
                                f"{verb(n, 'stays', 'stay')} missing.")
        resolved = {**params, "fills": fills, "cells": cells, "unfilled": left, "rows": rows}
        changed = tuple(c for c in params["columns"] if cells[c])
        return resolved, changed, tuple(warnings)

    def _filled(self, data, resolved):
        method = resolved["method"]
        for c in resolved["columns"]:
            if c not in data.columns:
                raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(c)}.")
            expected = resolved["cells"][c]
            if not expected:
                continue
            if method in ("ffill", "bfill"):
                series, mask, _ = _directional_fill(data[c], method == "ffill")
            else:
                series, mask = _value_fill(data[c], resolved["fills"][c])
            expect(int(mask.sum()), expected, f"cells to fill in {quote(c)}", self.label)
            yield c, series, mask

    def apply(self, data, row_ids, resolved):
        result = data.copy(deep=True)
        total = columns = 0
        for c, series, mask in self._filled(data, resolved):
            result[c] = series
            total += int(mask.sum())
            columns += 1
        return OperationResult(result, _ids(row_ids), {
            "cells_filled": total, "columns_changed": columns,
            "cells_left_missing": sum(resolved.get("unfilled", {}).values()),
        })

    def sample(self, data, resolved, limit):
        parts = list(self._filled(data, resolved))
        any_mask = np.logical_or.reduce([m for _, _, m in parts])
        positions = np.flatnonzero(any_mask)[:limit]
        return _before_after(data, positions, [c for c, _, _ in parts], {c: s for c, s, _ in parts})

    def summarize(self, resolved):
        total = sum(resolved["cells"].values())
        cols = [c for c in resolved["columns"] if resolved["cells"][c]]
        method = resolved["method"]
        where = f"{quote(cols[0])}" if len(cols) == 1 else f"{plural(len(cols), 'column')}"
        if method in ("constant", "mean", "median", "mode"):
            values = sorted(set(resolved["fills"].values()))
            shown = (f" ({quote(values[0])})" if len(values) == 1
                     else " (" + "; ".join(f"{quote(c)}: {quote(v)}" for c, v in resolved["fills"].items()) + ")")
            if method == "constant":
                return f"Fill {plural(total, 'missing cell')} in {where} with {quote(resolved['value'])}."
            return f"Fill {plural(total, 'missing cell')} in {where} with {METHOD_LABELS[method]}{shown}."
        return f"Fill {plural(total, 'missing cell')} in {where} with {METHOD_LABELS[method]}."


# ------------------------------------------------------------------
# E. Missing-value indicator column
# ------------------------------------------------------------------

def indicator_name(column: str, existing) -> str:
    """'<column>_missing', or '<column>_missing_2', '_3', ... if taken."""
    taken = set(existing)
    base = f"{column}_missing"
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


class AddMissingIndicator(Operation):
    op_id = "add_missing_indicator"
    label = "Add missing-value indicator"
    description = ("Add a column next to the one you choose that says, for each row, whether that cell is "
                   f"missing ('{INDICATOR_TRUE}') or not ('{INDICATOR_FALSE}'). Add it before filling, so it "
                   "records which values were originally missing.")

    def validate(self, params, data):
        check_keys(params, {"column", "name"}, self.label)
        column = one_column(params.get("column"), data)
        name = params.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise invalid("The new column's name must be text.")
            if name == "":
                name = None
            elif name != name.strip():
                raise invalid("The new column's name cannot start or end with spaces.")
            elif len(name) > MAX_NAME_LENGTH:
                raise invalid(f"Column names can be at most {MAX_NAME_LENGTH} characters.")
            elif name in data.columns:
                raise invalid(f"The table already has a column named {quote(name)}. Choose another name.")
        return {"column": column, "name": name}

    def resolve(self, params, data):
        column = params["column"]
        name = params["name"] or indicator_name(column, data.columns)
        missing = int(missing_mask(data[column]).sum())
        rows = len(data)
        warnings = []
        if rows and not missing:
            warnings.append(f"{quote(column)} has no missing values, so every row would be '{INDICATOR_FALSE}'.")
        elif rows and missing == rows:
            warnings.append(f"Every value in {quote(column)} is missing, so every row would be '{INDICATOR_TRUE}'.")
        resolved = {
            "column": column, "name": name, "position": data.columns.get_loc(column) + 2,
            "missing": missing, "rows": rows, "true_text": INDICATOR_TRUE, "false_text": INDICATOR_FALSE,
        }
        return resolved, (column, name), tuple(warnings)

    def _indicator(self, data, resolved) -> pd.Series:
        column, name = resolved["column"], resolved["name"]
        if column not in data.columns:
            raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(column)}.")
        if name in data.columns:
            raise CleaningError("replay_mismatch", f"{self.label}: the table already has a column {quote(name)}.")
        mask = missing_mask(data[column])
        expect(int(mask.sum()), resolved["missing"], f"missing cells in {quote(column)}", self.label)
        values = np.where(mask, resolved["true_text"], resolved["false_text"]).astype(object)
        return pd.Series(pd.array(values, dtype=TEXT_DTYPE), name=name)

    def apply(self, data, row_ids, resolved):
        indicator = self._indicator(data, resolved)
        result = data.copy(deep=True)
        result.insert(int(resolved["position"]) - 1, resolved["name"], indicator.array)
        return OperationResult(result, _ids(row_ids), {"columns_added": 1, "rows_flagged": resolved["missing"]})

    def sample(self, data, resolved, limit):
        indicator = self._indicator(data, resolved)
        flagged = indicator.to_numpy(dtype=object) == resolved["true_text"]
        half = max(1, limit // 2)
        positions = np.sort(np.concatenate([np.flatnonzero(flagged)[:half], np.flatnonzero(~flagged)[:half]]))
        positions = positions[:limit]
        out = data[[resolved["column"]]].iloc[positions].copy()
        out[resolved["name"]] = indicator.iloc[positions].to_numpy(dtype=object)
        return out

    def summarize(self, resolved):
        return (f"Add column {quote(resolved['name'])} after {quote(resolved['column'])}: "
                f"'{resolved['true_text']}' for the {plural(resolved['missing'], 'missing value')}, "
                f"'{resolved['false_text']}' otherwise.")


STANDARDIZE_MISSING = register(StandardizeMissing())
DROP_MISSING_ROWS = register(DropMissingRows())
DROP_MISSING_COLUMNS = register(DropMissingColumns())
FILL_MISSING = register(FillMissing())
ADD_MISSING_INDICATOR = register(AddMissingIndicator())
