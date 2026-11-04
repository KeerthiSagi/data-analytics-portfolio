"""CleaningSession: preview, confirmation, stale previews, audit, undo, reset, replay, lineage."""

import copy
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.fingerprint import state_fingerprint
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import load_csv

DROP = "drop_columns"


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": pd.array(["1", "2", "3", "4"], dtype=TEXT_DTYPE),
        "name": pd.array(["Ann", "Bo", None, "Di"], dtype=TEXT_DTYPE),
        "city": pd.array(["Oslo", "", "Rome", "Lima"], dtype=TEXT_DTYPE),
        "notes": pd.array([None, None, "late", None], dtype=TEXT_DTYPE),
    })


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def session():
    return CleaningSession(frame(), source_rows=np.array([2, 3, 5, 6]), clock=Clock())


def drop(session, *cols):
    return session.apply(session.preview(DROP, {"columns": list(cols)}), confirmed=True)


# ---- preview and confirmation -------------------------------------

def test_preview_reports_impact_without_changing_anything(session):
    fp, version = session.fingerprint, session.version
    p = session.preview(DROP, {"columns": ["notes", "city"]})
    assert p.plan.label == "Drop columns"
    assert p.plan.affected_columns == ("city", "notes")
    assert (p.impact.rows_before, p.impact.rows_after) == (4, 4)
    assert (p.impact.columns_before, p.impact.columns_after) == (4, 2)
    assert p.impact.counts == {"columns_removed": 2, "cells_removed": 8, "values_removed": 4}
    assert list(p.sample.columns) == ["city", "notes"] and p.sample.index.name == "Original row"
    assert p.sample.index.tolist() == [1, 2, 3, 4]
    assert any("not missing" in w for w in p.plan.warnings)
    assert (session.fingerprint, session.version, session.shape) == (fp, version, (4, 4))
    assert session.audit_log == () and session.steps == ()


def test_invalid_preview_raises_and_changes_nothing(session):
    with pytest.raises(CleaningError):
        session.preview(DROP, {"columns": ["missing"]})
    with pytest.raises(CleaningError):
        session.preview("nope", {})
    assert session.version == 0


@pytest.mark.parametrize("confirmed", [False, None, "yes", 1])
def test_apply_requires_explicit_confirmation(session, confirmed):
    p = session.preview(DROP, {"columns": ["notes"]})
    with pytest.raises(CleaningError) as err:
        session.apply(p, confirmed=confirmed)
    assert err.value.code == "confirmation_required"
    assert session.shape == (4, 4) and session.version == 0
    session.apply(p, confirmed=True)          # The same preview still works once confirmed
    assert session.shape == (4, 3)


def test_apply_without_confirmation_argument_is_rejected(session):
    p = session.preview(DROP, {"columns": ["notes"]})
    with pytest.raises(CleaningError):
        session.apply(p)


# ---- stale previews and duplicate submissions ----------------------

def code_of(session, preview):
    with pytest.raises(CleaningError) as err:
        session.apply(preview, confirmed=True)
    return err.value.code


def test_duplicate_submission_is_rejected(session):
    p = session.preview(DROP, {"columns": ["notes"]})
    session.apply(p, confirmed=True)
    assert code_of(session, p) == "preview_used"
    assert session.shape == (4, 3) and len(session.steps) == 1


def test_preview_is_stale_after_another_step(session):
    first = session.preview(DROP, {"columns": ["notes"]})
    second = session.preview(DROP, {"columns": ["city"]})
    session.apply(second, confirmed=True)
    assert not session.is_current(first)
    assert code_of(session, first) == "stale_preview"
    assert session.columns == ("id", "name", "notes")


def test_preview_is_stale_after_undo_even_if_data_matches(session):
    p = session.preview(DROP, {"columns": ["notes"]})
    drop(session, "city")
    session.undo()
    assert session.fingerprint == session.original_fingerprint      # Same data as when previewed...
    assert code_of(session, p) == "stale_preview"                    # ...but a different state version


def test_preview_is_stale_after_reset(session):
    drop(session, "city")
    p = session.preview(DROP, {"columns": ["notes"]})
    session.reset()
    assert code_of(session, p) == "stale_preview"


def test_preview_from_another_session_is_rejected(session):
    other = CleaningSession(frame())
    p = other.preview(DROP, {"columns": ["notes"]})
    assert code_of(session, p) == "other_session"


def test_tampered_preview_is_rejected(session):
    p = session.preview(DROP, {"columns": ["notes"]})
    forged = copy.copy(p)
    object.__setattr__(forged, "base_fingerprint", "0" * 64)
    assert code_of(session, forged) == "stale_preview"
    unknown = copy.copy(p)
    object.__setattr__(unknown, "token", "f" * 32)
    assert code_of(session, unknown) == "unknown_preview"
    assert session.version == 0


