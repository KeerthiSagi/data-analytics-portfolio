"""Exports through a CleaningSession: they always reflect the confirmed current state and change nothing.

Uses the builders the Export page registers (ui/exports.py), called directly
as Streamlit would call them on a click.
"""

import csv
import io
import json

import openpyxl
import pandas as pd
import pytest

from refine.export import csv_bytes, inspect_export, text_values
from refine.loader import load_file
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR
from ui.exports import StaleExport, dataset_downloads, record_downloads, report_downloads


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def state(s: CleaningSession) -> tuple:
    """Everything an export must leave alone."""
    return (s.version, s.fingerprint, s.original_fingerprint, len(s.steps), len(s.audit_log), s.recipe_json(),
            s.can_undo, s.snapshot_stats, s.row_ids.tolist(), s.shape, tuple(e.to_dict()["timestamp"]
                                                                              for e in s.audit_log))


@pytest.fixture
def sample():
    data = (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes()
    ds = load_file("messy_customers.csv", data)
    return ds, CleaningSession(ds.data, source_rows=ds.source_rows)


def downloads(ds, s, **kwargs):
    items = dataset_downloads(ds, s, **kwargs) + record_downloads(ds, s) + report_downloads(ds, s, ())
    return {d.key: d for d in items}


def csv_rows(data: bytes):
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def expected_rows(frame: pd.DataFrame):
    cols = [text_values(frame.iloc[:, j]) for j in range(frame.shape[1])]
    return [[str(c) for c in frame.columns]] + [["" if v is None else v for v in row] for row in zip(*cols)]


def build_all(d):
    return {key: item.build() for key, item in d.items()}


def check_matches_session(files, ds, s):
    assert csv_rows(files["refine_export_csv"]) == expected_rows(s.data)
    ws = openpyxl.load_workbook(io.BytesIO(files["refine_export_xlsx"])).active
    assert ws.max_column == s.shape[1] and [c.value for c in ws[1]] == list(s.data.columns)
    history = json.loads(files["refine_export_history"])
    assert [x["operation"] for x in history["active_steps"]] == [st.plan.operation for st in s.steps]
    assert len(history["events"]) == len(s.audit_log)
    assert json.loads(files["refine_export_recipe"]) == s.recipe()
    for key in ("refine_export_quality", "refine_export_comparison"):
        doc = json.loads(files[key])
        assert doc["state"]["current"]["fingerprint"] == s.fingerprint
        assert doc["state"]["steps_applied"] == len(s.steps)
    quality = json.loads(files["refine_export_quality"])
    assert quality["current"]["rows"] == s.shape[0] and quality["current"]["columns"] == s.shape[1]


# ---- every state ----------------------------------------------------------

def test_export_before_cleaning(sample):
    ds, s = sample
    d = downloads(ds, s)
    assert d["refine_export_csv"].file_name == "messy_customers_original.csv"
    assert d["refine_export_xlsx"].file_name == "messy_customers_original.xlsx"
    files = build_all(d)
    check_matches_session(files, ds, s)
    assert json.loads(files["refine_export_comparison"])["identical"] is True
    assert csv_rows(files["refine_export_csv"])[1][0] == "C-0001"


def test_export_after_cleaning(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    d = downloads(ds, s)
    assert d["refine_export_csv"].file_name == "messy_customers_cleaned.csv"
    files = build_all(d)
    check_matches_session(files, ds, s)
    assert len(csv_rows(files["refine_export_csv"])) == 1 + 488


def test_export_after_undo_and_reset_reflects_the_new_state(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "drop_columns", {"columns": ["segment"]})
    first = build_all(downloads(ds, s))
    assert "segment" not in csv_rows(first["refine_export_csv"])[0]
    s.undo()
    after_undo = build_all(downloads(ds, s))
    check_matches_session(after_undo, ds, s)
    assert "segment" in csv_rows(after_undo["refine_export_csv"])[0]
    assert len(csv_rows(after_undo["refine_export_csv"])) == 1 + 488
    s.reset()
    after_reset = build_all(downloads(ds, s))
    check_matches_session(after_reset, ds, s)
    assert len(csv_rows(after_reset["refine_export_csv"])) == 1 + 500
    assert json.loads(after_reset["refine_export_recipe"])["steps"] == []
    events = json.loads(after_reset["refine_export_history"])["events"]
    assert [e["event"] for e in events] == ["apply", "apply", "undo", "reset"]


def test_pending_preview_is_not_exported_and_stays_valid(sample):
    ds, s = sample
    apply(s, "trim_whitespace", {"columns": ["full_name"]})
    preview = s.preview("remove_duplicates", {})
    before = state(s)
    files = build_all(downloads(ds, s))
    assert state(s) == before
    assert s.is_current(preview)                       # The preview can still be applied
    assert len(csv_rows(files["refine_export_csv"])) == 1 + 500   # Not the preview's 488 rows
    assert [x["operation"] for x in json.loads(files["refine_export_history"])["active_steps"]] == ["trim_whitespace"]
    s.apply(preview, confirmed=True)
    assert len(csv_rows(build_all(downloads(ds, s))["refine_export_csv"])) == 1 + 488


def test_export_after_outlier_treatment(sample):
    ds, s = sample
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    apply(s, "treat_outliers", {"column": "age", "method": "iqr", "treatment": "cap"})
    files = build_all(downloads(ds, s))
    check_matches_session(files, ds, s)
    recipe = json.loads(files["refine_export_recipe"])
    assert recipe["steps"][-1]["resolved"] == s.steps[-1].plan.resolved   # Boundaries as frozen
    data, _ = replay_recipe(ds.data, recipe)
    pd.testing.assert_frame_equal(data, s.data)
    ws = openpyxl.load_workbook(io.BytesIO(files["refine_export_xlsx"])).active
    ages = [r[5] for r in ws.iter_rows(min_row=2, values_only=True)]
    assert ages == [None if pd.isna(v) else int(v) for v in s.data["age"]]


def test_export_after_row_removal(sample):
    ds, s = sample
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    apply(s, "treat_outliers", {"column": "age", "method": "iqr", "treatment": "remove_rows"})
    files = build_all(downloads(ds, s))
    check_matches_session(files, ds, s)
    cells = json.loads(files["refine_export_comparison"])["cells"]
    assert cells["rows_removed"] == 500 - s.shape[0] > 0
    ids = [r[0] for r in csv_rows(files["refine_export_csv"])[1:]]
    assert ids == s.data["customer_id"].tolist()       # Rows in order, removed rows absent


# ---- exports change nothing ------------------------------------------------

def test_no_export_changes_the_session(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "trim_whitespace", {"columns": ["full_name"]})
    s.undo()
    apply(s, "trim_whitespace", {"columns": ["country"]})
    before = state(s)
    data, original = s.data, s.original_data
    for _ in range(2):
        build_all(downloads(ds, s, escape_formulas=True))
        inspect_export(s.data)
    assert state(s) == before
    pd.testing.assert_frame_equal(s.data, data)
    pd.testing.assert_frame_equal(s.original_data, original)
    pd.testing.assert_frame_equal(ds.data, original)
    s.undo()                                           # Undo is still exact afterwards
    assert s.fingerprint == s.steps[-1].fingerprint_after
    assert s.verify_replay().ok


def test_escaping_changes_only_the_file(sample):
    ds, _ = sample
    s = CleaningSession(pd.DataFrame({"a": pd.array(["=1+1", "x"], dtype=TEXT_DTYPE)}))
    before = state(s)
    data = dataset_downloads(ds, s, escape_formulas=True)[0].build()
    assert csv_rows(data)[1] == ["'=1+1"]
    assert state(s) == before and s.data["a"].tolist() == ["=1+1", "x"]
    assert csv_rows(csv_bytes(s.data)[0])[1] == ["=1+1"]      # Without escaping: the exact value


# ---- builders are bound to the state the page showed -----------------------

def test_builders_refuse_after_the_state_changes(sample):
    ds, s = sample
    d = downloads(ds, s)
    apply(s, "remove_duplicates", {})
    for item in d.values():
        with pytest.raises(StaleExport):
            item.build()
    fresh = downloads(ds, s)
    assert len(csv_rows(fresh["refine_export_csv"].build())) == 1 + 488


def test_builders_refuse_after_undo_back_to_the_same_data(sample):
    """Same data but a different history: the history file would not match the page."""
    ds, s = sample
    d = downloads(ds, s)
    apply(s, "remove_duplicates", {})
    s.undo()
    assert s.fingerprint == s.original_fingerprint
    with pytest.raises(StaleExport):
        d["refine_export_history"].build()


def test_reports_reuse_a_cached_comparison(sample):
    ds, s = sample
    apply(s, "remove_duplicates", {})
    c = s.compare()
    calls = []
    original = s.compare
    s.compare = lambda **kw: calls.append(kw) or original(**kw)     # type: ignore[method-assign]
    try:
        quality, comparison = report_downloads(ds, s, (), cached=c)
        quality.build(), comparison.build()
        assert calls == []                              # The cached comparison was used
        quality, comparison = report_downloads(ds, s, (), reports={"current_profile": c.current_profile})
        quality.build(), comparison.build()
        assert len(calls) == 1                          # Computed once for both reports
        assert calls[0]["current_profile"] is c.current_profile
    finally:
        del s.compare


def test_reports_use_the_validity_rules(sample):
    from refine.quality import ValidityRule
    ds, s = sample
    rule = ValidityRule("age", "range", minimum=0, maximum=120)
    quality, _ = report_downloads(ds, s, (rule,))
    doc = json.loads(quality.build())
    assert doc["validity_rules"][0]["column"] == "age"
