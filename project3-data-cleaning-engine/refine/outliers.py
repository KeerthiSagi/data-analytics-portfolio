"""Outlier detection on numeric columns (read-only).

Three methods, each giving a lower and an upper boundary. A value is an
outlier when it is strictly outside them:

    value < lower  or  value > upper

so a value equal to a boundary is never an outlier.

* IQR:        Q1 - k * IQR  and  Q3 + k * IQR, with IQR = Q3 - Q1 (k = 1.5).
* Z-score:    mean - t * sd  and  mean + t * sd (t = 3.0), sd with ddof=0
              (population standard deviation). This is |z| > t written as
              boundaries, so preview, apply and replay all use one rule.
* Percentile: the values at the lower and upper percentiles (1 and 99).

Quartiles and percentiles use linear interpolation between the two
nearest sorted values (numpy's "linear" method, the default of both
numpy and pandas), computed with numpy so the result does not depend on
the pandas version.

Only whole-number and decimal columns are eligible (Int64, Float64 and
plain numpy integer and float columns). Boolean columns are not, and
text columns are never parsed here: convert them on the Data types tab
first. Missing cells are never outliers and are not part of the
statistics. Non-finite values (NaN that is not a missing value, inf) are
refused rather than dropped, because REFINE's typed columns never hold
them (Convert data type refuses inf and NaN).

Nothing here changes data. Rows are identified by position, never by
index label, so duplicate index labels cannot select the wrong rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from refine.conversion import column_kind
from refine.errors import CleaningError

METHODS = ("iqr", "zscore", "percentile")
METHOD_LABELS = {"iqr": "IQR", "zscore": "Z-score", "percentile": "Percentile"}
METHOD_KEYS = {
    "iqr": ("multiplier",),
    "zscore": ("threshold",),
    "percentile": ("lower_percentile", "upper_percentile"),
}
SETTING_KEYS = frozenset(k for keys in METHOD_KEYS.values() for k in keys)
DEFAULTS = {"multiplier": 1.5, "threshold": 3.0, "lower_percentile": 1.0, "upper_percentile": 99.0}
MAX_MULTIPLIER = 100.0
MAX_THRESHOLD = 100.0
QUANTILE_METHOD = "linear"

# Fewer present values than this give no meaningful quartiles, spread or
# percentiles, so detection is refused instead of reporting a number.
MIN_VALUES = 3

ELIGIBLE_KINDS = ("integer", "decimal")


def _invalid(message: str) -> CleaningError:
    return CleaningError("invalid_params", message)


def is_eligible(dtype) -> bool:
    """Whole-number and decimal columns. Booleans, text and dates are not eligible."""
    return column_kind(dtype) in ELIGIBLE_KINDS


def eligible_columns(data: pd.DataFrame) -> list[str]:
    return [c for c in data.columns if is_eligible(data[c].dtype)]


def fmt(value: float) -> str:
    """A boundary or statistic as text: 35, -5, 12.25, 1.5e+20 (shortest exact form)."""
    value = float(value)
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"The setting '{key}' must be a number.")
    value = float(value)
    if not math.isfinite(value):
        raise _invalid(f"The setting '{key}' must be a finite number.")
    return value


def check_settings(method: Any, params: Mapping[str, Any]) -> dict[str, float]:
    """The method's settings with defaults filled in, or CleaningError("invalid_params").

    Settings that belong to another method must be absent (or None).
    """
    if method not in METHODS:
        raise _invalid(f"The setting 'method' must be one of: {', '.join(METHODS)}.")
    own = METHOD_KEYS[method]
    stray = sorted(k for k in SETTING_KEYS - set(own) if params.get(k) is not None)
    if stray:
        raise _invalid(f"The setting '{stray[0]}' is not used by the {METHOD_LABELS[method]} method.")
    out = {k: _number(params[k] if params.get(k) is not None else DEFAULTS[k], k) for k in own}
    if method == "iqr":
        k = out["multiplier"]
        if not 0 < k <= MAX_MULTIPLIER:
            raise _invalid(f"The IQR multiplier must be more than 0 and at most {MAX_MULTIPLIER:g}.")
    elif method == "zscore":
        t = out["threshold"]
        if not 0 < t <= MAX_THRESHOLD:
            raise _invalid(f"The z-score threshold must be more than 0 and at most {MAX_THRESHOLD:g}.")
    else:
        lo, hi = out["lower_percentile"], out["upper_percentile"]
        if not (0 <= lo < hi <= 100):
            raise _invalid("Percentiles must satisfy 0 <= lower < upper <= 100; "
                           f"got lower {lo:g} and upper {hi:g}.")
    return out


def describe_settings(method: str, settings: Mapping[str, float]) -> str:
    """'IQR, k = 1.5', 'Z-score, threshold 3', 'Percentile, 1st to 99th'."""
    if method == "iqr":
        return f"IQR, k = {settings['multiplier']:g}"
    if method == "zscore":
        return f"Z-score, threshold {settings['threshold']:g}"
    return f"Percentile, {_ordinal(settings['lower_percentile'])} to {_ordinal(settings['upper_percentile'])}"


def _ordinal(p: float) -> str:
    if not float(p).is_integer():
        return f"{p:g}th"
    n = int(p)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ------------------------------------------------------------------
# Values and masks
# ------------------------------------------------------------------

def numeric_values(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(float64 values with NaN for missing cells, missing mask). Positional, never index-aligned."""
    missing = series.isna().to_numpy(dtype=bool)
    values = series.to_numpy(dtype=np.float64, na_value=np.nan)
    return values, missing


