"""Phase 9: complete workflows through the real engine and the Export page's builders.

Each workflow loads a file with the real loader, cleans it through a
CleaningSession, and checks that every consumer agrees with the session
and with each other: the profile (Profile & Quality page), the comparison
(Compare page), the CSV and Excel files, the history, the recipe and both
reports. The files are built by the same functions the Export page
registers (ui/exports.py), called as Streamlit calls them on a click.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json

import numpy as np
import openpyxl
import pandas as pd
import pytest

from refine.errors import CleaningError, LoadError
from refine.export import text_values
from refine.loader import load_file
from refine.operations import get_operation
from refine.profile import profile_dataset
from refine.quality import assess_quality
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from ui.exports import dataset_downloads, record_downloads, report_downloads


# ---- helpers -----------------------------------------------------------------

def strict_json(data: bytes):
    """Parse JSON, refusing the non-standard NaN / Infinity tokens."""
    def refuse(token):
        raise AssertionError(f"Non-standard JSON constant {token}")
    return json.loads(data, parse_constant=refuse)


def build_files(ds, session, rules=()):
    items = dataset_downloads(ds, session) + record_downloads(ds, session) + report_downloads(ds, session, rules)
    return {d.key.removeprefix("refine_export_"): d.build() for d in items}


def csv_table(data: bytes) -> list[list[str]]:
    assert data.decode("utf-8")                                # UTF-8, no BOM
    assert not data.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def sheet_rows(data: bytes) -> list[tuple]:
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    assert ws.title == "Cleaned Data"
    return list(ws.iter_rows(values_only=True))


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def check_consistent(ds, session, files):
    """Every export, the comparison and a fresh profile describe the session's current data."""
    data = session.data
    rows, cols = data.shape
    profile = profile_dataset(data)
    comparison = session.compare()

    # Row and column counts agree everywhere.
    assert comparison.metric("rows").current == rows == profile.n_rows
    assert comparison.metric("columns").current == cols
    table = csv_table(files["csv"])
    assert len(table) == rows + 1 and table[0] == list(data.columns)
    assert table[1:] == [["" if v is None else v for v in r]
                         for r in zip(*(text_values(data.iloc[:, j]) for j in range(cols)))]
    sheet = sheet_rows(files["xlsx"])
    assert list(sheet[0]) == list(data.columns)
    assert len(sheet) - 1 == rows                              # No trailing all-missing rows in these tables

    # Missing values agree: profile, comparison, both reports.
    quality = strict_json(files["quality"])
    report = strict_json(files["comparison"])
    assert comparison.metric("missing_cells").current == profile.missing_cells == quality["current"]["missing_cells"]
    per_column = {c.name: c.missing_count for c in profile.columns}
    assert {c["name"]: c["missing"] for c in quality["current"]["column_profiles"]} == per_column
    assert {m["column"]: m["current_missing"] for m in report["missing_values"]
            if m["status"] != "removed"} == per_column
    assert report["duplicates"]["current_duplicates"] == profile.duplicate_rows
    for doc in (quality, report):
        assert doc["state"]["current"]["fingerprint"] == session.fingerprint
        assert doc["state"]["original"]["fingerprint"] == session.original_fingerprint
        assert doc["state"]["steps_applied"] == len(session.steps)
    assert quality["current"]["rows"] == rows and report["cells"]["current_rows"] == rows

    # History and recipe are the session's own records.
    history = strict_json(files["history"])
    assert [s["operation"] for s in history["active_steps"]] == [s.plan.operation for s in session.steps]
    assert len(history["events"]) == len(session.audit_log)
    recipe = strict_json(files["recipe"])
    assert recipe == session.recipe()
    replayed, ids = replay_recipe(ds.data, recipe)               # The parsed original, as loaded
    assert replayed.equals(data) and ids.tolist() == session.row_ids.tolist()
    return comparison, profile, quality, report, history, recipe, sheet


# ---- Workflow A: standard cleaning ---------------------------------------------

