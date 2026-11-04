"""Remove duplicate rows.

Rows match when they are equal in the chosen columns (all columns by
default), optionally ignoring case and extra whitespace; see
refine/duplicates.py. Normalization is used for matching only: the rows
that are kept keep their exact stored values.

In each duplicate group the first row (or the last, if chosen) in the
current table order is kept and the others are removed. Surviving rows
keep their order and their row identities. The column list, the matching
options, which row is kept, and the expected numbers of groups and
removed rows are frozen in the plan; apply() refuses data that does not
give the same numbers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from refine.duplicates import group_ids, removal_mask
from refine.errors import CleaningError
from refine.notes import plural, quote, verb
from refine.operations.base import Operation, OperationResult
from refine.operations.missing_ops import no_change
from refine.operations.params import check_keys, choice, column_list, expect, flag, unique_label
from refine.operations.registry import register

GROUP_LABEL, OUTCOME_LABEL = "Duplicate group", "Outcome"
KEPT, REMOVED = "Kept", "Removed"


class RemoveDuplicates(Operation):
    op_id = "remove_duplicates"
    label = "Remove duplicate rows"
    description = ("Keep one row from each group of matching rows and remove the others. Rows match on all "
                   "columns, or on key columns you choose, optionally ignoring case and extra spaces.")

    def validate(self, params, data):
        check_keys(params, {"columns", "ignore_case", "normalize_whitespace", "keep"}, self.label)
        columns = params.get("columns")
        if columns is not None:
            columns = column_list(columns, data, what="columns to match on")
        return {
            "columns": columns,
            "ignore_case": flag(params, "ignore_case", False),
            "normalize_whitespace": flag(params, "normalize_whitespace", False),
            "keep": choice(params, "keep", ("first", "last"), "first"),
        }

    def _groups(self, data, resolved) -> np.ndarray:
        absent = [c for c in resolved["columns"] if c not in data.columns]
        if absent:
            raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(absent[0])}.")
        return group_ids(data, resolved["columns"], ignore_case=resolved["ignore_case"],
                         normalize_whitespace=resolved["normalize_whitespace"])

    def resolve(self, params, data):
        all_columns = params["columns"] is None
        columns = list(data.columns) if all_columns else params["columns"]
        resolved = {**params, "columns": columns, "all_columns": all_columns}
        groups = self._groups(data, resolved)
        remove = removal_mask(groups, params["keep"])
        n = int(remove.sum())
        if not n:
            raise no_change("No rows match each other on these columns, so there are no duplicates to remove.")
        sizes = np.bincount(groups)
        n_groups = int((sizes > 1).sum())
        rows = len(data)
        warnings = [f"{plural(n, 'row')} of {rows:,} ({100.0 * n / rows:.1f}%) would be removed, keeping the "
                    f"{params['keep']} row of each of the {plural(n_groups, 'duplicate group')}. Undo or reset "
                    "brings them back."]
        if not all_columns or params["ignore_case"] or params["normalize_whitespace"]:
            # Removed rows that are not exact copies of the row kept from their group.
            exact = group_ids(data, list(data.columns))
            kept = pd.Series(np.arange(rows)).groupby(groups).agg(params["keep"]).to_numpy()
            differs = int((exact[remove] != exact[kept[groups[remove]]]).sum())
            if differs:
                warnings.append(
                    f"{plural(differs, 'removed row')} {verb(differs, 'is', 'are')} not an exact copy of the row "
                    "kept from its group: "
                    + ("other columns differ" if not all_columns else "they differ in case or spacing")
                    + ". Those differences are lost from the working table."
                )
        if params["ignore_case"] or params["normalize_whitespace"]:
            what = " and ".join(w for w, on in (("case", params["ignore_case"]),
                                                ("extra spaces", params["normalize_whitespace"])) if on)
            warnings.append(f"Matching ignores {what}. This affects matching only: the rows kept keep their "
                            "exact values.")
        resolved.update({"rows": rows, "groups": n_groups, "rows_removed": n})
        return resolved, tuple(columns), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        groups = self._groups(data, resolved)
        remove = removal_mask(groups, resolved["keep"])
        expect(int(remove.sum()), resolved["rows_removed"], "duplicate rows", self.label)
        expect(int((np.bincount(groups) > 1).sum()) if len(groups) else 0, resolved["groups"],
               "duplicate groups", self.label)
        keep = ~remove
        result = data.loc[keep].reset_index(drop=True).copy(deep=True)
        return OperationResult(result, np.array(np.asarray(row_ids)[keep], dtype=np.int64, copy=True), {
            "rows_removed": int(remove.sum()), "groups": resolved["groups"], "rows_kept": int(keep.sum()),
        })

    def sample(self, data, resolved, limit):
        """Rows of the first duplicate groups, group by group, marked Kept or Removed."""
        groups = self._groups(data, resolved)
        remove = removal_mask(groups, resolved["keep"])
        sizes = np.bincount(groups) if len(groups) else np.zeros(0, dtype=np.int64)
        positions: list[int] = []
        numbers: list[int] = []
        for rank, g in enumerate(np.flatnonzero(sizes > 1), start=1):
            if len(positions) >= limit:
                break
            members = np.flatnonzero(groups == g)[: limit - len(positions)]
            positions.extend(int(p) for p in members)
            numbers.extend([rank] * len(members))
        idx = np.array(positions, dtype=np.int64)
        out = data.iloc[idx].copy()
        taken = set(data.columns)
        group_label = unique_label(GROUP_LABEL, taken)
        outcome_label = unique_label(OUTCOME_LABEL, taken | {group_label})
        out.insert(0, outcome_label, np.where(remove[idx], REMOVED, KEPT) if len(idx) else [])
        out.insert(0, group_label, numbers)
        return out

    def summarize(self, resolved):
        on = "all columns" if resolved["all_columns"] else ", ".join(map(quote, resolved["columns"]))
        extras = [w for w, f in (("ignoring case", resolved["ignore_case"]),
                                 ("ignoring extra spaces", resolved["normalize_whitespace"])) if f]
        how = f" ({', '.join(extras)})" if extras else ""
        return (f"Remove {plural(resolved['rows_removed'], 'duplicate row')} matching on {on}{how}, keeping the "
                f"{resolved['keep']} row of each of {plural(resolved['groups'], 'group')}.")


REMOVE_DUPLICATES = register(RemoveDuplicates())