def test_failed_apply_leaves_session_unchanged(session, monkeypatch):
    from refine.operations import drop_columns

    p = session.preview(DROP, {"columns": ["notes"]})

    def broken(*args, **kwargs):
        raise CleaningError("replay_mismatch", "boom")

    monkeypatch.setattr(drop_columns.DropColumns, "apply", broken)
    with pytest.raises(CleaningError):
        session.apply(p, confirmed=True)
    monkeypatch.undo()
    assert session.version == 0 and session.audit_log == ()
    session.apply(p, confirmed=True)          # Not marked as used by the failed attempt
    assert session.shape == (4, 3)


# ---- immutability ---------------------------------------------------

def test_input_frame_is_never_modified():
    df = frame()
    before = df.copy(deep=True)
    s = CleaningSession(df)
    drop(s, "notes")
    s.undo()
    drop(s, "city")
    s.reset()
    pd.testing.assert_frame_equal(df, before)


def test_returned_tables_are_copies(session):
    fp = session.fingerprint
    current, original, ids = session.data, session.original_data, session.row_ids
    current.iloc[0, 0] = "edited"
    original.iloc[0, 0] = "edited"
    ids[0] = 99
    assert session.fingerprint == fp
    assert session.data.iloc[0, 0] == "1" and session.row_ids[0] == 0


def test_editing_the_source_frame_later_does_not_affect_session():
    df = frame()
    s = CleaningSession(df)
    df.iloc[0, 0] = "edited"
    assert s.data.iloc[0, 0] == "1"


# ---- drop columns end to end ----------------------------------------

def test_drop_undo_reset(session):
    original = session.data
    entry = drop(session, "notes", "name")
    assert session.columns == ("id", "city")
    assert entry.event == "apply" and entry.step_number == 1
    session.undo()
    pd.testing.assert_frame_equal(session.data, original)
    assert session.columns == ("id", "name", "city", "notes")         # Original order restored

    drop(session, "notes")
    drop(session, "city")
    assert session.shape == (4, 2)
    session.reset()
    pd.testing.assert_frame_equal(session.data, original)
    assert session.steps == () and not session.is_modified


def test_undo_and_reset_with_nothing_to_do(session):
    with pytest.raises(CleaningError) as err:
        session.undo()
    assert err.value.code == "nothing_to_undo"
    with pytest.raises(CleaningError) as err:
        session.reset()
    assert err.value.code == "nothing_to_reset"
    assert session.audit_log == ()


@pytest.mark.parametrize("limit", [0, 1, 5])
def test_undo_restores_exact_states_with_any_snapshot_limit(limit):
    s = CleaningSession(frame(), snapshot_limit=limit)
    states = [(s.data, s.row_ids, s.fingerprint)]
    for col in ("notes", "city", "name"):
        drop(s, col)
        states.append((s.data, s.row_ids, s.fingerprint))
    for data, ids, fp in reversed(states[:-1]):
        s.undo()
        pd.testing.assert_frame_equal(s.data, data)
        assert s.row_ids.tolist() == ids.tolist() and s.fingerprint == fp
    assert len(s._snapshots) <= limit


def test_snapshot_cache_is_bounded():
    df = pd.DataFrame({f"c{i}": pd.array(["x"], dtype=TEXT_DTYPE) for i in range(10)})
    s = CleaningSession(df, snapshot_limit=2)
    for i in range(8):
        drop(s, f"c{i}")
    assert len(s._snapshots) == 2
    for _ in range(8):
        s.undo()
    assert s.fingerprint == s.original_fingerprint


# ---- audit log -------------------------------------------------------

def test_audit_entries_are_accurate(session):
    before = session.fingerprint
    entry = drop(session, "notes", "city")
    assert entry.sequence == 1 and entry.event == "apply" and entry.operation == DROP
    assert entry.label == "Drop columns"
    assert entry.params == {"columns": ["city", "notes"]}
    assert entry.resolved == {"columns": ["city", "notes"], "positions": [3, 4]}
    assert entry.affected_columns == ("city", "notes")
    assert (entry.rows_before, entry.rows_after, entry.columns_before, entry.columns_after) == (4, 4, 4, 2)
    assert entry.counts == {"columns_removed": 2, "cells_removed": 8, "values_removed": 4}
    assert entry.warnings and entry.fingerprint_before == before
    assert entry.fingerprint_after == session.fingerprint
    assert entry.timestamp == "2026-01-01T00:00:01+00:00"
    json.dumps(entry.to_dict())


def test_audit_distinguishes_apply_undo_and_reset(session):
    drop(session, "notes")
    drop(session, "city")
    undo = session.undo()
    reset = session.reset()
    log = session.audit_log
    assert [e.event for e in log] == ["apply", "apply", "undo", "reset"]
    assert [e.sequence for e in log] == [1, 2, 3, 4]
    assert undo.reverts_sequence == 2 and undo.step_number == 2 and undo.label == "Undo: Drop columns"
    assert undo.fingerprint_before == log[1].fingerprint_after
    assert undo.fingerprint_after == log[1].fingerprint_before
    assert reset.operation is None and reset.counts == {"steps_discarded": 1}
    assert reset.fingerprint_after == session.original_fingerprint
    assert (reset.columns_before, reset.columns_after) == (3, 4)


def test_audit_log_is_append_only(session):
    drop(session, "notes")
    log = session.audit_log
    assert isinstance(log, tuple)
    session.undo()
    assert session.audit_log[: len(log)] == log