def outside(values: np.ndarray, lower: float, upper: float) -> tuple[np.ndarray, np.ndarray]:
    """(below, above) masks: strictly outside [lower, upper]. NaN (missing) is neither."""
    with np.errstate(invalid="ignore"):
        return values < lower, values > upper


# ------------------------------------------------------------------
# Detection
# ------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class OutlierDetection:
    """What a method finds in one column. `status` is "ok" or why nothing could be detected."""

    column: str
    method: str
    settings: dict[str, float]
    status: str                    # ok | unsupported | no_values | too_few | non_finite | too_large
    reason: str = ""
    rows: int = 0
    evaluated: int = 0             # Present (non-missing) values
    missing: int = 0
    lower: float | None = None
    upper: float | None = None
    below: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool), repr=False)
    above: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool), repr=False)
    statistics: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def mask(self) -> np.ndarray:
        """True at the row positions of outliers."""
        return self.below | self.above

    @property
    def n_below(self) -> int:
        return int(self.below.sum())

    @property
    def n_above(self) -> int:
        return int(self.above.sum())

    @property
    def outliers(self) -> int:
        return self.n_below + self.n_above

    @property
    def pct(self) -> float:
        """Share of evaluated (non-missing) values classified as outliers, in percent."""
        return 100.0 * self.outliers / self.evaluated if self.evaluated else 0.0


