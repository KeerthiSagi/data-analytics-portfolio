"""Phase 9: workflows across the Streamlit pages, with Streamlit's AppTest.

Covers what only the running app can show: a pending preview surviving a
visit to Compare and Export (Streamlit clears a page's widget values on a
page switch unless they persist), the notices that say a preview is not
applied, and that the pages share their cached results without serving a
stale one after apply, undo or reset.
"""

from __future__ import annotations

import pytest

from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import session_of
from tests.test_clean_phase5_pages import run

CLEAN, COMPARE, EXPORT, PROFILE, HISTORY = ("views/clean.py", "views/compare.py", "views/export.py",
                                            "views/profile.py", "views/history.py")


def go(at, page):
    at.switch_page(page)
    return run(at)


def text_columns(at):
    return next(m for m in at.multiselect if m.label == "Text columns")


def preview_trim(at):
    """Configure and preview Trim whitespace on full_name, in the Text tab (not the first tab)."""
    go(at, CLEAN)
    at.selectbox(key="refine_text_operation").set_value("trim_whitespace")
    run(at)
    text_columns(at).set_value(["full_name"])
    run(at)
    at.button(key="refine_preview_trim_whitespace").click()
    run(at)
    assert any(b.key == "refine_apply" for b in at.button)


def pending(at):
    from ui.state import PREVIEW_KEY
    return at.session_state[PREVIEW_KEY] if PREVIEW_KEY in at.session_state else None


@pytest.fixture
def sample(app):
    load_sample(app)
    assert not app.exception, app.exception
    return app


# ---- Workflow C: pending preview and navigation --------------------------------

def test_pending_preview_survives_compare_and_export_and_can_still_be_applied(sample):
    at = sample
    preview_trim(at)
    preview = pending(at)
    s = session_of(at)
    state = (s.version, s.fingerprint, len(s.steps))

    go(at, COMPARE)
    assert "A previewed step is not included" in page_text(at) and "Trim whitespace" in page_text(at)
    from ui.state import COMPARISON_KEY
    assert at.session_state[COMPARISON_KEY][1].cells.cells_changed == 0       # The preview is not compared

    go(at, EXPORT)
    assert "A previewed step is not applied" in page_text(at)
    assert (s.version, s.fingerprint, len(s.steps)) == state                  # Visiting pages applied nothing

    go(at, CLEAN)
    assert pending(at) is preview and s.is_current(preview)                   # Not discarded either
    assert text_columns(at).value == ["full_name"]                            # Settings kept across pages
    assert at.selectbox(key="refine_text_operation").value == "trim_whitespace"
    text = page_text(at)
    assert "A previewed step is waiting" in text and "Open the Text tab" in text
    assert "Selection changed" not in text
    at.button(key="refine_apply").click()
    run(at)
    at.button(key="refine_confirm_apply").click()
    run(at)
    assert len(s.steps) == 1 and s.steps[0].plan.operation == "trim_whitespace"
    assert pending(at) is None and "A previewed step is waiting" not in page_text(at)

    go(at, COMPARE)
    assert "A previewed step is not included" not in page_text(at)
    assert at.session_state[COMPARISON_KEY][1].cells.cells_changed > 0        # Now the step is compared


def test_pending_preview_can_be_discarded_after_navigation(sample):
    at = sample
    preview_trim(at)
    go(at, HISTORY)
    go(at, CLEAN)
    at.button(key="refine_discard_preview").click()
    run(at)
    assert pending(at) is None and not session_of(at).steps
    go(at, EXPORT)
    assert "A previewed step is not applied" not in page_text(at)


def test_a_preview_made_stale_by_undo_is_discarded_not_applied(sample):
    at = sample
    s = session_of(at)
    s.apply(s.preview("remove_duplicates", {}), confirmed=True)
    preview_trim(at)
    go(at, HISTORY)
    at.button(key="refine_undo").click()
    run(at)
    go(at, CLEAN)
    assert pending(at) is None and "preview was discarded" in " ".join(w.value for w in at.warning)
    assert not s.steps


# ---- Caches shared by the pages ---------------------------------------------