# ---- recipe and replay ----------------------------------------------

def test_recipe_is_deterministic_and_has_no_timestamps():
    def run(clock_start):
        clock = Clock()
        clock.now = clock_start
        s = CleaningSession(frame(), clock=clock)
        drop(s, "notes")
        drop(s, "city")
        return s

    a = run(datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = run(datetime(2030, 6, 1, tzinfo=timezone.utc))
    assert a.recipe_json() == b.recipe_json()
    assert a.audit_log[0].timestamp != b.audit_log[0].timestamp
    assert "time" not in a.recipe_json()
    recipe = a.recipe()
    assert [s["operation"] for s in recipe["steps"]] == [DROP, DROP]
    assert recipe["steps"][0]["resolved"] == {"columns": ["notes"], "positions": [4]}
    assert recipe["result"]["columns"] == ["id", "name"]
    assert json.loads(a.recipe_json(indent=2)) == recipe


def test_recipe_only_holds_active_steps(session):
    drop(session, "notes")
    drop(session, "city")
    session.undo()
    assert len(session.recipe()["steps"]) == 1


def test_replay_reproduces_current_state(session):
    drop(session, "notes")
    drop(session, "city")
    data, ids = session.replay()
    pd.testing.assert_frame_equal(data, session.data)
    assert ids.tolist() == session.row_ids.tolist()
    check = session.verify_replay()
    assert check.ok and check.steps == 2 and check.replayed_fingerprint == session.fingerprint


def test_replay_on_a_fresh_copy_of_the_data(sample_bytes):
    ds = load_csv(sample_bytes)
    s = CleaningSession(ds.data)
    drop(s, "email", "age")
    recipe = json.loads(s.recipe_json())                   # Through JSON and back
    again = load_csv(sample_bytes)
    data, ids = replay_recipe(again.data, recipe)
    pd.testing.assert_frame_equal(data, s.data)
    data2, _ = replay_recipe(again.data, recipe)
    pd.testing.assert_frame_equal(data, data2)              # Deterministic


def test_replay_uses_frozen_values_not_recomputed_ones(session):
    drop(session, "notes")
    recipe = session.recipe()
    recipe["steps"][0]["resolved"]["columns"] = ["city"]    # A different frozen value...
    with pytest.raises(CleaningError) as err:
        session.replay(recipe)                              # ...is used as recorded and caught
    assert err.value.code == "replay_mismatch"


def test_replay_rejects_other_data_and_bad_recipes(session):
    drop(session, "notes")
    recipe = session.recipe()
    other = frame().assign(id=pd.array(["9", "2", "3", "4"], dtype=TEXT_DTYPE))
    with pytest.raises(CleaningError, match="different table"):
        replay_recipe(other, recipe)
    for bad in ({}, {**recipe, "recipe_version": 99}, {**recipe, "fingerprint_version": "x"},
                {**recipe, "steps": None}):
        with pytest.raises(CleaningError):
            replay_recipe(frame(), bad)
    tampered = {**recipe, "result": {"fingerprint": "0" * 64}}
    with pytest.raises(CleaningError, match="did not reproduce"):
        replay_recipe(frame(), tampered)


def test_empty_recipe_replays_to_original(session):
    data, ids = session.replay()
    pd.testing.assert_frame_equal(data, frame())
    assert session.verify_replay().ok


# ---- row lineage -----------------------------------------------------

def test_row_ids_start_as_original_positions(session):
    assert session.row_ids.tolist() == [0, 1, 2, 3]
    assert session.source_rows.tolist() == [2, 3, 5, 6]


def test_row_ids_are_not_columns_and_survive_steps(session):
    drop(session, "notes")
    assert "notes" not in session.columns
    assert not any("row" in c.lower() and c not in frame().columns for c in session.columns)
    assert session.row_ids.tolist() == [0, 1, 2, 3]
    session.undo()
    assert session.row_ids.tolist() == [0, 1, 2, 3]
    _, ids = session.replay()
    assert ids.tolist() == [0, 1, 2, 3]


def test_user_columns_named_like_internal_ids_are_untouched():
    df = pd.DataFrame({"row_id": pd.array(["a", "b"], dtype=TEXT_DTYPE),
                       "_refine_row": pd.array(["c", "d"], dtype=TEXT_DTYPE),
                       "x": pd.array(["1", "2"], dtype=TEXT_DTYPE)})
    s = CleaningSession(df)
    drop(s, "x")
    assert s.columns == ("row_id", "_refine_row")
    assert s.data["row_id"].tolist() == ["a", "b"]
    assert s.row_ids.tolist() == [0, 1]


def test_lineage_is_part_of_the_state_fingerprint(session):
    assert session.fingerprint == state_fingerprint(session.data, session.row_ids)


def test_non_range_index_is_normalized():
    df = frame()
    df.index = [10, 20, 30, 40]
    s = CleaningSession(df)
    assert list(s.data.index) == [0, 1, 2, 3]
    assert s.row_ids.tolist() == [0, 1, 2, 3]
