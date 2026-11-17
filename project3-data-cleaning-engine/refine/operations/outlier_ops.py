"""Treat outliers: detect outliers in one numeric column and treat them.

Settings (all frozen in the recipe):

    column      one whole-number or decimal column
    method      iqr | zscore | percentile (refine/outliers.py)
    multiplier / threshold / lower_percentile, upper_percentile
                the method's parameters (defaults 1.5 / 3.0 / 1, 99)
    treatment   cap            values outside the boundaries become the boundary
                set_missing    outliers become missing
                remove_rows    rows holding an outlier are removed
                replace_value  outliers become a number you enter (`value`)

Replay contract: resolve() calculates the boundaries once, from the table
the preview was made for, and freezes them in the plan together with the
column's dtype and the numbers of values below and above them. apply()
never recalculates the boundaries. It finds the values outside the frozen
boundaries and refuses data that gives different counts or a different
dtype, so a replay changes exactly the cells (or rows) the preview showed.

Missing cells are never outliers and stay missing. Rows are selected by
position. Row order and row identities are kept, except for removed rows.
Other columns are not touched.

Dtypes: set_missing and replace_value keep the column's dtype (a numpy
int64 column, which cannot hold a missing value, becomes Int64 for
set_missing). cap keeps it too, unless a whole-number column must be
capped at a fractional boundary: it then becomes Float64 rather than
rounding the boundary, and the preview warns. A fractional replacement
value for a whole-number column is refused, never rounded.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from refine.conversion import INT64_MAX, INT64_MIN, column_kind, dtype_name
from refine.errors import CleaningError
from refine.missing import is_plain_number
from refine.notes import plural, quote
from refine.operations.base import Operation, OperationResult
from refine.operations.missing_ops import _ids, _pct, no_change
from refine.operations.params import check_keys, choice, expect, invalid, one_column, unique_label
from refine.operations.registry import register
from refine.outliers import (
    METHOD_KEYS,
    METHOD_LABELS,
    METHODS,
    SETTING_KEYS,
    check_settings,
    describe_settings,
    detect_outliers,
    fmt,
    is_eligible,
    numeric_values,
    outside,
)

TREATMENTS = ("cap", "set_missing", "remove_rows", "replace_value")
TREATMENT_LABELS = {
    "cap": "Cap at the boundaries",
    "set_missing": "Replace with missing values",
    "remove_rows": "Remove rows",
    "replace_value": "Replace with a fixed value",
}
FLOAT_EXACT_INT = 2 ** 53          # Larger integers are not all exactly representable as Float64
LARGE_SHARE_PCT = 10.0
OUTCOME_LABEL, REMOVED = "Outcome", "Removed"
_INTEGER_TEXT = frozenset("0123456789+-")


def replacement_value(value: Any, kind: str) -> int | float:
    """The replacement as a number of the column's kind, or CleaningError("invalid_params")."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise invalid("Enter the number to replace outliers with.")
    if isinstance(value, bool):
        raise invalid("The replacement value must be a number, not true or false.")
    if isinstance(value, str):
        text = value.strip()
        if not is_plain_number(text):
            raise invalid(f"{quote(value)} is not a number. Enter a plain number such as 0, 42 or -3.5.")
        number: int | float = int(text) if set(text) <= _INTEGER_TEXT else float(text)
    elif isinstance(value, (int, float)):
        number = value
    else:
        raise invalid("The replacement value must be a number.")
    if isinstance(number, float) and not math.isfinite(number):
        raise invalid("The replacement value must be a finite number (not NaN or infinity).")
    if kind == "integer":
        if isinstance(number, float):
            if not number.is_integer():
                raise invalid(f"The column holds whole numbers, so {fmt(number)} cannot be stored without "
                              "rounding. Enter a whole number, or convert the column to decimal numbers first.")
            number = int(number)
        if not INT64_MIN <= number <= INT64_MAX:
            raise invalid("The replacement value is outside the range of whole numbers REFINE can store.")
        return int(number)
    number = float(number)
    if not math.isfinite(number):
        raise invalid("The replacement value is too large to store as a decimal number.")
    return number


def _result_dtype(series: pd.Series, treatment: str, lower: float, upper: float, below: int, above: int) -> str:
    """The dtype name the treated column will have (frozen, checked on replay)."""
    dtype = series.dtype
    kind = column_kind(dtype)
    if treatment == "set_missing" and isinstance(dtype, np.dtype) and kind == "integer":
        return "Int64"                                 # numpy int64 cannot hold a missing value
    if treatment == "cap" and kind == "integer":
        used = ([lower] if below else []) + ([upper] if above else [])
        if any(not float(b).is_integer() for b in used):
            return "Float64"
    return dtype_name(dtype)


