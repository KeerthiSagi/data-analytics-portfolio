"""The Export page, end to end with Streamlit's AppTest.

AppTest does not run download callbacks, so the files themselves are
tested through the same builders in tests/test_session_phase8.py. Here:
the page, its buttons, notes and empty states, and that it changes nothing.
"""

from dataclasses import replace

import pytest

from refine.loader import load_file
from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import session_of
from tests.test_clean_phase5_pages import run

EXPORT = "views/export.py"
LABELS = ["Download CSV", "Download Excel", "Download history", "Download recipe", "Download quality report",
          "Download comparison report"]
SECTIONS = ("Cleaned dataset", "Cleaning records", "Reports")


def buttons(at):
    return {b.label: b for b in at.get("download_button")}


def captions(at) -> str:
    return " ".join(c.value for c in at.caption)


def session_state(at):
    s = session_of(at)
    return (s.version, s.fingerprint, len(s.steps), len(s.audit_log), s.recipe_json(), s.snapshot_stats)


def apply(at, op, params):
    s = session_of(at)
    s.apply(s.preview(op, params), confirmed=True)


@pytest.fixture
def export(app):
    load_sample(app)
    app.switch_page(EXPORT).run()
    assert not app.exception, app.exception
    return app


def open_file(app, name: str, text: str):
    from ui.state import DATASET_KEY
    app.session_state[DATASET_KEY] = load_file(name, text.encode("utf-8"))
    app.switch_page(EXPORT).run()
    assert not app.exception, app.exception
    return app


def test_no_dataset_shows_empty_state(app):
    app.switch_page(EXPORT).run()
    assert not app.exception
    assert "No dataset loaded" in page_text(app) and not app.get("download_button")


def test_page_loads_with_every_download(export):
    text = page_text(export)
    for heading in SECTIONS:
        assert heading in text
    assert list(buttons(export)) == LABELS
    assert not any(b.proto.disabled for b in buttons(export).values())
    assert all(b.proto.ignore_rerun for b in buttons(export).values())    # Downloading does not rerun the page
    assert "Not available yet" not in text


def test_original_data_can_be_exported_without_cleaning(export):
    assert "No cleaning steps applied" in page_text(export)
    names = captions(export)
    for name in ("messy_customers_original.csv", "messy_customers_original.xlsx", "messy_customers_history.json",
                 "messy_customers_recipe.json", "messy_customers_quality_report.json",
                 "messy_customers_comparison_report.json"):
        assert name in names
    assert "the history is empty" in names and "gives back the original data" in names


def test_after_cleaning_names_and_counts(export):
    apply(export, "remove_duplicates", {})
    apply(export, "drop_columns", {"columns": ["segment"]})
    run(export)
    text = page_text(export)
    assert "No cleaning steps applied" not in text
    assert ">488<" in text and ">8<" in text and "Original 500" in text
    names = captions(export)
    assert "messy_customers_cleaned.csv" in names and "messy_customers_cleaned.xlsx" in names
    assert "2 audit events" in names and "2 active steps" in names and "2 steps with every frozen value" in names
    assert session_of(export).fingerprint[:12] in text


def test_csv_and_excel_notes(export):
    text, names = page_text(export), captions(export)
    assert "column types are not kept" in names and "Missing values and empty text are both empty fields" in names
    assert "never as a formula" in names and "Cleaned Data" in names
    assert "could run as spreadsheet formulas" not in text       # The sample has no formula-like text
    assert "stored differently in Excel" not in text
    assert not export.toggle


def test_formula_warning_and_escape_toggle(app):
    open_file(app, "f.csv", "name,note\nAna,=1+1\nBo,@SUM(A1)\nCy,-5\n")
    text = page_text(app)
    assert "2 cells could run as spreadsheet formulas" in text and "note" in text
    toggle = next(t for t in app.toggle if t.label == "Escape formula-like text")
    assert toggle.value is False
    assert "will start with an apostrophe" not in captions(app)
    toggle.set_value(True)
    run(app)
    assert "2 cells will start with an apostrophe" in captions(app)
    assert session_of(app).data["note"].tolist() == ["=1+1", "@SUM(A1)", "-5"]   # Only the file changes


def test_excel_notes_for_typed_values(app):
    open_file(app, "n.csv", "big,ratio\n1234567890123456,0.1\n7,0.30000000000000004\n")
    apply(app, "convert_type", {"columns": ["big"], "target": "integer"})
    apply(app, "convert_type", {"columns": ["ratio"], "target": "decimal"})
    run(app)
    text = page_text(app)
    assert "stored differently in Excel" in text
    assert "1 whole number with 16 or more digits is written as text" in text
    assert "1 decimal value needs more than 15 significant digits" in text


