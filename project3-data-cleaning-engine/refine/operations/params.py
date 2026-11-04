"""Checks shared by the operations' validate() methods.

Each raises CleaningError("invalid_params") with a message that tells the
user what to change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from refine.errors import CleaningError
from refine.notes import quote, verb


def invalid(message: str) -> CleaningError:
    return CleaningError("invalid_params", message)


def check_keys(params: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise invalid(f"Unknown setting for {label}: {', '.join(unknown)}.")


def column_list(value: Any, data: pd.DataFrame, *, what: str = "columns", allow_empty: bool = False) -> list[str]:
    """Distinct existing column names, returned in table order."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise invalid(f"Choose the {what} as a list of column names.")
    if not value and not allow_empty:
        raise invalid(f"Choose at least one of the {what}.")
    if not all(isinstance(c, str) for c in value):
        raise invalid("Column names must be text.")
    repeated = sorted({c for c in value if list(value).count(c) > 1})
    if repeated:
        raise invalid(f"Each column can be chosen once; repeated: {', '.join(map(quote, repeated))}.")
    existing = set(data.columns)
    absent = [c for c in value if c not in existing]
    if absent:
        raise invalid(f"The table has no {verb(len(absent), 'column', 'columns')} {', '.join(map(quote, absent))}.")
    chosen = set(value)
    return [c for c in data.columns if c in chosen]


def one_column(value: Any, data: pd.DataFrame) -> str:
    if not isinstance(value, str) or not value:
        raise invalid("Choose a column.")
    if value not in data.columns:
        raise invalid(f"The table has no column {quote(value)}.")
    return value


def flag(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise invalid(f"The setting '{key}' must be true or false.")
    return value


def choice(params: Mapping[str, Any], key: str, options: Sequence[str], default: str | None = None) -> str:
    value = params.get(key, default)
    if value not in options:
        raise invalid(f"The setting '{key}' must be one of: {', '.join(options)}.")
    return value


def integer(value: Any, key: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid(f"The setting '{key}' must be a whole number.")
    if minimum is not None and value < minimum:
        raise invalid(f"The setting '{key}' must be at least {minimum:,}.")
    if maximum is not None and value > maximum:
        raise invalid(f"The setting '{key}' must be at most {maximum:,}.")
    return value


def number(value: Any, key: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid(f"The setting '{key}' must be a number.")
    value = float(value)
    if not (minimum <= value <= maximum):
        raise invalid(f"The setting '{key}' must be between {minimum:g} and {maximum:g}.")
    return value


def text_columns_only(columns: Sequence[str], data: pd.DataFrame, action: str) -> None:
    """REFINE stores loaded values as text; these steps write text values."""
    other = [c for c in columns if not isinstance(data[c].dtype, pd.StringDtype)]
    if other:
        raise invalid(f"{action} works on text columns; {', '.join(map(quote, other))} "
                      f"{verb(len(other), 'is', 'are')} stored as {data[other[0]].dtype}. Convert "
                      f"{verb(len(other), 'it', 'them')} to text first (Data types) if you want to change "
                      "the values as text.")


def unique_label(base: str, taken: set[str]) -> str:
    """base, or "base (2)", "base (3)", ... whichever is not taken (for display-only columns)."""
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def expect(actual: int, expected: Any, what: str, label: str) -> None:
    """Replay guard: the data must give the counts recorded when the step was planned."""
    if expected is not None and int(expected) != actual:
        raise CleaningError(
            "replay_mismatch",
            f"{label}: the data gives {actual:,} {what} where the recorded step expected {int(expected):,}.",
        )