def _bounds(method: str, present: np.ndarray, settings: Mapping[str, float]) -> tuple[float, float, dict, list[str]]:
    notes: list[str] = []
    constant = bool(present.min() == present.max())
    if method == "iqr":
        q1, q3 = (float(q) for q in np.quantile(present, [0.25, 0.75], method=QUANTILE_METHOD))
        iqr = q3 - q1
        k = settings["multiplier"]
        lower, upper = q1 - k * iqr, q3 + k * iqr
        stats = {"q1": q1, "q3": q3, "iqr": iqr}
        if iqr == 0 and not constant:
            notes.append(f"The IQR is 0 (Q1 = Q3 = {fmt(q1)}) because at least half of the values are equal, "
                         f"so every value other than {fmt(q1)} is outside the boundaries.")
    elif method == "zscore":
        n = len(present)
        if constant:
            mean, sd = float(present[0]), 0.0          # Exactly: no rounding can create a spread
        else:
            # Scaled by the largest magnitude so squaring cannot overflow.
            scale = float(max(abs(present.min()), abs(present.max())))
            scaled = present / scale
            m = float(scaled.mean())
            mean = m * scale
            sd = float(np.sqrt(np.mean((scaled - m) ** 2))) * scale
        t = settings["threshold"]
        lower, upper = mean - t * sd, mean + t * sd
        stats = {"mean": mean, "std": sd}
        limit = math.sqrt(n - 1)
        if not constant and t >= limit:
            notes.append(f"With {n:,} values, no z-score can be larger than √(n - 1) = {limit:.3g}, so a threshold "
                         f"of {t:g} cannot find any outlier.")
    else:
        lo, hi = settings["lower_percentile"], settings["upper_percentile"]
        lower, upper = (float(q) for q in np.quantile(present, [lo / 100.0, hi / 100.0], method=QUANTILE_METHOD))
        stats = {"lower_value": lower, "upper_value": upper}
        if lower == upper and not constant:
            notes.append(f"Both percentiles fall on the same value ({fmt(lower)}) because of repeated values, so "
                         "every other value is outside the boundaries.")
    return float(lower), float(upper), stats, notes


def detect_outliers(series: pd.Series, method: str, settings: Mapping[str, Any]) -> OutlierDetection:
    """Detect outliers in one column. Never changes the series; raises only for invalid settings."""
    settings = check_settings(method, settings)
    name = str(series.name)
    rows = len(series)
    base = {"column": name, "method": method, "settings": settings, "rows": rows}
    if not is_eligible(series.dtype):
        return OutlierDetection(**base, status="unsupported",
                                reason=f"'{name}' is stored as {series.dtype}. Outlier detection works on whole-number "
                                       "and decimal columns; convert text to numbers on the Data types tab first.")
    values, missing = numeric_values(series)
    n_missing = int(missing.sum())
    base.update(missing=n_missing, evaluated=rows - n_missing)
    present = values[~missing]
    if not len(present):
        why = "has no rows" if not rows else "has no values (every cell is missing)"
        return OutlierDetection(**base, status="no_values", reason=f"'{name}' {why}, so there is nothing to check.")
    finite = np.isfinite(present)
    if not finite.all():
        bad = int((~finite).sum())
        return OutlierDetection(**base, status="non_finite",
                                reason=f"'{name}' holds {bad:,} non-finite value{'s' if bad != 1 else ''} (inf or NaN "
                                       "that is not a missing value). They would distort the statistics, so detection "
                                       "is refused. Replace them first.")
    if len(present) < MIN_VALUES:
        return OutlierDetection(**base, status="too_few",
                                reason=f"'{name}' has only {len(present):,} value{'s' if len(present) != 1 else ''}. "
                                       f"Outlier detection needs at least {MIN_VALUES}.")
    with np.errstate(over="ignore", invalid="ignore"):
        lower, upper, stats, notes = _bounds(method, present, settings)
    if not (math.isfinite(lower) and math.isfinite(upper)) or not all(math.isfinite(v) for v in stats.values()):
        return OutlierDetection(**base, status="too_large",
                                reason=f"The values in '{name}' are too large for the boundaries to be calculated "
                                       "as finite numbers.")
    below, above = outside(values, lower, upper)
    return OutlierDetection(**base, status="ok", lower=lower, upper=upper, below=below, above=above,
                            statistics=stats, notes=tuple(notes))


__all__ = [
    "DEFAULTS", "ELIGIBLE_KINDS", "METHODS", "METHOD_KEYS", "METHOD_LABELS", "MIN_VALUES", "OutlierDetection",
    "check_settings", "describe_settings", "detect_outliers", "eligible_columns", "fmt", "is_eligible",
    "numeric_values", "outside",
]
