"""Text standardization operations.

Each operation applies one rule from refine/text.py to the text columns
the user selects. They share one framework:

* validate: the columns must be text (a typed column is refused with a
  pointer to Convert data type, so typed values are never stringified
  silently) and the rule's settings must be valid.
* resolve: the rule is run on each column's distinct values; the number
  of cells that change per column is frozen, with warnings for
  information that may be lost (for example cells that would become
  empty text, which REFINE counts as missing).
* apply: the rule runs again with the frozen settings; a different count
  is refused (replay_mismatch). Missing cells stay missing, other columns
  and row identities are unchanged.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from refine.errors import CleaningError
from refine.notes import plural, quote, verb
from refine.operations.base import Operation, OperationResult
from refine.operations.missing_ops import _before_after, _ids, no_change
from refine.operations.params import (
    check_keys,
    choice,
    column_list,
    expect,
    flag,
    invalid,
    one_column,
    text_columns_only,
)
from refine.operations.registry import register
from refine.text import (
    CASE_LABELS,
    CASES,
    INVISIBLE_FORMAT,
    MAX_FIND_LENGTH,
    UNICODE_FORMS,
    change_case,
    collapse_whitespace,
    control_table,
    find_replace_function,
    map_distinct,
    mapping_problems,
    normalize_unicode,
    trim,
)

MAX_MAPPINGS = 500
MAX_VALUE_LENGTH = 200
EMPTY_WARNING = ("{n} would become empty text, which REFINE counts as missing. Undo or reset brings the "
                 "original text back.")


class _TextOperation(Operation):
    """Shared validate / resolve / apply for rules that map text to text."""

    keys: set[str] = set()

    def options(self, params, data) -> dict[str, Any]:
        return {}

    def rule(self, resolved):
        raise NotImplementedError

    def extra_warnings(self, resolved, changes) -> list[str]:
        return []

    def target_columns(self, params, data) -> list[str]:
        return column_list(params.get("columns"), data, what="columns to change")

    def validate(self, params, data):
        check_keys(params, {"columns"} | self.keys, self.label)
        columns = self.target_columns(params, data)
        text_columns_only(columns, data, self.label)
        return {"columns": columns, **self.options(params, data)}

    def _changes(self, data, resolved, only_changed: bool):
        rule = self.rule(resolved)
        for c in resolved["columns"]:
            if only_changed and not resolved["cells"].get(c):
                continue
            if c not in data.columns:
                raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(c)}.")
            text_columns_only([c], data, self.label)
            change = map_distinct(data[c], rule)
            if only_changed:
                expect(change.cells, resolved["cells"][c], f"cells to change in {quote(c)}", self.label)
            yield c, change

    def resolve(self, params, data):
        changes = dict(self._changes(data, params, only_changed=False))
        cells = {c: ch.cells for c, ch in changes.items()}
        if not sum(cells.values()):
            raise no_change("No cell in the selected columns would change, so there is nothing to apply.")
        warnings = self.extra_warnings(params, changes)
        empty = sum(ch.became_empty for ch in changes.values())
        if empty:
            warnings.append(EMPTY_WARNING.format(n=plural(empty, "cell")))
        resolved = {**params, "cells": cells,
                    "replacements": {c: ch.replacements for c, ch in changes.items() if ch.cells},
                    "became_empty": empty}
        return resolved, tuple(c for c in params["columns"] if cells[c]), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        result = data.copy(deep=True)
        total = columns = replaced = 0
        for c, change in self._changes(data, resolved, only_changed=True):
            result[c] = change.new.array
            total += change.cells
            replaced += change.replacements
            columns += 1
        return OperationResult(result, _ids(row_ids), {"cells_changed": total, "columns_changed": columns,
                                                       "replacements": replaced})

    def sample(self, data, resolved, limit):
        parts = list(self._changes(data, resolved, only_changed=True))
        any_changed = np.logical_or.reduce([ch.changed for _, ch in parts])
        positions = np.flatnonzero(any_changed)[:limit]
        return _before_after(data, positions, [c for c, _ in parts], {c: ch.new for c, ch in parts})

    def where(self, resolved) -> str:
        cols = [c for c in resolved["columns"] if resolved["cells"].get(c)]
        return quote(cols[0]) if len(cols) == 1 else plural(len(cols), "column")

    def total(self, resolved) -> int:
        return sum(resolved["cells"].values())


class TrimWhitespace(_TextOperation):
    op_id = "trim_whitespace"
    label = "Trim whitespace"
    description = "Remove spaces, tabs and line breaks at the start and end of each value. Inner spacing is kept."

    def rule(self, resolved):
        return trim

    def summarize(self, resolved):
        return f"Trim leading and trailing whitespace in {plural(self.total(resolved), 'cell')} of {self.where(resolved)}."


class ChangeCase(_TextOperation):
    op_id = "change_case"
    label = "Change capitalization"
    description = "Write every value in lowercase, UPPERCASE, Title Case or casefold (lowercase for matching)."
    keys = {"case"}

    def options(self, params, data):
        return {"case": choice(params, "case", CASES)}

    def rule(self, resolved):
        case = resolved["case"]
        return lambda v: change_case(v, case)

    def extra_warnings(self, resolved, changes):
        if resolved["case"] == "title":
            return ["Title case lower-cases every letter after the first of each word, so names and brands such "
                    "as 'McDonald', 'O'Neil' or 'iPhone' and acronyms such as 'USA' change. Check them before "
                    "applying."]
        if resolved["case"] == "casefold":
            return ["Casefold is meant for matching: it also changes some letters, for example 'ß' becomes "
                    "'ss'."]
        return []

    def summarize(self, resolved):
        return (f"Change {plural(self.total(resolved), 'cell')} in {self.where(resolved)} to "
                f"{CASE_LABELS[resolved['case']]}.")


class CollapseWhitespace(_TextOperation):
    op_id = "collapse_whitespace"
    label = "Collapse repeated spaces"
    description = "Replace runs of spaces inside values with one space, optionally treating tabs and line breaks as spaces."
    keys = {"all_whitespace"}

    def options(self, params, data):
        return {"all_whitespace": flag(params, "all_whitespace", False)}

    def rule(self, resolved):
        all_ws = resolved["all_whitespace"]
        return lambda v: collapse_whitespace(v, all_ws)

    def extra_warnings(self, resolved, changes):
        if not resolved["all_whitespace"]:
            return []
        return ["Tabs, line breaks and no-break spaces inside values become single spaces, so line structure "
                "inside a cell is lost."]

    def summarize(self, resolved):
        what = "whitespace" if resolved["all_whitespace"] else "spaces"
        return f"Collapse repeated {what} to one space in {plural(self.total(resolved), 'cell')} of {self.where(resolved)}."


class NormalizeUnicode(_TextOperation):
    op_id = "normalize_unicode"
    label = "Normalize Unicode"
    description = ("Rewrite characters in a standard Unicode form: NFC joins accents to their letters; NFKC also "
                   "replaces compatibility characters such as full-width letters.")
    keys = {"form"}

    def options(self, params, data):
        return {"form": choice(params, "form", UNICODE_FORMS, "NFKC")}

    def rule(self, resolved):
        form = resolved["form"]
        return lambda v: normalize_unicode(v, form)

    def extra_warnings(self, resolved, changes):
        if resolved["form"] == "NFKC":
            return ["NFKC replaces compatibility characters with plain ones, which can change meaning: '²' "
                    "becomes '2', '½' becomes '1⁄2', '①' becomes '1' and 'ﬁ' becomes 'fi'. Check the examples."]
        return []

    def summarize(self, resolved):
        return f"Normalize {plural(self.total(resolved), 'cell')} in {self.where(resolved)} to Unicode {resolved['form']}."


class RemoveControlCharacters(_TextOperation):
    op_id = "remove_control_characters"
    label = "Remove non-printing characters"
    description = ("Remove invisible control characters. Letters of every language, accents and emoji are "
                   "never removed.")
    keys = {"keep_whitespace", "remove_invisible"}

    def options(self, params, data):
        return {"keep_whitespace": flag(params, "keep_whitespace", True),
                "remove_invisible": flag(params, "remove_invisible", False)}

    def rule(self, resolved):
        table = control_table(resolved["keep_whitespace"], resolved["remove_invisible"])
        return lambda v: v.translate(table)

    def extra_warnings(self, resolved, changes):
        if not resolved["keep_whitespace"]:
            return ["Tabs and line breaks are removed too, so words they separated may run together."]
        return []

    def summarize(self, resolved):
        extra = " and invisible formatting characters" if resolved["remove_invisible"] else ""
        return (f"Remove control characters{extra} from {plural(self.total(resolved), 'cell')} in "
                f"{self.where(resolved)}.")


class FindReplace(_TextOperation):
    op_id = "find_replace"
    label = "Find and replace"
    description = ("Replace text you type, either whole cells that equal it or every occurrence inside cells. "
                   "Matching is literal: no wildcards or regular expressions.")
    keys = {"find", "replace", "match", "case_sensitive"}

    def options(self, params, data):
        find, replace = params.get("find"), params.get("replace", "")
        if not isinstance(find, str) or find == "":
            raise invalid("Enter the text to find.")
        if not isinstance(replace, str):
            raise invalid("The replacement must be text (it can be empty).")
        if len(find) > MAX_FIND_LENGTH or len(replace) > MAX_FIND_LENGTH:
            raise invalid(f"The text to find and its replacement can be at most {MAX_FIND_LENGTH} characters.")
        case_sensitive = flag(params, "case_sensitive", True)
        if find == replace and case_sensitive:
            raise no_change("The replacement is the same as the text to find, so nothing would change.")
        return {"find": find, "replace": replace, "match": choice(params, "match", ("whole_cell", "substring")),
                "case_sensitive": case_sensitive}

    def rule(self, resolved):
        return find_replace_function(resolved["find"], resolved["replace"], resolved["match"] == "whole_cell",
                                     resolved["case_sensitive"])

    def extra_warnings(self, resolved, changes):
        warnings = []
        if resolved["match"] == "substring" and len(resolved["find"]) <= 2:
            warnings.append(f"{quote(resolved['find'])} is short, so it may also match inside words you did not "
                            "mean to change. Check the examples.")
        if not resolved["case_sensitive"]:
            warnings.append("Case is ignored, so other capitalizations also match.")
        return warnings

    def summarize(self, resolved):
        n = sum(resolved["replacements"].values())
        how = "cells equal to" if resolved["match"] == "whole_cell" else "occurrences of"
        case = "" if resolved["case_sensitive"] else " (ignoring case)"
        return (f"Replace {plural(n, 'match', 'matches')} ({how} {quote(resolved['find'])}{case}) with "
                f"{quote(resolved['replace'])} in {plural(self.total(resolved), 'cell')} of {self.where(resolved)}.")


class MapCategories(_TextOperation):
    op_id = "map_categories"
    label = "Standardize categories"
    description = ("Map the exact values you choose to one canonical spelling, for example 'NY' and 'new york' to "
                   "'New York'. Values without a mapping are kept as they are.")
    keys = {"column", "mapping"}

    def target_columns(self, params, data):
        return [one_column(params.get("column"), data)]

    def validate(self, params, data):
        check_keys(params, {"column", "mapping"}, self.label)
        column = one_column(params.get("column"), data)
        text_columns_only([column], data, self.label)
        raw = params.get("mapping")
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise invalid("Give the mapping as a list of [observed value, canonical value] pairs.")
        pairs: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2 or not all(isinstance(v, str) for v in item):
                raise invalid("Each mapping must be a pair of text values: [observed value, canonical value].")
            source, target = item
            if target == "":
                raise invalid(f"{quote(source)} is mapped to empty text. Use Standardize missing values to mark "
                              "values as missing.")
            if len(target) > MAX_VALUE_LENGTH:
                raise invalid(f"Canonical values can be at most {MAX_VALUE_LENGTH} characters.")
            pairs.append((source, target))
        if len(pairs) > MAX_MAPPINGS:
            raise invalid(f"Map at most {MAX_MAPPINGS} values in one step.")
        problems = mapping_problems(pairs)
        if problems:
            raise invalid("The mapping has conflicts: " + " ".join(problems))
        kept = list(dict.fromkeys((s, t) for s, t in pairs if s != t))
        if not kept:
            raise invalid("Map at least one value to a different canonical value.")
        present = set(data[column].dropna().unique().tolist())
        absent = [s for s, _ in kept if s not in present]
        if absent:
            shown = ", ".join(map(quote, absent[:5]))
            raise invalid(f"{quote(column)} has no {verb(len(absent), 'value', 'values')} {shown}. Mappings use "
                          "the exact stored text, including case and spaces.")
        return {"columns": [column], "column": column, "mapping": [list(p) for p in kept]}

    def rule(self, resolved):
        mapping = {s: t for s, t in resolved["mapping"]}
        return lambda v: mapping.get(v, v)

    def extra_warnings(self, resolved, changes):
        targets = sorted({t for _, t in resolved["mapping"]})
        return [f"Values are merged into {plural(len(targets), 'canonical value')}: "
                + ", ".join(map(quote, targets[:6])) + (" and more" if len(targets) > 6 else "")
                + ". Grouping and counting will treat the merged spellings as one value."]

    def summarize(self, resolved):
        return (f"Map {plural(len(resolved['mapping']), 'value')} in {quote(resolved['column'])} to canonical "
                f"spellings, changing {plural(self.total(resolved), 'cell')}.")


TEXT_OPERATIONS = tuple(register(op) for op in (
    TrimWhitespace(), ChangeCase(), CollapseWhitespace(), NormalizeUnicode(), RemoveControlCharacters(),
    FindReplace(), MapCategories(),
))
TEXT_OPERATION_IDS = tuple(op.op_id for op in TEXT_OPERATIONS)
INVISIBLE_NAMES = tuple(INVISIBLE_FORMAT.values())

__all__ = ["TEXT_OPERATIONS", "TEXT_OPERATION_IDS", "INVISIBLE_NAMES"]