def messy_csv() -> str:
    """Missing numbers and text, duplicates, mixed case, padding, numbers and dates as text, outliers."""
    lines = ["id,name,city,age,salary,joined,active"]
    cities = ["New York", "new york", " NEW YORK ", "Boston", "boston "]
    for i in range(1, 41):
        name = "" if i % 13 == 0 else f"  Person {i} "
        age = "NA" if i % 9 == 0 else str(25 + i % 30)
        salary = "" if i % 11 == 0 else str(40_000 + i * 500)
        if i == 30:
            salary = "9999999"                                  # High outlier
        if i == 31:
            age = "130"                                         # Implausible age
        joined = "2023-02-30" if i == 7 else f"2023-01-{i % 28 + 1:02d}"   # One invalid date
        lines.append(f"{i},{name},{cities[i % 5]},{age},{salary},{joined},{'yes' if i % 2 else 'no'}")
    lines += [lines[5], lines[6], lines[5]]                     # Exact duplicates of rows 5 and 6
    return "\n".join(lines) + "\n"


@pytest.fixture
def messy():
    ds = load_file("messy people.csv", messy_csv().encode("utf-8"))
    return ds, CleaningSession(ds.data, source_rows=ds.source_rows)


STEPS = [
    ("standardize_missing", {"columns": ["age"], "tokens": ["NA"]}),
    ("fill_missing", {"columns": ["name"], "method": "constant", "value": "Unknown"}),
    ("trim_whitespace", {"columns": ["name", "city"]}),
    ("change_case", {"columns": ["city"], "case": "title"}),
    ("remove_duplicates", {}),
    ("convert_type", {"columns": ["age", "salary"], "target": "integer", "on_error": "coerce"}),
    ("convert_type", {"columns": ["joined"], "target": "date", "date_formats": ["ymd"], "on_error": "coerce"}),
    ("convert_type", {"columns": ["active"], "target": "boolean", "true_values": ["yes"], "false_values": ["no"],
                      "on_error": "coerce"}),
    ("fill_missing", {"columns": ["age"], "method": "median", "decimals": 0}),
    ("treat_outliers", {"column": "salary", "method": "iqr", "multiplier": 1.5, "treatment": "cap"}),
    ("treat_outliers", {"column": "age", "method": "iqr", "multiplier": 1.5, "treatment": "remove_rows"}),
]


def test_workflow_a_standard_cleaning_every_output_agrees(messy):
    ds, s = messy
    original_copy = ds.data.copy(deep=True)
    start = profile_dataset(ds.data)
    assert start.duplicate_rows == 3 and start.missing_cells > 0
    assert assess_quality(ds.data, start, ()).issues               # The file has findings to fix

    for op, params in STEPS:
        apply(s, op, params)

    data = s.data
    assert data.shape == (39, 7)                                    # 43 rows - 3 duplicates - 1 outlier row
    assert {c: str(t) for c, t in data.dtypes.items() if c in ("age", "salary", "active")} == \
        {"age": "Int64", "salary": "Int64", "active": "boolean"}
    assert data["city"].tolist().count("New York") > 0 and set(data["city"]) == {"New York", "Boston"}
    assert not data["name"].str.startswith(" ").any() and data["name"].isna().sum() == 0
    assert pd.isna(data.loc[s.row_ids.tolist().index(6), "joined"])  # 2023-02-30 could not be converted
    assert data["salary"].max() < 9_999_999 and 130 not in data["age"].tolist()

    files = build_files(ds, s)
    comparison, profile, quality, report, history, recipe, sheet = check_consistent(ds, s, files)

    # The original is untouched by cleaning, comparison and export.
    assert ds.data.equals(original_copy) and s.original_data.equals(original_copy)
    assert comparison.metric("missing_cells").original == start.missing_cells
    assert comparison.metric("duplicate_rows").original == 3 and comparison.metric("duplicate_rows").current == 0
    assert quality["original"]["missing_cells"] == start.missing_cells

    # Cell comparison is matched by lineage: removed rows are not cell changes.
    cells = report["cells"]
    assert cells["available"] and cells["rows_removed"] == 4 and cells["rows_kept"] == 39
    assert cells["cells_compared"] == 39 * 7
    assert set(report["columns"]["type_changed"]) == {"age", "salary", "joined", "active"}

    # History reflects the applied steps, in order, with their counts.
    assert [e["event"] for e in history["events"]] == ["apply"] * len(STEPS)
    assert history["active_steps"][4]["counts"]["rows_removed"] == 3

    # Excel keeps the types; CSV writes everything as text.
    header = list(sheet[0])
    first = dict(zip(header, sheet[1]))
    assert isinstance(first["age"], int) and isinstance(first["salary"], int)
    assert isinstance(first["active"], bool) and isinstance(first["joined"], dt.datetime)
    assert isinstance(first["id"], str)                                 # id was never converted
    csv_first = dict(zip(header, csv_table(files["csv"])[1]))
    assert csv_first["active"] in ("true", "false") and csv_first["joined"].count("-") == 2


