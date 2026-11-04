"""Drop selected columns: the first cleaning operation.

Settings:   {"columns": ["name", ...]}  chosen explicitly by the user.
Resolved:   {"columns": [...], "positions": [...]}  the columns in table
            order and their 1-based positions at the time of the preview.

Rows are not touched, so row identities pass through unchanged. Undo and
reset bring the columns back from the session's history; the uploaded
file is never changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from refine.errors import CleaningError
from refine.notes import plural, quote, verb
from refine.operations.base import Operation, OperationResult
from refine.operations.registry import register


def _present(series: pd.Series) -> int:
    """Cells holding a value: not pd.NA and not empty text (REFINE's 'missing')."""
    missing = series.isna()
    if not missing.all():
        missing = missing | (series.astype("string") == "").fillna(False).astype(bool)
    return int((~missing).sum())


class DropColumns(Operation):
    op_id = "drop_columns"
    label = "Drop columns"
    description = "Remove the columns you select from the working table. Rows are not changed."

    def validate(self, params: Mapping[str, Any], data: pd.DataFrame) -> dict[str, Any]:
        unknown = sorted(set(params) - {"columns"})
        if unknown:
            raise CleaningError("invalid_params", f"Unknown setting for Drop columns: {', '.join(unknown)}.")
        columns = params.get("columns")
        if isinstance(columns, str) or not isinstance(columns, (list, tuple)):
            raise CleaningError("invalid_params", "Choose the columns to drop as a list of column names.")
        if not columns:
            raise CleaningError("invalid_params", "Choose at least one column to drop.")
        if not all(isinstance(c, str) for c in columns):
            raise CleaningError("invalid_params", "Column names must be text.")
        repeated = sorted({c for c in columns if columns.count(c) > 1})
        if repeated:
            raise CleaningError("invalid_params",
                                f"Each column can be chosen once; repeated: {', '.join(map(quote, repeated))}.")
        existing = set(data.columns)
        missing = [c for c in columns if c not in existing]
        if missing:
            raise CleaningError(
                "invalid_params",
                f"The table has no {verb(len(missing), 'column', 'columns')} {', '.join(map(quote, missing))}.",
            )
        if len(columns) >= data.shape[1]:
            raise CleaningError("invalid_params",
                                "At least one column must remain; you selected every column.")
        chosen = set(columns)
        return {"columns": [c for c in data.columns if c in chosen]}        # Table order

    def resolve(self, params, data):
        columns = list(params["columns"])
        positions = [data.columns.get_loc(c) + 1 for c in columns]
        values = sum(_present(data[c]) for c in columns)
        warnings = []
        if values:
            warnings.append(
                f"{plural(values, 'value')} that {verb(values, 'is', 'are')} not missing will be removed from the "
                "working table. The uploaded file is not changed, and undo or reset brings the "
                f"{verb(len(columns), 'column', 'columns')} back."
            )
        if len(columns) * 2 >= data.shape[1]:
            warnings.append(
                f"This removes {len(columns):,} of {data.shape[1]:,} columns, at least half the table."
            )
        resolved = {"columns": columns, "positions": positions}
        return resolved, tuple(columns), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        columns = list(resolved["columns"])
        absent = [c for c in columns if c not in data.columns]
        if absent:
            raise CleaningError(
                "replay_mismatch",
                f"Cannot drop {', '.join(map(quote, absent))}: the table has no such "
                f"{verb(len(absent), 'column', 'columns')}.",
            )
        values = sum(_present(data[c]) for c in columns)
        # drop() returns a new frame; the deep copy guarantees it shares no
        # writable buffers with the input on pandas 2 (without copy-on-write).
        result = data.drop(columns=columns).copy(deep=True)
        return OperationResult(
            data=result,
            row_ids=np.array(row_ids, dtype=np.int64, copy=True),
            counts={
                "columns_removed": len(columns),
                "cells_removed": len(columns) * len(data),
                "values_removed": values,
            },
        )

    def sample(self, data, resolved, limit):
        return data[list(resolved["columns"])].head(limit).copy()

    def summarize(self, resolved):
        columns = resolved["columns"]
        return f"Drop {plural(len(columns), 'column')}: {', '.join(map(quote, columns))}."


DROP_COLUMNS = register(DropColumns())