def test_original_profile_from_the_profile_page_is_reused_after_cleaning(sample):
    from ui.state import ORIGINAL_REPORTS_KEY
    at = sample
    go(at, PROFILE)
    s = session_of(at)
    stored = at.session_state[ORIGINAL_REPORTS_KEY]
    assert stored[0][0] == s.original_fingerprint
    original_profile = stored[1]
    s.apply(s.preview("remove_duplicates", {}), confirmed=True)

    go(at, EXPORT)
    from ui.state import EXPORT_SLOT_KEY
    slot = at.session_state[EXPORT_SLOT_KEY][1]
    assert slot.value is None                                     # Nothing is computed until a download
    comparison = slot.get()                                       # What the first report download does
    assert comparison.original_profile is original_profile        # The original was not profiled again
    assert comparison.metric("rows").current == 488

    run(at)                                                       # A rerun of the Export page keeps it
    assert at.session_state[EXPORT_SLOT_KEY][1] is slot
    go(at, COMPARE)                                               # ... and so does the Compare page
    from ui.state import COMPARISON_KEY
    assert at.session_state[COMPARISON_KEY][1] is comparison
    assert "488" in page_text(at)


def test_caches_follow_undo_and_reset(sample):
    from ui.state import COMPARISON_KEY, EXPORT_SLOT_KEY
    at = sample
    s = session_of(at)
    s.apply(s.preview("remove_duplicates", {}), confirmed=True)
    go(at, EXPORT)
    at.session_state[EXPORT_SLOT_KEY][1].get()
    go(at, COMPARE)
    assert at.session_state[COMPARISON_KEY][1].metric("rows").current == 488

    s.undo()
    go(at, EXPORT)
    slot = at.session_state[EXPORT_SLOT_KEY][1]
    assert slot.value is None or slot.value.metric("rows").current == 500   # Never the 488-row comparison
    go(at, COMPARE)
    comparison = at.session_state[COMPARISON_KEY][1]
    assert comparison.metric("rows").current == 500 and comparison.identical

    s.apply(s.preview("drop_columns", {"columns": ["segment"]}), confirmed=True)
    s.reset()
    go(at, COMPARE)
    assert at.session_state[COMPARISON_KEY][1].identical
    go(at, EXPORT)
    assert at.session_state[EXPORT_SLOT_KEY][1].get().identical


def test_loading_another_file_clears_every_cache(sample):
    from ui.state import CLEANING_KEYS, EXPORT_SLOT_KEY
    at = sample
    go(at, EXPORT)
    assert EXPORT_SLOT_KEY in at.session_state
    go(at, "views/upload.py")                                     # Close is on the Upload page
    at.button(key="refine_close").click()
    run(at)
    for key in CLEANING_KEYS:
        assert key not in at.session_state


# ---- Every page after a full workflow ---------------------------------------------

def test_every_page_renders_after_a_full_workflow(sample):
    at = sample
    s = session_of(at)
    for op, params in (
        ("standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]}),
        ("remove_duplicates", {}),
        ("trim_whitespace", {"columns": ["full_name"]}),
        ("convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"}),
        ("treat_outliers", {"column": "age", "method": "iqr", "multiplier": 1.5, "treatment": "remove_rows"}),
    ):
        s.apply(s.preview(op, params), confirmed=True)
    rows = s.shape[0]
    for page in ("views/upload.py", PROFILE, CLEAN, COMPARE, HISTORY, EXPORT):
        go(at, page)
        assert not at.exception and not at.error, (page, at.exception, at.error)
    go(at, PROFILE)
    from ui.state import COMPARISON_KEY, PROFILE_KEY
    profile = at.session_state[PROFILE_KEY][1]
    go(at, COMPARE)
    comparison = at.session_state[COMPARISON_KEY][1]
    assert comparison.current_profile is profile                  # One profile, shared by both pages
    assert comparison.metric("rows").current == profile.n_rows == rows
    assert comparison.metric("missing_cells").current == profile.missing_cells


# ---- Deployment settings ---------------------------------------------------------

def test_config_keeps_tracebacks_out_of_the_browser():
    import tomllib
    from tests.conftest import PROJECT_DIR
    config = tomllib.loads((PROJECT_DIR / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["client"]["showErrorDetails"] == "type"
    assert "enableXsrfProtection" not in config.get("server", {})          # Streamlit's defaults (on) are kept
    assert "enableCORS" not in config.get("server", {})
