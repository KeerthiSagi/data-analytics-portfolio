"""History and recipe downloads: built from the session's own records, valid JSON, replayable recipes."""

import json
import math

import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.export import (
    HISTORY_FORMAT,
    HISTORY_VERSION,
    JSON_MIME,
    history_document,
    history_export,
    json_bytes,
    json_ready,
    recipe_bytes,
    recipe_export,
)
from refine.loader import load_file
from refine.session import RECIPE_FORMAT, RECIPE_VERSION, CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text(*[str(i) for i in range(12)]),
        "amount": text("-50", "1", "2", "NA", "3", "4", "5", "6", "7", "8", "9", "100"),
        "city": text(" A", "B", "B", "D", "E", "F", "G", "H", "I", "J", "K", "L"),
    })


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


@pytest.fixture
def sample():
    data = (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes()
    ds = load_file("messy_customers.csv", data)
    return ds, CleaningSession(ds.data, source_rows=ds.source_rows)


def parse(data: bytes):
    return json.loads(data.decode("utf-8"), parse_constant=lambda c: pytest.fail(f"non-standard JSON {c}"))


# ---- history --------------------------------------------------------------

def test_empty_history_is_a_valid_document():
    s = CleaningSession(frame())
    doc = parse(history_export(s).data)
    assert doc["format"] == HISTORY_FORMAT and doc["history_version"] == HISTORY_VERSION
    assert doc["active_steps"] == [] and doc["events"] == []
    assert doc["state"]["steps_applied"] == 0
    assert doc["state"]["original"]["fingerprint"] == doc["state"]["current"]["fingerprint"] == s.fingerprint


def test_single_step():
    s = CleaningSession(frame())
    entry = apply(s, "trim_whitespace", {"columns": ["city"]})
    doc = parse(history_export(s).data)
    (step,) = doc["active_steps"]
    assert step["step"] == 1 and step["operation"] == "trim_whitespace" and step["columns"] == ["city"]
    assert step["applied_at"] == entry.timestamp and step["audit_sequence"] == entry.sequence
    assert step["counts"] == dict(s.steps[0].counts)
    assert step["params"] == s.steps[0].plan.step.params
    (event,) = doc["events"]
    assert event["status"] == "active" and event["event"] == "apply" and event["timestamp"] == entry.timestamp


def test_multiple_steps_in_order_with_undo_and_reset():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {"columns": ["city"]})
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    s.undo()
    apply(s, "convert_type", {"columns": ["amount"], "target": "integer", "on_error": "coerce"})
    doc = history_document(s)
    assert [x["operation"] for x in doc["active_steps"]] == ["remove_duplicates", "convert_type"]
    assert [x["step"] for x in doc["active_steps"]] == [1, 2]
    assert [(e["event"], e["status"]) for e in doc["events"]] == [
        ("apply", "active"), ("apply", "undone"), ("undo", None), ("apply", "active")]
    assert doc["events"][1]["undone_by"] == 3 and doc["events"][2]["reverts_sequence"] == 2
    assert [e["timestamp"] for e in doc["events"]] == [e.timestamp for e in s.audit_log]
    s.reset()
    doc = history_document(s)
    assert doc["active_steps"] == []
    assert [e["status"] for e in doc["events"]] == ["discarded by reset", "undone", None, "discarded by reset",
                                                     None]
    assert doc["events"][-1]["event"] == "reset" and doc["events"][-1]["counts"] == {"steps_discarded": 2}


def test_history_matches_the_audit_log_exactly():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {"columns": ["city"]})
    apply(s, "drop_columns", {"columns": ["city"]})
    doc = history_document(s)
    for event, entry in zip(doc["events"], s.audit_log):
        for key in ("sequence", "timestamp", "event", "operation", "label", "summary", "rows_before",
                    "rows_after", "columns_before", "columns_after", "step_number"):
            assert event[key] == getattr(entry, key)
        assert event["params"] == entry.params and event["counts"] == entry.counts
        assert event["columns"] == list(entry.affected_columns) and event["warnings"] == list(entry.warnings)


def test_history_holds_no_snapshots_data_or_fingerprints_per_step():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {"columns": ["city"]})
    raw = history_export(s).data.decode("utf-8")
    doc = json.loads(raw)
    for item in doc["active_steps"] + doc["events"]:
        assert "resolved" not in item and "fingerprint_before" not in item and "fingerprint_after" not in item
    assert "snapshot" not in raw and "row_ids" not in raw and "raw_bytes" not in raw
    assert "100" not in raw                            # No cell values of the table


def test_history_with_source_and_file_name(sample):
    ds, s = sample
    exp = history_export(s, source=ds.source)
    doc = parse(exp.data)
    assert exp.file_name == "messy_customers_history.json" and exp.mime == JSON_MIME
    assert doc["source"] == {"file_name": "messy_customers.csv", "file_format": "csv", "sha256": ds.source.sha256,
                             "rows": 500, "columns": 9}


