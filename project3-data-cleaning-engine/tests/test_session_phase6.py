"""Treat outliers through CleaningSession: preview, confirmation, stale previews, audit,
undo, reset, replay with frozen boundaries, fingerprints, and steps from earlier phases."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.loader import load_file
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    """'amount' is text until converted; 1..9 plus one high and one low value."""
    return pd.DataFrame({
        "id": text(*[str(i) for i in range(12)]),
        "amount": text("-50", "1", "2", "NA", "3", "4", "5", "6", "7", "8", "9", "100"),
        "city": text("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"),
    })


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def typed_session(**kwargs) -> CleaningSession:
    s = CleaningSession(frame(), **kwargs)
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    apply(s, "convert_type", {"columns": ["amount"], "target": "integer"})
    return s


CAP = {"column": "amount", "method": "iqr", "treatment": "cap"}


def test_preview_changes_nothing_and_needs_confirmation():
    s = typed_session()
    fp, data, steps = s.fingerprint, s.data, len(s.steps)
    preview = s.preview("treat_outliers", CAP)
    assert s.fingerprint == fp and len(s.steps) == steps
    pd.testing.assert_frame_equal(s.data, data)
    assert preview.impact.counts == {"outliers": 2, "cells_changed": 2, "rows_removed": 0}
    assert preview.sample.index.name == "Original row" and preview.sample.index.tolist() == [1, 12]
    with pytest.raises(CleaningError) as err:
        s.apply(preview)
    assert err.value.code == "confirmation_required" and s.fingerprint == fp


def test_apply_records_history_with_frozen_detection():
    s = typed_session()
    entry = apply(s, "treat_outliers", CAP)
    assert entry.event == "apply" and entry.operation == "treat_outliers" and entry.step_number == 3
    assert entry.label == "Treat outliers"
    assert entry.summary == "Cap 2 outliers in 'amount' outside -5 to 15 (IQR, k = 1.5) at the boundaries."
    r = entry.resolved
    assert (r["lower_bound"], r["upper_bound"], r["below"], r["above"], r["missing"]) == (-5.0, 15.0, 1, 1, 1)
    assert entry.params == {"column": "amount", "method": "iqr", "multiplier": 1.5, "treatment": "cap",
                            "value": None}
    assert entry.counts["cells_changed"] == 2 and entry.affected_columns == ("amount",)
    assert s.data["amount"].tolist()[:3] == [-5, 1, 2] and s.data["amount"].iloc[-1] == 15
    assert pd.isna(s.data["amount"].iloc[3])
    assert str(s.data["amount"].dtype) == "Int64"


def test_stale_preview_is_rejected():
    s = typed_session()
    preview = s.preview("treat_outliers", CAP)
    apply(s, "treat_outliers", dict(CAP, treatment="set_missing"))
    fp = s.fingerprint
    with pytest.raises(CleaningError) as err:
        s.apply(preview, confirmed=True)
    assert err.value.code == "stale_preview" and s.fingerprint == fp


def test_preview_made_before_undo_is_stale_and_cannot_be_applied_twice():
    s = typed_session()
    preview = s.preview("treat_outliers", CAP)
    s.apply(preview, confirmed=True)
    with pytest.raises(CleaningError) as err:
        s.apply(preview, confirmed=True)
    assert err.value.code == "preview_used"
    again = s.preview("treat_outliers", dict(CAP, method="zscore", threshold=1))
    s.undo()
    with pytest.raises(CleaningError) as err:
        s.apply(again, confirmed=True)
    assert err.value.code == "stale_preview"


@pytest.mark.parametrize("treatment, extra", [("cap", {}), ("set_missing", {}), ("remove_rows", {}),
                                              ("replace_value", {"value": "0"})])
def test_undo_and_reset_restore_exact_states(treatment, extra):
    s = typed_session()
    before = s.fingerprint
    data_before, ids_before = s.data, s.row_ids
    apply(s, "treat_outliers", dict(CAP, treatment=treatment, **extra))
    assert s.fingerprint != before
    undo = s.undo()
    assert undo.event == "undo" and s.fingerprint == before
    pd.testing.assert_frame_equal(s.data, data_before)
    assert s.row_ids.tolist() == ids_before.tolist()
    apply(s, "treat_outliers", dict(CAP, treatment=treatment, **extra))
    s.reset()
    assert s.fingerprint == s.original_fingerprint and s.steps == ()


def test_undo_without_cached_states_replays_the_outlier_step():
    s = typed_session(snapshot_limit=0)
    before = s.fingerprint
    apply(s, "treat_outliers", dict(CAP, treatment="remove_rows"))
    apply(s, "treat_outliers", dict(CAP, method="percentile", lower_percentile=10, upper_percentile=90,
                                    treatment="set_missing"))
    s.undo()
    s.undo()
    assert s.fingerprint == before


def test_remove_rows_lineage():
    s = typed_session()
    apply(s, "treat_outliers", dict(CAP, treatment="remove_rows"))
    assert s.row_ids.tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]          # Rows 0 (-50) and 11 (100) removed
    assert s.data["id"].tolist() == [str(i) for i in range(1, 11)]
    assert s.shape == (10, 3)


def test_recipe_replays_and_is_json_round_trip():
    s = typed_session()
    apply(s, "treat_outliers", CAP)
    apply(s, "treat_outliers", dict(CAP, method="zscore", threshold=1.5, treatment="replace_value", value=4))
    recipe = json.loads(s.recipe_json())
    steps = recipe["steps"]
    assert [st["operation"] for st in steps] == ["standardize_missing", "convert_type", "treat_outliers",
                                                 "treat_outliers"]
    assert steps[2]["resolved"]["lower_bound"] == -5.0 and steps[3]["params"]["value"] == 4
    data, ids = replay_recipe(s.original_data, recipe)
    pd.testing.assert_frame_equal(data, s.data)
    assert ids.tolist() == s.row_ids.tolist()
    assert s.verify_replay().ok
    assert json.loads(s.recipe_json()) == recipe                  # Deterministic


def test_replay_uses_frozen_boundaries_not_recalculated_ones():
    """The second IQR step runs on capped data; its boundaries differ from the first and are frozen too."""
    s = typed_session()
    apply(s, "treat_outliers", dict(CAP, method="percentile", lower_percentile=20, upper_percentile=80))
    second = s.preview("treat_outliers", dict(CAP, method="zscore", threshold=1.2))
    first_bounds = s.steps[-1].plan.resolved["lower_bound"], s.steps[-1].plan.resolved["upper_bound"]
    assert (second.plan.resolved["lower_bound"], second.plan.resolved["upper_bound"]) != first_bounds
    s.apply(second, confirmed=True)
    recipe = s.recipe()
    tampered = copy.deepcopy(recipe)
    tampered["steps"][2]["resolved"]["upper_bound"] = 1e9          # Frozen value changed: nothing above it
    with pytest.raises(CleaningError) as err:
        replay_recipe(s.original_data, tampered)
    assert err.value.code == "replay_mismatch" and "values above" in err.value.message
    tampered = copy.deepcopy(recipe)
    tampered["steps"][3]["resolved"]["treatment"] = "set_missing"   # Same counts, different result
    with pytest.raises(CleaningError, match="did not reproduce"):
        replay_recipe(s.original_data, tampered)


def test_fingerprints_follow_the_data():
    a, b = typed_session(), typed_session()
    apply(a, "treat_outliers", CAP)
    apply(b, "treat_outliers", CAP)
    assert a.fingerprint == b.fingerprint
    c = typed_session()
    apply(c, "treat_outliers", dict(CAP, treatment="set_missing"))
    assert c.fingerprint != a.fingerprint


def test_no_op_creates_no_history():
    s = typed_session()
    log, fp = len(s.audit_log), s.fingerprint
    with pytest.raises(CleaningError) as err:
        s.preview("treat_outliers", dict(CAP, multiplier=100))
    assert err.value.code == "no_change"
    assert len(s.audit_log) == log and s.fingerprint == fp and len(s.steps) == 2


def test_failures_leave_the_session_unchanged():
    s = typed_session()
    log, fp = len(s.audit_log), s.fingerprint
    for params in (dict(CAP, column="city"), dict(CAP, treatment="replace_value", value="abc"),
                   dict(CAP, method="percentile", lower_percentile=90, upper_percentile=10)):
        with pytest.raises(CleaningError) as err:
            s.preview("treat_outliers", params)
        assert err.value.code == "invalid_params"
    assert len(s.audit_log) == log and s.fingerprint == fp
    apply(s, "treat_outliers", CAP)                                 # The session still works
    assert len(s.steps) == 3


def test_sequence_with_missing_value_steps():
    s = typed_session()
    apply(s, "treat_outliers", dict(CAP, treatment="set_missing"))
    apply(s, "add_missing_indicator", {"column": "amount"})
    apply(s, "fill_missing", {"columns": ["amount"], "method": "median"})
    d = s.data
    assert d["amount"].tolist() == [5, 1, 2, 5, 3, 4, 5, 6, 7, 8, 9, 5]
    assert d["amount_missing"].tolist() == ["true", "false", "false", "true", *["false"] * 7, "true"]
    assert s.verify_replay().ok
    for _ in range(3):
        s.undo()
    assert str(s.data["amount"].dtype) == "Int64" and s.data["amount"].iloc[0] == -50


def test_session_from_a_table_with_duplicate_index_labels():
    data = pd.DataFrame({"x": pd.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 100], dtype="Int64")}, index=[0, 0] * 5)
    s = CleaningSession(data)
    apply(s, "treat_outliers", {"column": "x", "method": "iqr", "treatment": "remove_rows"})
    assert s.data["x"].tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9] and s.row_ids.tolist() == list(range(9))


def test_sample_dataset_outliers_after_conversion():
    ds = load_file("messy_customers.csv", (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes())
    s = CleaningSession(ds.data)
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    preview = s.preview("treat_outliers", {"column": "age", "method": "iqr", "treatment": "set_missing"})
    r = preview.plan.resolved
    assert (r["lower_bound"], r["upper_bound"]) == (-12.0, 116.0)
    assert (r["below"], r["above"], r["evaluated"], r["missing"]) == (0, 4, 461, 39)
    assert sorted(preview.sample["age"].tolist()) == [150, 230, 999, 999]   # 999 appears on a duplicate row too
    s.apply(preview, confirmed=True)
    assert s.data["age"].max() <= 116 and str(s.data["age"].dtype) == "Int64"
    assert s.verify_replay().ok
