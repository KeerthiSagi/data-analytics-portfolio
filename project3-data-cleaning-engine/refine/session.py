"""CleaningSession: the working copy of a dataset and its step history.

Lifecycle of a change:

    preview = session.preview("drop_columns", {"columns": ["notes"]})
    ...show preview.plan, preview.impact, preview.sample to the user...
    session.apply(preview, confirmed=True)      # Only with explicit confirmation
    session.undo()                              # Exact previous state
    session.reset()                             # Original parsed table
    session.recipe()                            # Reproducible, JSON-compatible

Guarantees
----------
* The original parsed table is copied once and never changed.
* Nothing changes before `apply(..., confirmed=True)`. A preview is bound
  to the exact state it was made from (state version + fingerprint) and
  can be applied once; any change in between (another step, undo, reset)
  makes it stale and it is rejected.
* Operations return new tables. Callers only ever receive copies, so
  editing a returned table cannot change the session.
* Undo restores the exact previous state. It uses a small cache of
  earlier states when available and otherwise replays the recorded steps
  from the original; either way the result is checked against the
  fingerprint recorded when the step was applied. The cache is bounded by
  a number of states and by a memory budget, so large tables keep fewer
  states (or none) and rely on replay.
* Replay applies each recorded step with its frozen resolved values,
  never recomputing them, and checks every intermediate fingerprint.

Row lineage
-----------
Every row of the original table gets an internal identity: its 0-based
position in the parsed table. Identities are kept beside the table
(`row_ids`), never as a column, so they cannot collide with user columns
and never appear in exported data. Operations carry them through; undo
and replay restore them with the table.

The audit log records apply, undo and reset events with timestamps. The
recipe holds only the active applied steps and no timestamps, so the same
steps on the same data always give the same recipe.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd

from refine.config import (
    ENGINE_NAME,
    ENGINE_VERSION,
    PREVIEW_SAMPLE_ROWS,
    SNAPSHOT_LIMIT,
    SNAPSHOT_MEMORY_BUDGET_BYTES,
)
from refine.errors import CleaningError
from refine.fingerprint import FINGERPRINT_VERSION, state_fingerprint
from refine.operations import Impact, Plan, Preview, Step, canonical_json, get_operation, jsonable

RECIPE_FORMAT = "refine-recipe"
RECIPE_VERSION = 1

EventKind = Literal["apply", "undo", "reset"]


# ------------------------------------------------------------------
# Records
# ------------------------------------------------------------------

@dataclass(frozen=True)
class AppliedStep:
    """One active step: what was planned and the states it connects."""

    plan: Plan
    counts: dict[str, int]
    fingerprint_before: str
    fingerprint_after: str
    audit_sequence: int

    def recipe_entry(self) -> dict[str, Any]:
        return {
            **self.plan.step.to_dict(),
            "resolved": jsonable(self.plan.resolved),
            "fingerprint_before": self.fingerprint_before,
            "fingerprint_after": self.fingerprint_after,
        }


@dataclass(frozen=True)
class AuditEntry:
    """One event in the session's history. Append-only."""

    sequence: int
    timestamp: str                       # ISO 8601, UTC
    event: EventKind
    operation: str | None                # None for reset
    label: str
    summary: str
    params: dict[str, Any]
    resolved: dict[str, Any]
    affected_columns: tuple[str, ...]
    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int
    counts: dict[str, int]
    warnings: tuple[str, ...]
    fingerprint_before: str
    fingerprint_after: str
    step_number: int | None = None       # Position in the step list (apply / undo)
    reverts_sequence: int | None = None  # Undo: the apply event it reverted

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence, "timestamp": self.timestamp, "event": self.event,
            "operation": self.operation, "label": self.label, "summary": self.summary,
            "params": jsonable(self.params), "resolved": jsonable(self.resolved),
            "affected_columns": list(self.affected_columns),
            "rows_before": self.rows_before, "rows_after": self.rows_after,
            "columns_before": self.columns_before, "columns_after": self.columns_after,
            "counts": dict(self.counts), "warnings": list(self.warnings),
            "fingerprint_before": self.fingerprint_before, "fingerprint_after": self.fingerprint_after,
            "step_number": self.step_number, "reverts_sequence": self.reverts_sequence,
        }


