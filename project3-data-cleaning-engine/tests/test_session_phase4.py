"""Missing-value and duplicate steps through CleaningSession: confirmation, stale
previews, audit, undo, reset, replay, lineage and the snapshot memory budget."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import load_csv


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "2", "3", "4", "4"),
        "age": text("40", "NA", "NA", None, "20", "20"),
        "city": text("Oslo", "", "", "Rome", None, None),
    })


STEPS = [
    ("standardize_missing", {"columns": ["age"], "tokens": ["NA"]}),
    ("add_missing_indicator", {"column": "age"}),
    ("fill_missing", {"columns": ["age"], "method": "median"}),
    ("remove_duplicates", {"columns": ["id"], "keep": "last"}),
    ("fill_missing", {"columns": ["city"], "method": "bfill"}),      # Last city stays missing
    ("drop_missing_rows", {"columns": ["city"]}),
]


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def run_all(session):
    for op, params in STEPS:
        apply(session, op, params)
    return session


@pytest.mark.parametrize("op, params", [
    ("standardize_missing", {"columns": ["age"], "tokens": ["NA"]}),
    ("add_missing_indicator", {"column": "age"}),
    ("fill_missing", {"columns": ["city"], "method": "constant", "value": "?"}),
    ("remove_duplicates", {"columns": ["id"]}),
    ("drop_missing_columns", {"threshold": 30}),
])
def test_preview_changes_nothing_and_needs_confirmation(op, params):
    s = CleaningSession(frame())
    fp, data = s.fingerprint, s.data
    p = s.preview(op, params)
    assert s.fingerprint == fp and s.version == 0 and s.steps == () and s.audit_log == ()
    pd.testing.assert_frame_equal(s.data, data)
    with pytest.raises(CleaningError) as err:
        s.apply(p, confirmed=False)
    assert err.value.code == "confirmation_required" and s.fingerprint == fp
    s.apply(p, confirmed=True)
    assert s.fingerprint != fp and len(s.steps) == 1
    with pytest.raises(CleaningError) as err:
        s.apply(p, confirmed=True)
    assert err.value.code == "preview_used"


def test_preview_impact_matches_apply():
    s = CleaningSession(frame())
    p = s.preview("remove_duplicates", {"columns": ["id"]})
    assert (p.impact.rows_before, p.impact.rows_after) == (6, 4)
    assert p.impact.counts == {"rows_removed": 2, "groups": 2, "rows_kept": 4}
    assert p.sample.index.name == "Original row" and p.sample.index.tolist() == [2, 3, 5, 6]
    entry = s.apply(p, confirmed=True)
    assert entry.counts == p.impact.counts and entry.rows_after == 4


def test_stale_previews_are_rejected():
    s = CleaningSession(frame())
    fill = s.preview("fill_missing", {"columns": ["city"], "method": "constant", "value": "?"})
    apply(s, "remove_duplicates", {})
    with pytest.raises(CleaningError) as err:
        s.apply(fill, confirmed=True)
    assert err.value.code == "stale_preview"
    dup = s.preview("drop_missing_rows", {"columns": ["city"]})
    s.undo()
    with pytest.raises(CleaningError) as err:
        s.apply(dup, confirmed=True)
    assert err.value.code == "stale_preview"
    assert s.steps == ()


def test_no_change_preview_is_reported_and_records_nothing():
    s = CleaningSession(frame())
    with pytest.raises(CleaningError) as err:
        s.preview("standardize_missing", {"columns": ["id"], "tokens": ["NA"]})
    assert err.value.code == "no_change"
    assert s.version == 0 and s.audit_log == ()


def test_audit_records_frozen_values_and_counts():
    s = CleaningSession(frame())
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA"]})
    entry = apply(s, "fill_missing", {"columns": ["age"], "method": "median"})
    assert entry.operation == "fill_missing" and entry.label == "Fill missing values"
    assert entry.params == {"columns": ["age"], "method": "median", "decimals": None, "non_numeric": "reject"}
    assert entry.resolved["fills"] == {"age": "20"} and entry.resolved["cells"] == {"age": 3}   # 40, 20, 20
    assert entry.counts == {"cells_filled": 3, "columns_changed": 1, "cells_left_missing": 0}
    assert entry.summary == "Fill 3 missing cells in 'age' with the median ('20')."
    assert entry.affected_columns == ("age",)
    first = s.audit_log[0]
    assert first.resolved["tokens"] == ["NA"] and first.resolved["matches"] == {"age": 2}
    assert first.counts == {"cells_changed": 2, "columns_changed": 1}
    json.dumps([e.to_dict() for e in s.audit_log])


def test_undo_and_reset_restore_exact_states():
    for limit in (0, 2, 5):
        s = CleaningSession(frame(), snapshot_limit=limit)
        states = [(s.data, s.row_ids, s.fingerprint)]
        for op, params in STEPS:
            apply(s, op, params)
            states.append((s.data, s.row_ids, s.fingerprint))
        for data, ids, fp in reversed(states[:-1]):
            s.undo()
            pd.testing.assert_frame_equal(s.data, data)
            assert s.row_ids.tolist() == ids.tolist() and s.fingerprint == fp
        run_all(s)
        s.reset()
        assert s.fingerprint == s.original_fingerprint
        pd.testing.assert_frame_equal(s.data, frame())


def test_replay_is_deterministic_and_reproduces_every_step():
    a = run_all(CleaningSession(frame()))
    b = run_all(CleaningSession(frame()))
    assert a.recipe_json() == b.recipe_json()
    assert a.verify_replay().ok
    data, ids = replay_recipe(frame(), json.loads(a.recipe_json()))
    pd.testing.assert_frame_equal(data, a.data)
    assert ids.tolist() == a.row_ids.tolist()


def test_replay_uses_frozen_fill_values():
    s = CleaningSession(frame())
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA"]})
    apply(s, "fill_missing", {"columns": ["age"], "method": "mean"})
    recipe = copy.deepcopy(s.recipe())
    assert recipe["steps"][1]["resolved"]["fills"] == {"age": "26.666666666666668"}     # 80 / 3
    recipe["steps"][1]["resolved"]["fills"]["age"] = "27"           # A tampered frozen value
    with pytest.raises(CleaningError) as err:
        replay_recipe(frame(), recipe)
    assert err.value.code == "replay_mismatch"


def test_replay_rejects_changed_counts():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {"columns": ["id"]})
    recipe = copy.deepcopy(s.recipe())
    recipe["steps"][0]["resolved"]["rows_removed"] = 1
    recipe["steps"][0]["fingerprint_before"] = None                 # Force the operation's own check
    with pytest.raises(CleaningError) as err:
        replay_recipe(frame(), recipe)
    assert "recorded step expected 1" in err.value.message


def test_row_lineage_through_row_removal():
    s = CleaningSession(frame(), source_rows=np.array([2, 3, 4, 5, 6, 7]))
    apply(s, "remove_duplicates", {"columns": ["id"], "keep": "last"})       # Keeps rows 0, 2, 3, 5
    assert s.row_ids.tolist() == [0, 2, 3, 5] and s.source_rows.tolist() == [2, 4, 5, 7]
    apply(s, "drop_missing_rows", {"columns": ["city"]})                     # Removes '' (row 2) and None (5)
    assert s.row_ids.tolist() == [0, 3] and s.source_rows.tolist() == [2, 5]
    apply(s, "add_missing_indicator", {"column": "age"})
    assert s.row_ids.tolist() == [0, 3] and "age_missing" in s.columns
    s.undo()
    s.undo()
    assert s.row_ids.tolist() == [0, 2, 3, 5]


def test_user_column_named_like_indicator_is_not_overwritten():
    df = frame().assign(age_missing=text(*"abcdef"))
    s = CleaningSession(df)
    apply(s, "add_missing_indicator", {"column": "age"})
    assert s.columns == ("id", "age", "age_missing_2", "city", "age_missing")
    assert s.data["age_missing"].tolist() == list("abcdef")


# ---- snapshot memory budget ------------------------------------------

def test_snapshots_respect_the_memory_budget():
    s = CleaningSession(frame())
    one = s.snapshot_stats
    assert one.count == 0 and one.limit == 5
    run_all(s)
    assert s.snapshot_stats.count == 5                                 # Small table: full limit

    size = CleaningSession(frame())
    apply(size, "add_missing_indicator", {"column": "age"})         # The original is never cached
    apply(size, "remove_duplicates", {})
    per_state = size.snapshot_stats.bytes
    assert per_state > 0

    tight = CleaningSession(frame(), snapshot_budget_bytes=int(per_state * 2.5))
    run_all(tight)
    stats = tight.snapshot_stats
    assert 1 <= stats.count <= 2 and stats.bytes <= stats.budget_bytes


def test_states_over_the_budget_are_never_cached_and_undo_replays():
    s = CleaningSession(frame(), snapshot_budget_bytes=1)
    states = [s.fingerprint]
    for op, params in STEPS:
        apply(s, op, params)
        states.append(s.fingerprint)
        assert s.snapshot_stats.count == 0
    for fp in reversed(states[:-1]):
        s.undo()
        assert s.fingerprint == fp


def test_snapshot_bookkeeping_after_undo_and_reset():
    s = run_all(CleaningSession(frame()))
    s.undo()
    stats = s.snapshot_stats
    assert stats.count == len(s._snapshot_bytes) and stats.bytes == sum(s._snapshot_bytes.values())
    s.reset()
    assert s.snapshot_stats.count == 0 and s.snapshot_stats.bytes == 0


# ---- the sample dataset end to end --------------------------------------

def test_sample_dataset_pipeline_replays_on_a_fresh_load(sample_bytes):
    ds = load_csv(sample_bytes.decode("utf-8"), name="messy_customers.csv")
    s = CleaningSession(ds.data, source_rows=ds.source_rows)
    exact = s.preview("remove_duplicates", {})
    assert exact.impact.counts["rows_removed"] == 12                    # As documented for the sample
    s.apply(exact, confirmed=True)
    key = s.preview("remove_duplicates", {"columns": ["customer_id"]})
    assert key.impact.counts["rows_removed"] == 8
    s.apply(key, confirmed=True)
    apply(s, "standardize_missing", {"columns": ["email", "signup_date", "age", "annual_spend"],
                                     "tokens": ["NA", "N/A", "null", "-", "unknown"], "empty_text": True})
    apply(s, "add_missing_indicator", {"column": "age"})
    apply(s, "fill_missing", {"columns": ["age"], "method": "median"})
    apply(s, "fill_missing", {"columns": ["annual_spend"], "method": "mean", "decimals": 2,
                              "non_numeric": "exclude"})
    apply(s, "drop_missing_rows", {"columns": ["email"]})
    assert s.shape[0] < 480 and len(s.steps) == 7
    fresh = load_csv(sample_bytes.decode("utf-8"), name="copy.csv")
    data, ids = replay_recipe(fresh.data, json.loads(s.recipe_json()))
    pd.testing.assert_frame_equal(data, s.data)
    assert ids.tolist() == s.row_ids.tolist()
    assert all(dtype == TEXT_DTYPE for dtype in s.data.dtypes)          # Text preserved throughout