def test_unsupported_excel_export_is_disabled(export):
    from ui.state import EXPORT_CHECK_KEY
    fp, check = export.session_state[EXPORT_CHECK_KEY]
    export.session_state[EXPORT_CHECK_KEY] = (fp, replace(check, excel=replace(
        check.excel, limit_error="The table has 2,000,000 rows. An Excel worksheet holds at most 1,048,575 rows "
                                 "below the header row.")))
    run(export)
    assert "Too large for an Excel worksheet" in page_text(export)
    assert buttons(export)["Download Excel"].proto.disabled
    assert not buttons(export)["Download CSV"].proto.disabled


def test_pending_preview_is_mentioned_and_kept(export):
    from ui.state import PREVIEW_KEY
    s = session_of(export)
    preview = s.preview("remove_duplicates", {})
    export.session_state[PREVIEW_KEY] = preview
    run(export)
    assert "A previewed step is not applied" in page_text(export)
    assert "Remove duplicate rows" in page_text(export)
    assert export.session_state[PREVIEW_KEY] is preview and s.is_current(preview)
    assert ">500<" in page_text(export)                       # The confirmed rows, not the preview's 488
    assert not export.warning                                 # No "preview discarded" message


def test_downloads_trigger_no_cleaning(export):
    apply(export, "remove_duplicates", {})
    run(export)
    before = session_state(export)
    for label in LABELS:
        buttons(export)[label].click()
        run(export)
    assert session_state(export) == before


def test_page_caches_its_check_and_computes_no_comparison(export):
    from ui.state import COMPARISON_KEY, EXPORT_CHECK_KEY
    s = session_of(export)
    assert export.session_state[EXPORT_CHECK_KEY][0] == s.fingerprint
    assert COMPARISON_KEY not in export.session_state          # Reports are built only on a click
    cached = export.session_state[EXPORT_CHECK_KEY][1]
    run(export)
    assert export.session_state[EXPORT_CHECK_KEY][1] is cached
    apply(export, "remove_duplicates", {})
    run(export)
    assert export.session_state[EXPORT_CHECK_KEY][0] == session_of(export).fingerprint
    assert export.session_state[EXPORT_CHECK_KEY][1].rows == 488


@pytest.fixture
def registered(monkeypatch):
    """The deferred downloads the page registers with Streamlit: file name -> (mime, callable)."""
    from streamlit.runtime.media_file_manager import MediaFileManager
    seen = {}
    original = MediaFileManager.add_deferred

    def record(self, callable_, mimetype, coordinates, file_name=None, **kwargs):   # Streamlit 1.67 adds element_id
        seen[file_name] = (mimetype, callable_)
        return original(self, callable_, mimetype, coordinates, file_name=file_name, **kwargs)

    monkeypatch.setattr(MediaFileManager, "add_deferred", record)
    return seen


def test_clicked_downloads_produce_the_current_files(export, registered):
    import csv
    import io
    import json

    import openpyxl
    apply(export, "remove_duplicates", {})
    preview_session = session_of(export)
    from ui.state import PREVIEW_KEY
    export.session_state[PREVIEW_KEY] = preview_session.preview("drop_columns", {"columns": ["segment"]})
    run(export)
    before = session_state(export)
    files = {name: (mime, build()) for name, (mime, build) in registered.items()}   # As Streamlit runs them
    assert session_state(export) == before
    mime, data = files["messy_customers_cleaned.csv"]
    assert mime == "text/csv"
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    assert len(rows) == 1 + 488 and "segment" in rows[0]            # Confirmed data, not the pending preview
    mime, data = files["messy_customers_cleaned.xlsx"]
    assert mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert openpyxl.load_workbook(io.BytesIO(data)).active.max_row == 1 + 488
    for name in ("messy_customers_history.json", "messy_customers_recipe.json",
                 "messy_customers_quality_report.json", "messy_customers_comparison_report.json"):
        mime, data = files[name]
        assert mime == "application/json" and json.loads(data)
    assert json.loads(files["messy_customers_recipe.json"][1]) == session_of(export).recipe()


def test_compare_page_then_export_reuses_the_comparison(export):
    from ui.state import COMPARISON_KEY
    export.switch_page("views/compare.py").run()
    assert COMPARISON_KEY in export.session_state
    export.switch_page(EXPORT).run()
    assert not export.exception
    assert list(buttons(export)) == LABELS
