"""Outliers tab on the Clean page, end to end with AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import CLEAN, session_of
from tests.test_clean_phase5_pages import confirm, frames_with, key, run


@pytest.fixture
def clean(app):
    load_sample(app)
    app.switch_page(CLEAN).run()
    assert not app.exception, app.exception
    return app


def convert_numbers(at):
    """Convert 'age' to Int64 through the engine (the Data types tab has its own tests)."""
    s = session_of(at)
    s.apply(s.preview("convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"}), confirmed=True)
    return run(at)


def choose(at, column="age", method=None):
    at.selectbox(key=key(at, "ol_column")).set_value(column)
    if method:
        at.selectbox(key=key(at, "ol_method")).set_value(method)
    return run(at)


def preview(at):
    at.button(key="refine_preview_treat_outliers").click()
    return run(at)


def test_no_numeric_columns_message(clean):
    assert "No numeric columns" in page_text(clean)                 # The sample loads as text
    assert "Data types tab" in page_text(clean)


def test_only_numeric_columns_are_offered(clean):
    convert_numbers(clean)
    box = clean.selectbox(key=key(clean, "ol_column"))
    assert box.options == ["age"] and box.value is None
    assert clean.button(key="refine_preview_treat_outliers").disabled


def test_detection_summary_and_capping_flow(clean):
    convert_numbers(clean)
    choose(clean)
    text = page_text(clean)
    assert "Detection summary" in text and "Lower boundary" in text and "Upper boundary" in text
    assert "-12" in text and "116" in text and "Missing values" in text
    fp = session_of(clean).fingerprint
    preview(clean)
    text = page_text(clean)
    assert "Treat outliers" in text and "Cap 4 outliers in 'age' outside -12 to 116" in text
    assert frames_with(clean, "Setting")                            # Frozen detection table
    assert session_of(clean).fingerprint == fp                      # Preview changes nothing
    confirm(clean)
    s = session_of(clean)
    assert s.steps[-1].plan.operation == "treat_outliers" and s.data["age"].max() == 116
    assert any("Applied step 2" in m.value for m in clean.success)


def test_method_parameters_follow_the_method(clean):
    convert_numbers(clean)
    choose(clean, method="zscore")
    assert clean.number_input(key=key(clean, "ol_threshold")).value == 3.0
    assert not [n for n in clean.number_input if n.key == key(clean, "ol_multiplier")]
    choose(clean, method="percentile")
    assert clean.number_input(key=key(clean, "ol_lower_percentile")).value == 1.0
    assert clean.number_input(key=key(clean, "ol_upper_percentile")).value == 99.0
    assert not [n for n in clean.number_input if n.key == key(clean, "ol_threshold")]


def test_invalid_parameters_are_explained_and_disable_preview(clean):
    convert_numbers(clean)
    choose(clean)
    clean.number_input(key=key(clean, "ol_multiplier")).set_value(0.0)
    run(clean)
    assert "Cannot detect outliers with these settings" in page_text(clean)
    assert clean.button(key="refine_preview_treat_outliers").disabled
    choose(clean, method="percentile")
    clean.number_input(key=key(clean, "ol_lower_percentile")).set_value(60.0)
    clean.number_input(key=key(clean, "ol_upper_percentile")).set_value(40.0)
    run(clean)
    assert "Percentiles must satisfy" in page_text(clean)
    assert clean.button(key="refine_preview_treat_outliers").disabled


def test_replacement_value_is_required_and_validated(clean):
    convert_numbers(clean)
    choose(clean)
    clean.radio(key=key(clean, "ol_treatment")).set_value("replace_value")
    run(clean)
    assert clean.button(key="refine_preview_treat_outliers").disabled
    clean.text_input(key=key(clean, "ol_value")).input("abc")
    run(clean)
    preview(clean)
    assert any("is not a number" in e.value for e in clean.error)
    assert len(session_of(clean).steps) == 1
    clean.text_input(key=key(clean, "ol_value")).input("40.5")
    run(clean)
    preview(clean)
    assert any("whole numbers" in e.value for e in clean.error)    # Never rounded silently
    clean.text_input(key=key(clean, "ol_value")).input("40")
    run(clean)
    preview(clean)
    confirm(clean)
    assert session_of(clean).data["age"].max() == 82


def test_remove_rows_shows_rows_removed(clean):
    convert_numbers(clean)
    choose(clean)
    clean.radio(key=key(clean, "ol_treatment")).set_value("remove_rows")
    run(clean)
    preview(clean)
    text = page_text(clean)
    assert "Rows removed" in text and "Possible data loss" in text and "500 → 496" in text
    confirm(clean)
    assert session_of(clean).shape == (496, 9)


def test_no_outliers_disables_preview(clean):
    convert_numbers(clean)
    choose(clean, method="percentile")
    clean.number_input(key=key(clean, "ol_lower_percentile")).set_value(0.0)
    clean.number_input(key=key(clean, "ol_upper_percentile")).set_value(100.0)
    run(clean)
    assert "nothing to treat" in " ".join(c.value for c in clean.caption)
    assert clean.button(key="refine_preview_treat_outliers").disabled


def test_changed_settings_and_stale_preview(clean):
    convert_numbers(clean)
    choose(clean)
    preview(clean)
    clean.number_input(key=key(clean, "ol_multiplier")).set_value(3.0)
    run(clean)
    assert "Selection changed" in page_text(clean)                  # The preview no longer matches the widgets
    session_of(clean).undo()                                        # The data changes under the preview
    run(clean)
    assert any("data changed" in w.value for w in clean.warning)
    assert len(session_of(clean).steps) == 0