def test_workflow_a_through_the_page_caches_matches_a_fresh_comparison(messy):
    """The reports reuse cached profiles exactly as the Export page passes them, and still agree."""
    ds, s = messy
    original_profile = profile_dataset(s.data)
    original_quality = assess_quality(s.data, original_profile, ())
    for op, params in STEPS[:6]:
        apply(s, op, params)
    reports = {"original_profile": original_profile, "original_quality": original_quality}
    cached = report_downloads(ds, s, (), reports=reports)
    fresh = report_downloads(ds, s, ())
    for a, b in zip(cached, fresh):
        doc_a, doc_b = strict_json(a.build()), strict_json(b.build())
        for doc in (doc_a, doc_b):
            doc.pop("exported_at")
        assert json.dumps(doc_a, sort_keys=True) == json.dumps(doc_b, sort_keys=True)


# ---- Workflow B: undo and reset --------------------------------------------------

def test_workflow_b_undo_and_reset_refresh_every_output(messy):
    ds, s = messy
    for op, params in STEPS[:5]:
        apply(s, op, params)
    before_last = (s.data, s.row_ids, s.fingerprint)
    apply(s, *STEPS[5])
    files = build_files(ds, s)
    assert strict_json(files["comparison"])["columns"]["type_changed"] == ["age", "salary"]

    s.undo()
    assert s.data.equals(before_last[0]) and s.row_ids.tolist() == before_last[1].tolist()
    assert s.fingerprint == before_last[2]
    files = build_files(ds, s)
    _, _, _, report, history, recipe, _ = check_consistent(ds, s, files)
    assert report["columns"]["type_changed"] == []                       # Nothing left from the undone step
    assert len(recipe["steps"]) == 5
    statuses = [e.get("status") for e in history["events"] if e["event"] == "apply"]
    assert statuses == ["active"] * 5 + ["undone"]

    s.reset()
    assert s.data.equals(ds.data) and s.fingerprint == s.original_fingerprint and not s.is_modified
    files = build_files(ds, s)
    _, _, _, report, history, recipe, _ = check_consistent(ds, s, files)
    assert report["identical"] is True and recipe["steps"] == []
    assert report["cells"]["cells_changed"] == 0
    assert [e["event"] for e in history["events"]] == ["apply"] * 6 + ["undo", "reset"]
    assert {e.get("status") for e in history["events"] if e["event"] == "apply"} == {"undone", "discarded by reset"}
    assert history["active_steps"] == []


# ---- Workflow D: recipe replay ----------------------------------------------------

def test_workflow_d_exported_recipe_replays_with_frozen_outlier_boundaries(messy, monkeypatch):
    ds, s = messy
    for op, params in STEPS:
        apply(s, op, params)
    recipe = strict_json(build_files(ds, s)["recipe"])
    assert recipe["source"]["fingerprint"] == s.original_fingerprint
    outlier_steps = [x for x in recipe["steps"] if x["operation"] == "treat_outliers"]
    assert [x["resolved"]["column"] for x in outlier_steps] == ["salary", "age"]
    assert outlier_steps[0]["resolved"]["upper_bound"] == s.steps[9].plan.resolved["upper_bound"]

    # Replay must use the frozen boundaries, never detect again.
    import refine.operations.outlier_ops as outlier_ops

    def no_detection(*args, **kwargs):
        raise AssertionError("replay recalculated outliers")
    monkeypatch.setattr(outlier_ops, "detect_outliers", no_detection)
    reloaded = load_file("messy people.csv", messy_csv().encode("utf-8"))   # The same file loaded again
    data, ids = replay_recipe(reloaded.data, recipe)
    assert data.equals(s.data) and ids.tolist() == s.row_ids.tolist()

    # A different original table is refused, not silently cleaned.
    other = load_file("other.csv", messy_csv().replace("Person 1 ", "Person one ").encode("utf-8"))
    with pytest.raises(CleaningError) as err:
        replay_recipe(other.data, recipe)
    assert err.value.code == "replay_mismatch"


