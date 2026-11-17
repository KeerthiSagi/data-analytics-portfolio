"""Data types and Text tabs on the Clean page, end to end with AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import CLEAN, session_of


@pytest.fixture
def clean(app):
    load_sample(app)
    app.switch_page(CLEAN).run()
    assert not app.exception, app.exception
    return app


def key(at, name):
    s = session_of(at)
    return f"refine_{name}_{s.session_id[:8]}_{s.version}"


def run(at):
    at.run()
    assert not at.exception, at.exception
    return at


def confirm(at):
    at.button(key="refine_apply").click()
    run(at)
    at.button(key="refine_confirm_apply").click()
    return run(at)


def frames_with(at, column):
    return [d.value for d in at.dataframe if column in d.value.columns]


# ---- Data types -------------------------------------------------------

def test_tabs_render(clean):
    text = page_text(clean)
    assert "Data types" in text and "Never guess" not in text
    assert text.count("Not available yet") == 0                              # Outliers is built (Phase 6)
    assert clean.button(key="refine_preview_convert_type").disabled          # Nothing chosen yet
    assert clean.button(key="refine_preview_trim_whitespace").disabled


def test_numeric_conversion_shows_failures_before_preview_and_applies(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["annual_spend"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("decimal")
    run(clean)
    analysis = frames_with(clean, "Would convert")[0]
    assert analysis.loc[0, "Cannot convert"] > 0                             # "$1,579.48" without options
    reasons = " ".join(frames_with(clean, "Why")[0]["Why"])
    assert "currency" in reasons and "thousands" in reasons
    clean.selectbox(key=key(clean, "ct_thousands")).set_value(",")
    clean.checkbox(key=key(clean, "ct_currency")).check()
    clean.radio(key=key(clean, "ct_on_error")).set_value("coerce")
    run(clean)
    fp = session_of(clean).fingerprint
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    text = page_text(clean)
    assert "Convert 'annual_spend' to Decimal number (Float64)" in text and "Values converted" in text
    assert session_of(clean).fingerprint == fp                               # Preview changes nothing
    confirm(clean)
    s = session_of(clean)
    assert len(s.steps) == 1 and str(s.data["annual_spend"].dtype) == "Float64"
    assert any("Applied step 1" in m.value for m in clean.success)


def test_strict_policy_error_is_shown_and_nothing_changes(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["age"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("integer")
    run(clean)
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    assert any("Refuse the step" in e.value and "unknown" in e.value for e in clean.error)
    assert session_of(clean).steps == ()


def test_preserve_policy_is_explained_and_disabled(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["age"])
    clean.radio(key=key(clean, "ct_on_error")).set_value("preserve")
    run(clean)
    assert "Not possible for typed columns" in page_text(clean)
    assert clean.button(key="refine_preview_convert_type").disabled


def test_dates_require_an_explicit_format(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["signup_date"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("date")
    run(clean)
    assert "Dates are never guessed" in page_text(clean)
    assert clean.button(key="refine_preview_convert_type").disabled
    clean.multiselect(key=key(clean, "ct_formats")).set_value(["ymd", "mdy"])
    run(clean)
    assert not clean.button(key="refine_preview_convert_type").disabled
    assert frames_with(clean, "Ambiguous")


def test_boolean_needs_mappings_then_converts(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["is_active"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("boolean")
    run(clean)
    assert clean.button(key="refine_preview_convert_type").disabled
    clean.multiselect(key=key(clean, "ct_presets")).set_value(["true/false", "yes/no", "y/n", "1/0"])
    run(clean)
    assert frames_with(clean, "Would convert")[0].loc[0, "Cannot convert"] == 0
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    confirm(clean)
    assert str(session_of(clean).data["is_active"].dtype) == "boolean"


def test_mixed_kinds_show_a_clear_message(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["is_active"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("boolean")
    run(clean)
    clean.multiselect(key=key(clean, "ct_presets")).set_value(["true/false", "yes/no", "y/n", "1/0"])
    run(clean)
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    confirm(clean)
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["is_active", "age"])
    run(clean)
    assert "one kind at a time" in page_text(clean) + " ".join(c.value for c in clean.caption)


# ---- Text ------------------------------------------------------------

def choose_text_op(at, op_id):
    at.selectbox(key="refine_text_operation").set_value(op_id)
    return run(at)


def test_trim_shows_counts_and_applies(clean):
    clean.multiselect(key=key(clean, "tx_trim_whitespace_columns")).set_value(["segment", "country"])
    run(clean)
    assert "Would change:" in page_text(clean)
    clean.button(key="refine_preview_trim_whitespace").click()
    run(clean)
    assert "Trim leading and trailing whitespace" in page_text(clean)
    confirm(clean)
    data = session_of(clean).data
    assert not data["segment"].str.endswith(" ").any()


def test_typed_columns_are_not_offered_for_text_rules(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["is_active"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("boolean")
    run(clean)
    clean.multiselect(key=key(clean, "ct_presets")).set_value(["true/false", "yes/no", "y/n", "1/0"])
    run(clean)
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    confirm(clean)
    options = clean.multiselect(key=key(clean, "tx_trim_whitespace_columns")).options
    assert "is_active" not in options and "segment" in options
    assert any("Typed columns are not listed" in c.value for c in clean.caption)


def test_change_case_no_change_message(clean):
    choose_text_op(clean, "change_case")
    clean.multiselect(key=key(clean, "tx_change_case_columns")).set_value(["customer_id"])
    clean.radio(key=key(clean, "tx_case")).set_value("upper")
    run(clean)
    clean.button(key="refine_preview_change_case").click()
    run(clean)
    assert any("nothing to apply" in i.value for i in clean.info)


def test_find_replace_requires_text_to_find(clean):
    choose_text_op(clean, "find_replace")
    clean.multiselect(key=key(clean, "tx_find_replace_columns")).set_value(["country"])
    run(clean)
    assert clean.button(key="refine_preview_find_replace").disabled
    clean.text_input(key=key(clean, "tx_find")).input("UK")
    clean.text_input(key=key(clean, "tx_replace")).input("United Kingdom")
    run(clean)
    clean.button(key="refine_preview_find_replace").click()
    run(clean)
    confirm(clean)
    assert "UK" not in set(session_of(clean).data["country"].dropna())


def test_category_mapping_is_explicit(clean):
    choose_text_op(clean, "map_categories")
    clean.selectbox(key=key(clean, "mc_column")).set_value("segment")
    run(clean)
    assert clean.button(key="refine_preview_map_categories").disabled         # Suggestions are not applied
    assert "Suggested (case/spacing)" in frames_with(clean, "Suggested (case/spacing)")[0].columns
    clean.button(key=key(clean, "mc_add_suggestions")).click()
    run(clean)
    clean.multiselect(key=key(clean, "mc_values")).set_value(["consumer"])
    clean.text_input(key=key(clean, "mc_target")).input("Consumer")
    run(clean)
    clean.button(key=key(clean, "mc_add")).click()
    run(clean)
    assert "Mappings to apply" in page_text(clean)
    clean.button(key="refine_preview_map_categories").click()
    run(clean)
    assert "Mappings (frozen in the step)" in page_text(clean)
    confirm(clean)
    s = session_of(clean)
    assert s.steps[-1].plan.operation == "map_categories"
    assert "CONSUMER " not in set(s.data["segment"].dropna())


def test_undo_from_history_restores_text_dtype(clean):
    clean.multiselect(key=key(clean, "ct_columns")).set_value(["is_active"])
    clean.selectbox(key=key(clean, "ct_target")).set_value("boolean")
    run(clean)
    clean.multiselect(key=key(clean, "ct_presets")).set_value(["true/false", "yes/no", "y/n", "1/0"])
    run(clean)
    clean.button(key="refine_preview_convert_type").click()
    run(clean)
    confirm(clean)
    s = session_of(clean)
    s.undo()
    assert s.fingerprint == s.original_fingerprint
