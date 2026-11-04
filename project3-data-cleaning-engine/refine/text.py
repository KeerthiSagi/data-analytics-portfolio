"""Text standardization rules. Read-only: functions return new values.

Every rule is a function from one text value to another. `map_distinct`
applies it to a text column's distinct values (so cost grows with the
number of distinct values, not rows) and rebuilds the column with the
same dtype. Missing cells (pd.NA) stay missing; they are never passed to
a rule or turned into the text "nan" or "None".

Rules
-----
* trim: remove leading and trailing whitespace (Python's definition,
  which includes tabs, line breaks and no-break spaces).
* case: lower, upper, title or casefold. Title case capitalizes the first
  letter after the start, a space, a hyphen or a slash and lower-cases
  the rest, so "o'neil" becomes "O'neil" and "McDonald" becomes
  "Mcdonald": check names and brands before using it.
* collapse whitespace: runs of two or more spaces become one space; with
  `all_whitespace`, any run of whitespace (tabs, line breaks, no-break
  spaces) becomes one space. Nothing else is removed.
* unicode: NFC (canonical composition, never changes meaning) or NFKC
  (compatibility forms too: full-width "ＡＢＣ" becomes "ABC", "①" becomes
  "1", the ligature "ﬁ" becomes "fi").
* control characters: removes C0/C1 control characters (Unicode category
  Cc), optionally keeping tab, line feed and carriage return, and
  optionally a short list of invisible formatting characters (zero-width
  space, word joiner, byte order mark, soft hyphen, Mongolian vowel
  separator). Letters of every script, accents, emoji and zero-width
  joiners (needed in several scripts and emoji) are kept.
* find and replace: literal text only (no regular expressions, so no
  pattern can run for a long time), either the whole cell or every
  occurrence inside it, with or without matching case.
* category mapping: exact stored values to the canonical values the user
  chose. Unmapped values are kept.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

CASES = ("lower", "upper", "title", "casefold")
CASE_LABELS = {"lower": "lowercase", "upper": "UPPERCASE", "title": "Title Case", "casefold": "casefold"}
UNICODE_FORMS = ("NFC", "NFKC")
INVISIBLE_FORMAT = {
    "​": "zero-width space", "⁠": "word joiner", "﻿": "byte order mark",
    "­": "soft hyphen", "᠎": "Mongolian vowel separator",
}
KEPT_CONTROLS = "\t\n\r"
MAX_FIND_LENGTH = 200

_SPACES = re.compile(r" {2,}")
_WHITESPACE = re.compile(r"\s+")
_TITLE_START = re.compile(r"(^|[\s\-/])(\w)")


def trim(value: str) -> str:
    return value.strip()


def change_case(value: str, case: str) -> str:
    if case == "lower":
        return value.lower()
    if case == "upper":
        return value.upper()
    if case == "casefold":
        return value.casefold()
    return _TITLE_START.sub(lambda m: m.group(1) + m.group(2).upper(), value.lower())


def collapse_whitespace(value: str, all_whitespace: bool) -> str:
    return (_WHITESPACE if all_whitespace else _SPACES).sub(" ", value)


def normalize_unicode(value: str, form: str) -> str:
    return unicodedata.normalize(form, value)


def control_table(keep_whitespace: bool, remove_invisible: bool) -> dict[int, None]:
    codes = [c for c in [*range(0x00, 0x20), *range(0x7F, 0xA0)]
             if not (keep_whitespace and chr(c) in KEPT_CONTROLS)]
    if remove_invisible:
        codes += [ord(c) for c in INVISIBLE_FORMAT]
    return dict.fromkeys(codes)


def find_replace_function(find: str, replace: str, whole_cell: bool, case_sensitive: bool
                          ) -> Callable[[str], tuple[str, int]]:
    """value -> (new value, number of replacements)."""
    if whole_cell:
        target = find if case_sensitive else find.casefold()

        def whole(value: str) -> tuple[str, int]:
            same = value == find if case_sensitive else value.casefold() == target
            return (replace, 1) if same else (value, 0)
        return whole
    if case_sensitive:
        def literal(value: str) -> tuple[str, int]:
            n = value.count(find)
            return (value.replace(find, replace), n) if n else (value, 0)
        return literal
    pattern = re.compile(re.escape(find), re.IGNORECASE)

    def insensitive(value: str) -> tuple[str, int]:
        # A function replacement: the replacement text is used literally (no backslash escapes).
        return pattern.subn(lambda _m: replace, value)
    return insensitive


# ------------------------------------------------------------------
# Applying a rule to a column
# ------------------------------------------------------------------

@dataclass(frozen=True)
class TextChange:
    new: pd.Series
    changed: np.ndarray            # Row mask: cells whose text changes
    cells: int                     # Changed cells
    distinct: int                  # Distinct values that change
    replacements: int              # Find and replace: occurrences replaced (else equals cells)
    became_empty: int              # Cells that would hold empty text (REFINE counts it as missing)
    examples: tuple[tuple[str, str, int], ...]   # (before, after, cells), most frequent first


def map_distinct(series: pd.Series, rule: Callable[[str], str | tuple[str, int]], example_limit: int = 8
                 ) -> TextChange:
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    codes = np.asarray(codes, dtype=np.int64)
    before = list(uniques)
    after: list[str] = []
    counts: list[int] = []
    for value in before:
        out = rule(value)
        if isinstance(out, tuple):
            after.append(out[0])
            counts.append(out[1])
        else:
            after.append(out)
            counts.append(int(out != value))
    n = len(before)
    freq = np.bincount(codes[codes >= 0], minlength=n) if n else np.zeros(0, dtype=np.int64)
    changed_u = np.array([a != b for a, b in zip(before, after)], dtype=bool) if n else np.zeros(0, dtype=bool)
    present = codes >= 0
    changed = np.zeros(len(codes), dtype=bool)
    if n:
        changed[present] = changed_u[codes[present]]
        new_values = pd.array(after, dtype=series.dtype).take(codes, allow_fill=True)
    else:
        new_values = series.array.copy()
    empty_u = np.array([a == "" and b != "" for a, b in zip(after, before)], dtype=bool) if n else changed_u
    order = np.argsort(-freq, kind="stable") if n else []
    examples = tuple((before[i], after[i], int(freq[i])) for i in order if changed_u[i])[:example_limit]
    return TextChange(
        new=pd.Series(new_values, name=series.name),
        changed=changed,
        cells=int(freq[changed_u].sum()) if n else 0,
        distinct=int(changed_u.sum()),
        replacements=int((np.array(counts, dtype=np.int64) * freq).sum()) if n else 0,
        became_empty=int(freq[empty_u].sum()) if n else 0,
        examples=examples,
    )


def value_frequencies(series: pd.Series) -> list[tuple[str, int]]:
    """Distinct non-missing values with their counts, most frequent first (ties: first appearance)."""
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    codes = np.asarray(codes, dtype=np.int64)
    if not len(uniques):
        return []
    freq = np.bincount(codes[codes >= 0], minlength=len(uniques))
    order = np.argsort(-freq, kind="stable")
    return [(str(uniques[i]), int(freq[i])) for i in order]


def category_key(value: str) -> str:
    """The grouping key for suggestions: case and spacing ignored."""
    return _WHITESPACE.sub(" ", value).strip().casefold()


def suggest_categories(frequencies: list[tuple[str, int]]) -> dict[str, str]:
    """{observed value: suggested canonical value} for values that differ only in case or spacing.

    The suggestion is the most frequent spelling in the group that has no
    extra spaces (no leading, trailing or repeated spaces), or the most
    frequent spelling if every variant has them. Suggestions are only
    shown; nothing is mapped until the user approves a mapping.
    """
    groups: dict[str, list[tuple[str, int]]] = {}
    for value, count in frequencies:            # Already most frequent first
        groups.setdefault(category_key(value), []).append((value, count))
    out: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        tidy = [v for v, _ in members if v == _WHITESPACE.sub(" ", v).strip()]
        canonical = tidy[0] if tidy else members[0][0]
        for value, _ in members:
            if value != canonical:
                out[value] = canonical
    return out


def mapping_problems(pairs: list[tuple[str, str]]) -> list[str]:
    """Conflicts in a list of (from, to) pairs; empty when the mapping can be used."""
    problems = []
    targets: dict[str, str] = {}
    for source, target in pairs:
        if source in targets and targets[source] != target:
            problems.append(f"'{source}' is mapped to both '{targets[source]}' and '{target}'.")
        targets.setdefault(source, target)
    sources = {s for s, t in pairs if s != t}
    for source, target in pairs:
        if source != target and target in sources and targets.get(target) != target:
            problems.append(f"'{target}' is a canonical value and is also mapped to "
                            f"'{targets[target]}'. Map every variant directly to its final value.")
    return list(dict.fromkeys(problems))


def mapping_rule(mapping: Mapping[str, str]) -> Callable[[str], str]:
    return lambda value: mapping.get(value, value)


__all__ = [
    "CASES", "CASE_LABELS", "INVISIBLE_FORMAT", "KEPT_CONTROLS", "MAX_FIND_LENGTH", "UNICODE_FORMS", "TextChange",
    "category_key", "change_case", "collapse_whitespace", "control_table", "find_replace_function", "map_distinct",
    "mapping_problems", "mapping_rule", "normalize_unicode", "suggest_categories", "trim", "value_frequencies",
]
