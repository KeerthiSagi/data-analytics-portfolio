"""Building blocks shared by every cleaning operation.

An operation turns user settings into a change of the working table in
three separate stages, so nothing is changed until the user confirms:

1. validate(params, data): check the settings against the current table
   and return them in a normalized form (or raise CleaningError).
2. resolve(params, data): work out every value the change depends on
   (which columns, which rows, which replacement values) and freeze them
   in `Plan.resolved`, together with warnings about possible data loss.
3. apply(data, row_ids, resolved): perform the change using only the
   frozen resolved values, and return a *new* table. The input table is
   never modified.

Because apply reads only `resolved`, replaying a recorded step later
reproduces exactly the same change: no value is silently recomputed from
data that may have changed in between.

All settings and resolved values are JSON-compatible, so a step can be
written to a recipe and read back unchanged.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from refine.errors import CleaningError


def canonical_json(value: Any) -> str:
    """Compact JSON with sorted keys: the same value always gives the same text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def jsonable(value: Any) -> Any:
    """A JSON-compatible deep copy (tuples become lists, numpy scalars Python ones).

    Raises CleaningError if the value cannot be represented in JSON.
    """
    def convert(v: Any) -> Any:
        if isinstance(v, Mapping):
            return {str(k): convert(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [convert(x) for x in v]
        if isinstance(v, np.generic):
            return v.item()
        return v

    try:
        return json.loads(canonical_json(convert(value)))
    except (TypeError, ValueError) as exc:
        raise CleaningError("invalid_params", f"Step settings must be plain values: {exc}") from exc


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """What the user asked for: an operation and its settings."""

    operation: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "params": jsonable(self.params)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        return cls(str(data["operation"]), jsonable(data.get("params", {})))


@dataclass(frozen=True)
class Plan:
    """A validated step with every value it depends on resolved and frozen."""

    step: Step
    label: str                          # Operation name shown to users
    resolved: dict[str, Any]
    affected_columns: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    summary: str = ""                   # One sentence describing the change

    @property
    def operation(self) -> str:
        return self.step.operation

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.step.to_dict(),
            "label": self.label,
            "resolved": jsonable(self.resolved),
            "affected_columns": list(self.affected_columns),
            "warnings": list(self.warnings),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class Impact:
    """Shape of the table before and after, plus operation-specific counts."""

    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_before": self.rows_before, "rows_after": self.rows_after,
            "columns_before": self.columns_before, "columns_after": self.columns_after,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True, eq=False)
class Preview:
    """The expected effect of a plan on one exact state of the table.

    A preview can be applied once, and only while the session is still in
    the state it was made from (`base_version` and `base_fingerprint`).
    """

    token: str
    session_id: str
    base_version: int
    base_fingerprint: str
    plan: Plan
    impact: Impact
    sample: pd.DataFrame = field(repr=False)     # Affected data, indexed by original row number


@dataclass(frozen=True, eq=False)
class OperationResult:
    """A new table, the row identities it holds, and what changed."""

    data: pd.DataFrame
    row_ids: np.ndarray
    counts: dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------------
# Operation interface
# ------------------------------------------------------------------

class Operation(ABC):
    """One kind of cleaning step. Subclasses are stateless."""

    op_id: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def validate(self, params: Mapping[str, Any], data: pd.DataFrame) -> dict[str, Any]:
        """Normalized settings, or CleaningError("invalid_params", ...)."""

    @abstractmethod
    def resolve(self, params: dict[str, Any], data: pd.DataFrame) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
        """(resolved values, affected columns, warnings) for validated settings."""

    @abstractmethod
    def apply(self, data: pd.DataFrame, row_ids: np.ndarray, resolved: Mapping[str, Any]) -> OperationResult:
        """A new table built from `data` using only `resolved`. Must not modify its inputs."""

    @abstractmethod
    def sample(self, data: pd.DataFrame, resolved: Mapping[str, Any], limit: int) -> pd.DataFrame:
        """Up to `limit` rows of the data the step affects, in table order."""

    def summarize(self, resolved: Mapping[str, Any]) -> str:
        return self.label

    def plan(self, params: Mapping[str, Any] | None, data: pd.DataFrame) -> Plan:
        if params is not None and not isinstance(params, Mapping):
            raise CleaningError("invalid_params", "Step settings must be a mapping of names to values.")
        normalized = jsonable(self.validate(dict(params or {}), data))
        resolved, affected, warnings = self.resolve(normalized, data)
        resolved = jsonable(resolved)
        return Plan(
            step=Step(self.op_id, normalized),
            label=self.label,
            resolved=resolved,
            affected_columns=tuple(affected),
            warnings=tuple(warnings),
            summary=self.summarize(resolved),
        )