@dataclass(frozen=True)
class SnapshotStats:
    """What the undo cache holds right now."""

    count: int
    bytes: int
    limit: int
    budget_bytes: int


@dataclass(frozen=True)
class ReplayCheck:
    ok: bool
    steps: int
    expected_fingerprint: str
    replayed_fingerprint: str
    message: str


@dataclass(frozen=True, eq=False)
class _State:
    data: pd.DataFrame = field(repr=False)
    row_ids: np.ndarray = field(repr=False)
    fingerprint: str


def _readonly(ids: np.ndarray) -> np.ndarray:
    ids = np.array(ids, dtype=np.int64, copy=True)
    ids.setflags(write=False)
    return ids


def _state_bytes(state: _State) -> int:
    return int(state.data.memory_usage(deep=True, index=True).sum()) + state.row_ids.nbytes


def _make_state(data: pd.DataFrame, row_ids: np.ndarray) -> _State:
    ids = _readonly(row_ids)
    if len(ids) != len(data):
        raise CleaningError("replay_mismatch", "Row identities do not match the table's rows.")
    return _State(data, ids, state_fingerprint(data, ids))


# ------------------------------------------------------------------
# Replay (also usable without a session)
# ------------------------------------------------------------------

def _check_recipe(recipe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(recipe, Mapping) or recipe.get("format") != RECIPE_FORMAT:
        raise CleaningError("replay_mismatch", "This is not a REFINE recipe.")
    if recipe.get("recipe_version") != RECIPE_VERSION:
        raise CleaningError("replay_mismatch",
                            f"Recipe version {recipe.get('recipe_version')!r} is not supported.")
    if recipe.get("fingerprint_version") != FINGERPRINT_VERSION:
        raise CleaningError("replay_mismatch", "The recipe was fingerprinted by a different REFINE version.")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        raise CleaningError("replay_mismatch", "The recipe has no step list.")
    return steps


def _replay_steps(state: _State, entries: list[Mapping[str, Any]], first_number: int = 1) -> _State:
    """Apply recorded steps with their frozen resolved values, checking each fingerprint."""
    for number, entry in enumerate(entries, start=first_number):
        before = entry.get("fingerprint_before")
        if before is not None and before != state.fingerprint:
            raise CleaningError("replay_mismatch",
                                f"Step {number} expects a different table than the one it was replayed on.")
        operation = get_operation(entry["operation"])
        result = operation.apply(state.data, state.row_ids, entry["resolved"])
        state = _make_state(result.data, result.row_ids)
        after = entry.get("fingerprint_after")
        if after is not None and after != state.fingerprint:
            raise CleaningError("replay_mismatch",
                                f"Replaying step {number} ({operation.label}) did not reproduce the recorded result.")
    return state


def replay_recipe(original: pd.DataFrame, recipe: Mapping[str, Any]) -> tuple[pd.DataFrame, np.ndarray]:
    """Rebuild the cleaned table from the original parsed table and a recipe.

    Raises CleaningError("replay_mismatch") if the table is not the one the
    recipe was made for, or if any step does not reproduce its recorded
    result.
    """
    steps = _check_recipe(recipe)
    original = original.reset_index(drop=True)
    state = _make_state(original, np.arange(len(original), dtype=np.int64))
    expected_source = recipe.get("source", {}).get("fingerprint")
    if expected_source != state.fingerprint:
        raise CleaningError("replay_mismatch", "The recipe was made for a different table.")
    state = _replay_steps(state, steps)
    expected = recipe.get("result", {}).get("fingerprint")
    if expected is not None and expected != state.fingerprint:
        raise CleaningError("replay_mismatch", "Replaying the recipe did not reproduce the recorded result.")
    return state.data.copy(deep=True), np.array(state.row_ids, copy=True)


# ------------------------------------------------------------------
# Session
# ------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CleaningSession:
    """The original table, the current table, and every step between them."""

    def __init__(
        self,
        data: pd.DataFrame,
        *,
        source_rows: np.ndarray | None = None,
        snapshot_limit: int = SNAPSHOT_LIMIT,
        snapshot_budget_bytes: int = SNAPSHOT_MEMORY_BUDGET_BYTES,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(data, pd.DataFrame):
            raise TypeError("CleaningSession needs a pandas DataFrame.")
        original = data.reset_index(drop=True).copy(deep=True)
        self.session_id = uuid.uuid4().hex
        self._original = _make_state(original, np.arange(len(original), dtype=np.int64))
        self._current = self._original
        self._source_rows = None if source_rows is None else _readonly(source_rows)
        if self._source_rows is not None and len(self._source_rows) != len(original):
            raise ValueError("source_rows must have one entry per row.")
        self._steps: list[AppliedStep] = []
        self._audit: list[AuditEntry] = []
        self._version = 0
        self._snapshot_limit = max(0, int(snapshot_limit))
        self._snapshot_budget = max(0, int(snapshot_budget_bytes))
        self._snapshot_bytes: dict[int, int] = {}
        # Key: number of active steps the state follows (0 is the original, always kept).
        self._snapshots: OrderedDict[int, _State] = OrderedDict()
        self._issued: dict[str, int] = {}      # Preview token -> state version it was made for
        self._used: set[str] = set()
        self._clock = clock

    # ---- read-only views (always copies) ----------------------------

    @property
    def version(self) -> int:
        """Increases on every change of state (apply, undo, reset); never reused."""
        return self._version

    @property
    def fingerprint(self) -> str:
        return self._current.fingerprint

    @property
    def original_fingerprint(self) -> str:
        return self._original.fingerprint

    @property
    def data(self) -> pd.DataFrame:
        """A copy of the current table (user columns only, no lineage)."""
        return self._current.data.copy(deep=True)

    @property
    def original_data(self) -> pd.DataFrame:
        return self._original.data.copy(deep=True)

    @property
    def row_ids(self) -> np.ndarray:
        """Original row identity (0-based parsed row) of each current row."""
        return np.array(self._current.row_ids, copy=True)

    @property
    def source_rows(self) -> np.ndarray | None:
        """File line (CSV) or sheet row (XLSX) of each current row, if known."""
        if self._source_rows is None:
            return None
        return self._source_rows[self._current.row_ids]

    @property
    def shape(self) -> tuple[int, int]:
        return self._current.data.shape

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self._current.data.columns)

    @property
    def steps(self) -> tuple[AppliedStep, ...]:
        return tuple(self._steps)

    @property
    def audit_log(self) -> tuple[AuditEntry, ...]:
        return tuple(self._audit)

    @property
    def can_undo(self) -> bool:
        return bool(self._steps)

    @property
    def snapshot_stats(self) -> SnapshotStats:
        return SnapshotStats(len(self._snapshots), sum(self._snapshot_bytes.values()),
                             self._snapshot_limit, self._snapshot_budget)

    @property
    def is_modified(self) -> bool:
        return self._current.fingerprint != self._original.fingerprint

    # ---- preview and apply -------------------------------------------

    def preview(self, operation: str, params: Mapping[str, Any] | None = None) -> Preview:
        """Plan a step against the current state and report its effect. Changes nothing."""
        op = get_operation(operation)
        state = self._current
        plan = op.plan(params, state.data)
        result = op.apply(state.data, state.row_ids, plan.resolved)
        sample = op.sample(state.data, plan.resolved, PREVIEW_SAMPLE_ROWS)
        # The table keeps a RangeIndex, so the sample's index is row positions.
        positions = np.asarray(sample.index, dtype=np.int64)
        sample.index = pd.Index(state.row_ids[positions] + 1, name="Original row")
        token = uuid.uuid4().hex
        self._issued[token] = self._version
        return Preview(
            token=token,
            session_id=self.session_id,
            base_version=self._version,
            base_fingerprint=state.fingerprint,
            plan=plan,
            impact=Impact(
                rows_before=state.data.shape[0], rows_after=result.data.shape[0],
                columns_before=state.data.shape[1], columns_after=result.data.shape[1],
                counts=dict(result.counts),
            ),
            sample=sample,
        )

    def check_preview(self, preview: Preview) -> None:
        """Raise CleaningError if this preview cannot be applied to the current state."""
        if not isinstance(preview, Preview) or preview.session_id != self.session_id:
            raise CleaningError("other_session",
                                "This preview was made for a different dataset. Preview the step again.")
        if preview.token in self._used:
            raise CleaningError("preview_used", "This preview has already been applied.")
        if preview.token not in self._issued:
            raise CleaningError("unknown_preview", "This preview was not made by this session.")
        if (self._issued[preview.token] != self._version or preview.base_version != self._version
                or preview.base_fingerprint != self._current.fingerprint):
            raise CleaningError("stale_preview",
                                "The data has changed since this preview was made. Preview the step again.")

    def is_current(self, preview: Preview | None) -> bool:
        if preview is None:
            return False
        try:
            self.check_preview(preview)
        except CleaningError:
            return False
        return True

    def apply(self, preview: Preview, *, confirmed: bool = False) -> AuditEntry:
        """Apply a previewed step. Requires confirmed=True; the session is unchanged on any error."""
        if confirmed is not True:
            raise CleaningError("confirmation_required", "Confirm the step before it is applied.")
        self.check_preview(preview)
        plan = preview.plan
        before = self._current
        result = get_operation(plan.operation).apply(before.data, before.row_ids, plan.resolved)
        after = _make_state(result.data, result.row_ids)

        # Commit: nothing above has changed the session.
        self._used.add(preview.token)
        self._remember(len(self._steps), before)
        self._current = after
        self._version += 1
        entry = self._log(
            "apply", plan.operation, plan.label, plan.summary, plan.step.params, plan.resolved,
            plan.affected_columns, before, after, result.counts, plan.warnings,
            step_number=len(self._steps) + 1,
        )
        self._steps.append(AppliedStep(plan, dict(result.counts), before.fingerprint, after.fingerprint,
                                       entry.sequence))
        return entry

    # ---- undo, reset, replay ---------------------------------------

    def undo(self) -> AuditEntry:
        """Restore the state before the most recent active step."""
        if not self._steps:
            raise CleaningError("nothing_to_undo", "There is no applied step to undo.")
        last = self._steps[-1]
        target = len(self._steps) - 1
        restored = self._state_after(target)
        if restored.fingerprint != last.fingerprint_before:
            raise CleaningError("replay_mismatch",
                                "The previous state could not be restored exactly. Nothing was changed.")
        before = self._current
        self._steps.pop()
        for k in [k for k in self._snapshots if k >= target]:
            self._forget(k)
        self._current = restored
        self._version += 1
        plan = last.plan
        return self._log(
            "undo", plan.operation, f"Undo: {plan.label}", f"Undid step {target + 1}: {plan.summary}",
            plan.step.params, plan.resolved, plan.affected_columns, before, restored, {}, (),
            step_number=target + 1, reverts_sequence=last.audit_sequence,
        )

    def reset(self) -> AuditEntry:
        """Discard every active step and return to the original parsed table."""
        if not self._steps:
            raise CleaningError("nothing_to_reset", "No step has been applied; the data is already the original.")
        before = self._current
        discarded = len(self._steps)
        self._steps.clear()
        self._snapshots.clear()
        self._snapshot_bytes.clear()
        self._current = self._original
        self._version += 1
        return self._log(
            "reset", None, "Reset to original", f"Discarded {discarded} applied step(s).",
            {}, {}, (), before, self._original, {"steps_discarded": discarded}, (),
        )

    def recipe(self) -> dict[str, Any]:
        """The active steps as a deterministic, JSON-compatible recipe (no timestamps)."""
        original, current = self._original, self._current
        return {
            "format": RECIPE_FORMAT,
            "recipe_version": RECIPE_VERSION,
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "source": {"fingerprint": original.fingerprint, "rows": original.data.shape[0],
                       "columns": list(original.data.columns)},
            "steps": [s.recipe_entry() for s in self._steps],
            "result": {"fingerprint": current.fingerprint, "rows": current.data.shape[0],
                       "columns": list(current.data.columns)},
        }

    def recipe_json(self, indent: int | None = None) -> str:
        if indent is None:
            return canonical_json(self.recipe())
        import json
        return json.dumps(self.recipe(), sort_keys=True, indent=indent, ensure_ascii=False)

    def replay(self, recipe: Mapping[str, Any] | None = None) -> tuple[pd.DataFrame, np.ndarray]:
        """Rebuild a table from this session's original table and a recipe (default: its own)."""
        return replay_recipe(self._original.data, self.recipe() if recipe is None else recipe)

    def verify_replay(self) -> ReplayCheck:
        """Replay the active steps from the original and compare with the current state."""
        try:
            data, ids = self.replay()
        except CleaningError as exc:
            return ReplayCheck(False, len(self._steps), self.fingerprint, "", exc.message)
        replayed = state_fingerprint(data, ids)
        ok = replayed == self.fingerprint
        message = (f"Replaying {len(self._steps)} step(s) from the original reproduced the current data exactly."
                   if ok else "Replaying the steps produced different data.")
        return ReplayCheck(ok, len(self._steps), self.fingerprint, replayed, message)

    # ---- comparison (read-only) ------------------------------------

    def compare(self, **reports):
        """Compare the original table with the current one (see refine/compare.py).

        Reads the stored tables without copying them and matches rows through
        the session's row lineage. Changes nothing: not the tables, the
        history, the version or the fingerprints. `reports` are passed on to
        compare_datasets (cached profiles, quality reports, validity rules).
        """
        from refine.compare import compare_datasets

        return compare_datasets(self._original.data, self._current.data, row_ids=self._current.row_ids, **reports)

    # ---- internals ---------------------------------------------------

    def _remember(self, k: int, state: _State) -> None:
        """Cache a state for undo, within the count limit and the memory budget (oldest evicted first)."""
        if k == 0 or self._snapshot_limit == 0:
            return
        size = _state_bytes(state)
        if size > self._snapshot_budget:
            return                      # Too large to cache: undo replays instead
        self._snapshots[k] = state
        self._snapshot_bytes[k] = size
        self._snapshots.move_to_end(k)
        while (len(self._snapshots) > self._snapshot_limit
               or sum(self._snapshot_bytes.values()) > self._snapshot_budget):
            self._forget(next(iter(self._snapshots)))

    def _forget(self, k: int) -> None:
        self._snapshots.pop(k, None)
        self._snapshot_bytes.pop(k, None)

    def _state_after(self, k: int) -> _State:
        """The state after the first k active steps: cached, or replayed from the nearest earlier state."""
        if k == 0:
            return self._original
        if k in self._snapshots:
            self._snapshots.move_to_end(k)
            return self._snapshots[k]
        start = max((j for j in self._snapshots if j < k), default=0)
        state = self._original if start == 0 else self._snapshots[start]
        entries = [s.recipe_entry() for s in self._steps[start:k]]
        return _replay_steps(state, entries, first_number=start + 1)

    def _log(self, event: EventKind, operation, label, summary, params, resolved, affected,
             before: _State, after: _State, counts, warnings, *, step_number=None,
             reverts_sequence=None) -> AuditEntry:
        entry = AuditEntry(
            sequence=len(self._audit) + 1,
            timestamp=self._clock().astimezone(timezone.utc).isoformat(timespec="seconds"),
            event=event, operation=operation, label=label, summary=summary,
            params=jsonable(params), resolved=jsonable(resolved), affected_columns=tuple(affected),
            rows_before=before.data.shape[0], rows_after=after.data.shape[0],
            columns_before=before.data.shape[1], columns_after=after.data.shape[1],
            counts=dict(counts), warnings=tuple(warnings),
            fingerprint_before=before.fingerprint, fingerprint_after=after.fingerprint,
            step_number=step_number, reverts_sequence=reverts_sequence,
        )
        self._audit.append(entry)
        return entry


__all__ = ["AppliedStep", "AuditEntry", "CleaningSession", "ReplayCheck", "SnapshotStats", "Step", "replay_recipe",
           "RECIPE_FORMAT", "RECIPE_VERSION"]