def test_history_export_changes_nothing():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {"columns": ["city"]})
    before = (s.version, s.fingerprint, len(s.audit_log), s.recipe_json(), s.snapshot_stats, s.row_ids.tolist())
    history_export(s)
    assert (s.version, s.fingerprint, len(s.audit_log), s.recipe_json(), s.snapshot_stats,
            s.row_ids.tolist()) == before


# ---- recipe ---------------------------------------------------------------

def test_empty_recipe():
    s = CleaningSession(frame())
    recipe = parse(recipe_export(s).data)
    assert recipe == s.recipe() and recipe["steps"] == []
    assert recipe["format"] == RECIPE_FORMAT and recipe["recipe_version"] == RECIPE_VERSION
    data, _ = replay_recipe(frame(), recipe)
    pd.testing.assert_frame_equal(data, frame())


def test_recipe_is_the_session_recipe_unchanged(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    exp = recipe_export(s, source=ds.source)
    assert exp.file_name == "messy_customers_recipe.json"
    assert parse(exp.data) == s.recipe()
    assert exp.data.decode("utf-8") == s.recipe_json(indent=2) + "\n"   # Same text the History page shows
    assert recipe_bytes(s) == exp.data


def test_exported_recipe_replays_to_the_cleaned_data(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["age", "annual_spend"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    apply(s, "trim_whitespace", {"columns": ["full_name", "country"]})
    apply(s, "treat_outliers", {"column": "age", "method": "iqr", "treatment": "cap"})
    apply(s, "drop_columns", {"columns": ["segment"]})
    recipe = parse(recipe_bytes(s))
    assert [x["operation"] for x in recipe["steps"]] == ["remove_duplicates", "standardize_missing", "convert_type",
                                                          "trim_whitespace", "treat_outliers", "drop_columns"]
    data, ids = replay_recipe(ds.data, recipe)             # Replayed on the parsed original file
    pd.testing.assert_frame_equal(data, s.data)
    assert ids.tolist() == s.row_ids.tolist()
    reloaded = load_file("messy_customers.csv", (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes())
    data2, _ = replay_recipe(reloaded.data, recipe)          # And on the same file loaded again
    pd.testing.assert_frame_equal(data2, s.data)


def test_frozen_outlier_boundaries_are_exported_exactly():
    s = CleaningSession(frame())
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    apply(s, "convert_type", {"columns": ["amount"], "target": "decimal"})
    apply(s, "treat_outliers", {"column": "amount", "method": "zscore", "threshold": 1.5, "treatment": "cap"})
    frozen = s.steps[-1].plan.resolved
    exported = parse(recipe_bytes(s))["steps"][-1]["resolved"]
    assert exported == frozen
    for key in ("lower_bound", "upper_bound"):           # Fractional boundaries, exact to the last bit
        assert isinstance(frozen[key], float) and math.isfinite(frozen[key])
        assert exported[key].hex() == frozen[key].hex()
    assert exported["statistics"] == frozen["statistics"] and exported["outliers"] == 2
    data, _ = replay_recipe(frame(), parse(recipe_bytes(s)))
    pd.testing.assert_frame_equal(data, s.data)


def test_exported_recipe_refuses_other_data():
    s = CleaningSession(frame())
    apply(s, "trim_whitespace", {"columns": ["city"]})
    recipe = parse(recipe_bytes(s))
    other = frame()
    other.loc[0, "id"] = "changed"
    with pytest.raises(CleaningError) as err:
        replay_recipe(other, recipe)
    assert err.value.code == "replay_mismatch"


def test_recipe_export_changes_nothing():
    s = CleaningSession(frame())
    apply(s, "trim_whitespace", {"columns": ["city"]})
    before = (s.version, s.fingerprint, len(s.audit_log), s.recipe(), s.snapshot_stats)
    recipe_export(s)
    assert (s.version, s.fingerprint, len(s.audit_log), s.recipe(), s.snapshot_stats) == before


# ---- JSON safety ----------------------------------------------------------

def test_json_ready_converts_and_rejects():
    import numpy as np
    doc = {"a": np.int64(3), "b": np.float64(0.5), "c": float("nan"), "d": float("inf"), "e": pd.NA, "f": pd.NaT,
           "g": (1, 2), "h": pd.Timestamp("2026-01-15 10:00"), 4: np.bool_(True), "i": np.array([1, 2])}
    assert json_ready(doc) == {"a": 3, "b": 0.5, "c": None, "d": None, "e": None, "f": None, "g": [1, 2],
                               "h": "2026-01-15T10:00:00", "4": True, "i": [1, 2]}
    with pytest.raises(TypeError):
        json_ready({"x": object()})


def test_json_bytes_never_emits_nan_or_infinity():
    raw = json_bytes({"x": [float("nan"), float("-inf"), 1.5]}).decode("utf-8")
    assert "NaN" not in raw and "Infinity" not in raw and json.loads(raw) == {"x": [None, None, 1.5]}
    assert raw.endswith("\n")


def test_unicode_is_written_as_utf8():
    raw = json_bytes({"name": "Zoë 日本 🎉"})
    assert "Zoë 日本 🎉".encode("utf-8") in raw
