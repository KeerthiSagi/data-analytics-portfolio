"""Helpers for building load-note messages consistently."""

from __future__ import annotations

from collections.abc import Sequence

from refine.config import NOTE_EXAMPLE_LIMIT


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """'1 row', '2 rows', '1,204 rows'."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n:,} {word}"


def example_list(values: Sequence[int], unit: str) -> str:
    """'line 4', 'lines 4, 9 and 12', 'lines 4, 9, 12, 15, 20 and 7 more'."""
    shown = [f"{v:,}" for v in values[:NOTE_EXAMPLE_LIMIT]]
    rest = len(values) - len(shown)
    if not shown:
        return ""
    label = unit if len(values) == 1 else unit + "s"
    if rest:
        return f"{label} {', '.join(shown)} and {rest:,} more"
    if len(shown) == 1:
        return f"{label} {shown[0]}"
    return f"{label} {', '.join(shown[:-1])} and {shown[-1]}"


def quote(name: str) -> str:
    return f"'{name}'"


def verb(n: int, one: str, many: str) -> str:
    """Pick the verb form that agrees with n ('has' / 'have')."""
    return one if n == 1 else many