# ---- Data integrity ------------------------------------------------------------------

def test_an_unexpected_error_while_applying_changes_nothing(messy, monkeypatch):
    ds, s = messy
    apply(s, *STEPS[0])
    preview = s.preview(*STEPS[4])
    state = (s.version, s.fingerprint, len(s.steps), len(s.audit_log), s.recipe_json(), s.data)
    operation = get_operation("remove_duplicates")

    def broken(*args, **kwargs):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(type(operation), "apply", broken)
    with pytest.raises(RuntimeError):
        s.apply(preview, confirmed=True)
    after = (s.version, s.fingerprint, len(s.steps), len(s.audit_log), s.recipe_json(), s.data)
    assert after[:5] == state[:5] and after[5].equals(state[5])
    monkeypatch.undo()
    assert s.is_current(preview)                                 # Still usable once the failure is gone
    s.apply(preview, confirmed=True)
    assert s.shape[0] == 40


def test_row_lineage_after_duplicate_and_outlier_row_removal(messy):
    ds, s = messy
    for op, params in STEPS:
        apply(s, op, params)
    ids = s.row_ids
    assert len(set(ids.tolist())) == len(ids) and (np.diff(ids) > 0).all()      # Kept rows, in order
    assert {40, 41, 42} - set(ids.tolist()) == {40, 41, 42}                      # The appended duplicates
    original = ds.data
    for current_row, original_row in enumerate(ids):                             # Each row is the same record
        assert s.data["id"].iloc[current_row] == original["id"].iloc[original_row]
    assert s.source_rows.tolist() == [int(i) + 2 for i in ids]                  # File line = row + header + 1
    from refine.compare import _shown                                             # How examples are displayed
    for col in s.compare().cells.columns:
        for e in col.examples:                                                    # "Before" is the original cell
            assert e.before == _shown(original[col.name].iloc[e.original_row - 1])
            assert e.original_row - 1 in ids


# ---- Workflow E: edge cases ----------------------------------------------------------

def load(text: str, name: str = "edge.csv"):
    return load_file(name, text.encode("utf-8"))


@pytest.mark.parametrize("text, code", [("", "empty_file"), ("a,b\n", "header_only"), ("\n\n", "empty_file")])
def test_empty_files_are_refused_by_the_loader(text, code):
    with pytest.raises(LoadError) as err:
        load(text)
    assert err.value.code == code and err.value.message


def test_single_row_and_no_cleaning_needed():
    ds = load("a,b\n1,x\n")
    s = CleaningSession(ds.data)
    for op, params in (("trim_whitespace", {"columns": ["b"]}), ("remove_duplicates", {}),
                       ("standardize_missing", {"columns": ["a"], "tokens": ["NA"]})):
        with pytest.raises(CleaningError) as err:
            s.preview(op, params)
        assert err.value.code == "no_change"
    files = build_files(ds, s)
    check_consistent(ds, s, files)
    assert strict_json(files["comparison"])["identical"] is True and s.version == 0


def test_removing_every_row_or_column_is_refused_and_changes_nothing():
    ds = load("a,b\n,x\n,y\n")
    s = CleaningSession(ds.data)
    for op, params in (("drop_missing_rows", {"columns": ["a"], "how": "any"}),
                       ("drop_columns", {"columns": ["a", "b"]})):
        with pytest.raises(CleaningError) as err:
            s.preview(op, params)
        assert err.value.code == "invalid_params" and "must remain" in err.value.message
    assert s.version == 0 and not s.audit_log


