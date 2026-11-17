"""Comparison through CleaningSession: before and after cleaning, after undo and
reset, across several steps, and proof that comparing changes nothing."""

import numpy as np
import pandas as pd
import pytest

from refine.compare import compare_datasets
from refine.loader import load_file
from refine.session import CleaningSession
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "2", "3", "4", "5"),
        "amount": text("10", "20", "20", "NA", "40", "900"),
        "city": text(" Paris", "Rome", "Rome", "oslo", None, "Rome"),
    })


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def state(s: CleaningSession) -> tuple:
    """Everything comparison must leave alone."""
    return (s.version, s.fingerprint, s.original_fingerprint, len(s.steps), len(s.audit_log), s.recipe_json(),
            s.can_undo, s.snapshot_stats, s.row_ids.tolist(), s.shape)


@pytest.fixture
def sample_session():
    data = (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes()
    ds = load_file("messy_customers.csv", data)
    return CleaningSession(ds.data, source_rows=ds.source_rows)


def test_before_cleaning_everything_is_unchanged():
    s = CleaningSession(frame())
    c = s.compare()
    assert c.identical and c.cells.rows_removed == 0 and c.cells.cells_changed == 0
    assert c.metric("duplicate_rows").original == c.metric("duplicate_rows").current == 1


def test_after_cleaning():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    c = s.compare()
    assert c.metric("rows").difference == -1
    assert c.duplicates.original_duplicates == 1 and c.duplicates.current_duplicates == 0
    assert c.column_missing("amount").original_missing == 0 and c.column_missing("amount").current_missing == 1
    assert c.cells.rows_removed == 1
    amount = c.cells.column("amount")
    assert amount.made_missing == 1 and amount.modified == 0 and amount.examples[0].original_row == 4
    assert c.cells.cells_changed == 1        # The removed duplicate is not a cell change


def test_session_compare_matches_compare_datasets_on_copies():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {})
    apply(s, "trim_whitespace", {"columns": ["city"]})
    a = s.compare()
    b = compare_datasets(s.original_data, s.data, row_ids=s.row_ids)
    assert a.overview == b.overview and a.missing == b.missing and a.cells == b.cells
    assert a.columns == b.columns and a.duplicates == b.duplicates


def test_after_undo_and_reset():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {})
    apply(s, "drop_columns", {"columns": ["city"]})
    assert [x.name for x in s.compare().columns.removed] == ["city"]
    s.undo()
    c = s.compare()
    assert not c.columns.removed and c.cells.rows_removed == 1
    s.reset()
    c = s.compare()
    assert c.identical and c.metric("rows").difference == 0


def test_comparing_changes_no_session_state():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    before = state(s)
    data, original = s.data, s.original_data
    for _ in range(3):
        s.compare()
    assert state(s) == before
    pd.testing.assert_frame_equal(s.data, data)
    pd.testing.assert_frame_equal(s.original_data, original)
    s.undo()                                  # Undo still works exactly after comparing
    assert s.fingerprint == s.steps[-1].fingerprint_after


def test_pending_preview_stays_valid_after_comparing():
    s = CleaningSession(frame())
    preview = s.preview("remove_duplicates", {})
    s.compare()
    assert s.is_current(preview)
    apply(s, "trim_whitespace", {"columns": ["city"]})
    assert not s.is_current(preview)


def test_sample_dataset_multiple_steps_of_every_kind(sample_session):
    s = sample_session
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["age", "annual_spend"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    apply(s, "trim_whitespace", {"columns": ["full_name", "country"]})
    apply(s, "treat_outliers", {"column": "age", "method": "iqr", "treatment": "remove_rows"})
    before = state(s)
    c = s.compare()
    assert state(s) == before
    assert c.metric("rows").original == 500 and c.metric("rows").current == s.shape[0]
    assert c.cells.rows_removed == 500 - s.shape[0]
    assert [t.name for t in c.columns.type_changed] == ["age"]
    assert c.cells.column("age").compared_as == "text"
    assert c.cells.column("customer_id").changed == 0     # Never edited, so no false changes after removals
    assert c.cells.column("full_name").modified > 0
    assert c.duplicates.original_duplicates == 12
    age = c.detail("age")
    assert age.has_numeric and {m.key: m for m in age.numeric_changes()}["maximum"].direction == "decrease"


def test_comparison_after_undoing_a_row_removal(sample_session):
    s = sample_session
    apply(s, "remove_duplicates", {})
    s.undo()
    c = s.compare()
    assert c.identical and c.cells.rows_removed == 0


def test_lineage_is_used_not_positions():
    """Drop the first row: positionally every cell would differ; by identity none does."""
    df = pd.DataFrame({"v": text("a", "b", "c", "d"), "w": text("", "1", "2", "3")})
    s = CleaningSession(df)
    apply(s, "drop_missing_rows", {"columns": ["w"], "how": "any"})
    c = s.compare()
    assert c.cells.rows_removed == 1 and c.cells.cells_changed == 0
    assert np.array_equal(s.row_ids, [1, 2, 3])