class TreatOutliers(Operation):
    op_id = "treat_outliers"
    label = "Treat outliers"
    description = ("Find values outside boundaries calculated with the IQR, z-score or percentile method in one "
                   "numeric column, then cap them, replace them with missing values or a number, or remove "
                   "their rows. Missing cells are never outliers.")

    # ---- validate ------------------------------------------------------

    def validate(self, params, data):
        check_keys(params, {"column", "method", "treatment", "value"} | SETTING_KEYS, self.label)
        column = one_column(params.get("column"), data)
        dtype = data[column].dtype
        if not is_eligible(dtype):
            hint = (" True / false columns have no outliers." if column_kind(dtype) == "boolean" else
                    " Convert it to whole or decimal numbers on the Data types tab first.")
            raise invalid(f"Outlier detection works on whole-number and decimal columns; {quote(column)} is stored "
                          f"as {dtype}.{hint}")
        method = choice(params, "method", METHODS, "iqr")
        settings = check_settings(method, params)
        treatment = choice(params, "treatment", TREATMENTS)
        out: dict[str, Any] = {"column": column, "method": method, **settings, "treatment": treatment,
                               "value": None}
        if treatment == "replace_value":
            out["value"] = replacement_value(params.get("value"), column_kind(dtype))
        elif params.get("value") is not None:
            raise invalid("A replacement value is only used with 'Replace with a fixed value'.")
        return out

    # ---- resolve -------------------------------------------------------

    def resolve(self, params, data):
        column, method, treatment = params["column"], params["method"], params["treatment"]
        settings = {k: params[k] for k in METHOD_KEYS[method]}
        series = data[column]
        found = detect_outliers(series, method, settings)
        if not found.ok:
            raise invalid(found.reason)
        lower, upper = found.lower, found.upper
        n_below, n_above, n = found.n_below, found.n_above, found.outliers
        how = describe_settings(method, settings)
        if not n:
            note = f" {found.notes[0]}" if found.notes else ""
            raise no_change(f"No value in {quote(column)} is outside the boundaries {fmt(lower)} to {fmt(upper)} "
                            f"({how}), so nothing would change.{note}")
        rows = len(data)
        if treatment == "remove_rows" and n == rows:
            raise invalid(f"Every row holds an outlier in {quote(column)}, so this would remove all {rows:,} rows. "
                          "At least one row must remain; choose other settings or another treatment.")
        result_dtype = _result_dtype(series, treatment, lower, upper, n_below, n_above)
        source_dtype = dtype_name(series.dtype)
        if result_dtype == "Float64" and source_dtype != "Float64":
            values, _ = numeric_values(series)
            if np.nanmax(np.abs(values)) > FLOAT_EXACT_INT:
                raise invalid(f"Capping {quote(column)} at a fractional boundary would store it as Float64, which "
                              "cannot hold its largest whole numbers exactly. Choose another treatment.")

        warnings: list[str] = list(found.notes)
        if found.pct >= LARGE_SHARE_PCT:
            warnings.append(f"{found.pct:.1f}% of the values in {quote(column)} are classified as outliers. Check "
                            "that the method and its settings suit this column.")
        if treatment == "cap":
            parts = [f"{plural(n_below, 'value')} below {fmt(lower)} would become {fmt(lower)}" if n_below else "",
                     f"{plural(n_above, 'value')} above {fmt(upper)} would become {fmt(upper)}" if n_above else ""]
            warnings.append("; ".join(p for p in parts if p) + ". The original values are removed from the "
                            "working table; undo or reset brings them back.")
        elif treatment == "set_missing":
            warnings.append(f"{plural(n, 'value')} would become missing. The original values are removed from the "
                            "working table; undo or reset brings them back.")
        elif treatment == "replace_value":
            value = params["value"]
            warnings.append(f"{plural(n, 'value')} would become {fmt(value)}. The original values are removed from "
                            "the working table; undo or reset brings them back.")
            if value < lower or value > upper:
                warnings.append(f"The replacement value {fmt(value)} is itself outside the boundaries "
                                f"{fmt(lower)} to {fmt(upper)}.")
        else:
            warnings.append(f"{plural(n, 'row')} of {rows:,} ({_pct(n, rows)}) would be removed from the working "
                            "table, with all their columns. Rows where the column is missing are kept. Undo or "
                            "reset brings the rows back.")
        if result_dtype != source_dtype:
            why = ("it cannot hold missing values" if treatment == "set_missing" else
                   "a boundary is not a whole number, and capping never rounds it")
            warnings.append(f"{quote(column)} would change from {source_dtype} to {result_dtype}, because {why}.")

        resolved = {
            **params,
            "lower_bound": lower, "upper_bound": upper, "statistics": found.statistics,
            "rows": rows, "evaluated": found.evaluated, "missing": found.missing,
            "below": n_below, "above": n_above, "outliers": n,
            "source_dtype": source_dtype, "result_dtype": result_dtype,
        }
        return resolved, (column,), tuple(warnings)

    # ---- apply ---------------------------------------------------------

    def _masks(self, data, resolved) -> tuple[pd.Series, np.ndarray, np.ndarray]:
        """The column and its (below, above) masks for the frozen boundaries, checked against the plan."""
        column = resolved["column"]
        if column not in data.columns:
            raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(column)}.")
        series = data[column]
        if dtype_name(series.dtype) != resolved["source_dtype"]:
            raise CleaningError("replay_mismatch", f"{self.label}: {quote(column)} is stored as "
                                                   f"{dtype_name(series.dtype)}, but the recorded step expects "
                                                   f"{resolved['source_dtype']}.")
        values, missing = numeric_values(series)
        if not np.isfinite(values[~missing]).all():
            raise CleaningError("replay_mismatch", f"{self.label}: {quote(column)} holds non-finite values.")
        below, above = outside(values, float(resolved["lower_bound"]), float(resolved["upper_bound"]))
        expect(int(below.sum()), resolved["below"], f"values below {fmt(resolved['lower_bound'])} in {quote(column)}",
               self.label)
        expect(int(above.sum()), resolved["above"], f"values above {fmt(resolved['upper_bound'])} in {quote(column)}",
               self.label)
        return series, below, above

    def _treated(self, series: pd.Series, below: np.ndarray, above: np.ndarray, resolved) -> pd.Series:
        treatment, target = resolved["treatment"], resolved["result_dtype"]
        out = series.reset_index(drop=True)
        if dtype_name(out.dtype) != target:
            out = out.astype(target)
        if treatment == "cap":
            low, high = float(resolved["lower_bound"]), float(resolved["upper_bound"])
            if column_kind(out.dtype) == "integer":
                low, high = int(low) if below.any() else 0, int(high) if above.any() else 0
            out = out.mask(below, low).mask(above, high)
        elif treatment == "set_missing":
            out = out.mask(below | above)
        else:
            out = out.mask(below | above, resolved["value"])
        if dtype_name(out.dtype) != target:            # Never silently change a column's type
            raise CleaningError("invalid_params", f"Treating {quote(str(series.name))} would change its type from "
                                                  f"{series.dtype} to {out.dtype}.")
        return out.rename(series.name)

    def apply(self, data, row_ids, resolved):
        series, below, above = self._masks(data, resolved)
        hit = below | above
        n = int(hit.sum())
        if resolved["treatment"] == "remove_rows":
            keep = np.flatnonzero(~hit)
            result = data.iloc[keep].reset_index(drop=True).copy(deep=True)
            return OperationResult(result, _ids(np.asarray(row_ids)[keep]),
                                   {"outliers": n, "rows_removed": n, "rows_kept": len(keep), "cells_changed": 0})
        result = data.copy(deep=True)
        result[resolved["column"]] = self._treated(series, below, above, resolved).array
        return OperationResult(result, _ids(row_ids), {"outliers": n, "cells_changed": n, "rows_removed": 0})

    # ---- sample / summary ----------------------------------------------

    def sample(self, data, resolved, limit):
        """Outlier rows in table order (low and high ones both shown), before and after."""
        series, below, above = self._masks(data, resolved)
        half = max(1, limit // 2)
        low, high = np.flatnonzero(below), np.flatnonzero(above)
        first = np.concatenate([low[:half], high[:half]])
        rest = np.setdiff1d(np.concatenate([low, high]), first)[: max(0, limit - len(first))]
        positions = np.sort(np.concatenate([first, rest]))[:limit].astype(np.int64)
        column = resolved["column"]
        if resolved["treatment"] == "remove_rows":
            cols = [column] + [c for c in data.columns if c != column]
            out = data.iloc[positions][cols].copy()
            out.insert(0, unique_label(OUTCOME_LABEL, set(data.columns)), REMOVED)
            out.index = pd.Index(positions, dtype=np.int64)
            return out
        after = self._treated(series, below, above, resolved)
        label = unique_label(f"{column} (after)", set(data.columns))
        return pd.DataFrame({
            column: series.iloc[positions].to_numpy(dtype=object, na_value=None),
            label: after.iloc[positions].to_numpy(dtype=object, na_value=None),
        }, index=pd.Index(positions, dtype=np.int64))

    def summarize(self, resolved):
        column, n = quote(resolved["column"]), resolved["outliers"]
        settings = {k: resolved[k] for k in METHOD_KEYS[resolved["method"]]}
        how = describe_settings(resolved["method"], settings)
        bounds = f"outside {fmt(resolved['lower_bound'])} to {fmt(resolved['upper_bound'])} ({how})"
        treatment = resolved["treatment"]
        if treatment == "cap":
            return f"Cap {plural(n, 'outlier')} in {column} {bounds} at the boundaries."
        if treatment == "set_missing":
            return f"Replace {plural(n, 'outlier')} in {column} {bounds} with missing values."
        if treatment == "replace_value":
            return f"Replace {plural(n, 'outlier')} in {column} {bounds} with {fmt(resolved['value'])}."
        return f"Remove {plural(n, 'row')} with an outlier in {column} {bounds}."


TREAT_OUTLIERS = register(TreatOutliers())

__all__ = ["METHOD_LABELS", "TREATMENTS", "TREATMENT_LABELS", "TREAT_OUTLIERS", "TreatOutliers", "replacement_value"]