def test_all_missing_and_constant_columns():
    ds = load("empty,const,v\n,5,1\n,5,2\n,5,3\n")
    s = CleaningSession(ds.data)
    with pytest.raises(CleaningError, match="no values"):
        s.preview("fill_missing", {"columns": ["empty"], "method": "mean"})
    apply(s, "convert_type", {"columns": ["const"], "target": "integer", "on_error": "strict"})
    for method, extra in (("iqr", {"multiplier": 1.5}), ("zscore", {"threshold": 3.0}),
                          ("percentile", {"lower_percentile": 1.0, "upper_percentile": 99.0})):
        with pytest.raises(CleaningError) as err:
            s.preview("treat_outliers", {"column": "const", "method": method, "treatment": "cap", **extra})
        assert err.value.code == "no_change"
    apply(s, "drop_missing_columns", {"threshold": 100.0})
    assert s.columns == ("const", "v")
    check_consistent(ds, s, build_files(ds, s))


def test_unicode_long_text_control_characters_and_invalid_dates():
    long_text = "é" * 40_000
    ds = load(f'u,t,d\n"Zoë 東京 🎉",{long_text},2023-02-30\n"=SUM(A1)","a\x01b",not a date\nß,ok,2023-01-01\n')
    s = CleaningSession(ds.data)
    apply(s, "convert_type", {"columns": ["d"], "target": "date", "date_formats": ["ymd"], "on_error": "coerce"})
    assert s.data["d"].isna().tolist() == [True, True, False]
    files = build_files(ds, s)
    check_consistent(ds, s, files)
    table = csv_table(files["csv"])
    assert table[1][:2] == ["Zoë 東京 🎉", long_text] and table[2][:2] == ["=SUM(A1)", "a\x01b"]   # CSV exact
    sheet = sheet_rows(files["xlsx"])
    assert sheet[1][0] == "Zoë 東京 🎉" and len(sheet[1][1]) == 32_767                          # Excel cell limit
    assert sheet[2][0] == "=SUM(A1)" and sheet[2][1] == "a�b"                              # Text, not a formula


def test_nullable_dtypes_duplicate_index_and_infinite_values_at_engine_level():
    """Loaded files never hold these, but the engine and the exports accept any DataFrame."""
    frame = pd.DataFrame({
        "i": pd.array([1, None, 3, 3], dtype="Int64"),
        "t": pd.array(["x", None, "z", "z"], dtype=TEXT_DTYPE),
        "b": pd.array([True, None, False, False], dtype="boolean"),
        "f": pd.array([1.5, None, float("inf"), float("inf")], dtype="Float64"),
    }, index=[0, 0, 1, 1])
    s = CleaningSession(frame)
    assert s.data.index.tolist() == [0, 1, 2, 3]                  # Index labels are never used as identity
    apply(s, "remove_duplicates", {})
    assert s.row_ids.tolist() == [0, 1, 2]
    with pytest.raises(CleaningError, match="non-finite"):
        s.preview("treat_outliers", {"column": "f", "method": "iqr", "multiplier": 1.5, "treatment": "cap"})
    ds = load("i,t,b,f\n1,x,true,1\n")                             # Only for the file name in the builders
    files = {d.key.removeprefix("refine_export_"): d.build()
             for d in dataset_downloads(ds, s) + report_downloads(ds, s, ())}
    assert csv_table(files["csv"])[3] == ["3", "z", "false", "inf"]
    assert sheet_rows(files["xlsx"])[3] == (3, "z", False, "inf")  # Infinity is text in Excel
    for key in ("quality", "comparison"):
        strict_json(files[key])                                    # Valid JSON: no NaN or Infinity tokens


def test_zero_row_table_at_engine_level():
    s = CleaningSession(pd.DataFrame({"a": pd.array([], dtype=TEXT_DTYPE)}))
    ds = load("a\n1\n")
    files = {d.key.removeprefix("refine_export_"): d.build()
             for d in dataset_downloads(ds, s) + record_downloads(ds, s) + report_downloads(ds, s, ())}
    assert csv_table(files["csv"]) == [["a"]] and sheet_rows(files["xlsx"]) == [("a",)]
    assert strict_json(files["comparison"])["identical"] is True
