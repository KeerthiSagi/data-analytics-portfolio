"""Missing values and Duplicates tabs on the Clean page, end to end with AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import CLEAN, HISTORY, session_of


@pytest.fixture
def clean(app):
    load_sample(app)
    app.switch_page(CLEAN).run()
    assert not app.exception, app.exception
    return app


def widget(at, kind, label):
    return next(w for w in getattr(at, kind) if w.label == label)


def choose_missing_op(at, op_id):
    widget(at, "selectbox", "Missing-value operation").set_value(op_id).run()
    assert not at.exception, at.exception
    return at


def confirm(at):
    at.button(key="refine_apply").click().run()
    at.button(key="refine_confirm_apply").click().run()
    assert not at.exception, at.exception
    return at


def test_tabs_render_with_placeholders(clean):
    text = page_text(clean)
    assert "What counts as missing" in text and "Groups and rows" in text
    assert text.count("Not available yet") >= 1          # Phase 5 built Data types and Text; Outliers remains
    assert widget(clean, "selectbox", "Missing-value operation").value == "standardize_missing"
    assert clean.button(key="refine_preview_standardize_missing").disabled


def test_standardize_shows_counts_and_applies_after_confirmation(clean):
    widget(clean, "multiselect", "Columns to check").set_value(["age"]).run()
    found = next(d.value for d in clean.dataframe if "Kind" in d.value.columns)
    assert dict(zip(found["Text found"], found["Cells"])) == {"N/A": 8, "null": 6, "-": 6, "NA": 4, "''": 2}
    widget(clean, "multiselect", "Markers to treat as missing").set_value(["NA", "N/A"]).run()
    assert "Would become missing: 12 cells" in page_text(clean)
    widget(clean, "text_area", "Other markers, one per line").set_value("unknown\nnull").run()
    assert "Would become missing: 31 cells" in page_text(clean)
    fp = session_of(clean).fingerprint
    clean.button(key="refine_preview_standardize_missing").click().run()
    assert "Mark 31 cells as missing in 1 column" in page_text(clean)
    assert session_of(clean).fingerprint == fp                                 # Nothing changed yet
    clean.button(key="refine_apply").click().run()
    assert session_of(clean).steps == ()                                       # Dialog only
    clean.button(key="refine_confirm_apply").click().run()
    s = session_of(clean)
    assert len(s.steps) == 1 and s.steps[0].plan.resolved["matches"] == {"age": 31}
    assert any("Applied step 1" in m.value for m in clean.success)


def test_case_insensitive_matching_from_the_page(clean):
    widget(clean, "multiselect", "Columns to check").set_value(["email"]).run()
    widget(clean, "multiselect", "Markers to treat as missing").set_value(["NA"]).run()
    assert "Would become missing: 7 cells" in page_text(clean)
    widget(clean, "checkbox", "Match case exactly").uncheck().run()
    assert "Would become missing: 7 cells" in page_text(clean)                # Only 'NA' spelled that way
    widget(clean, "text_area", "Other markers, one per line").set_value("NULL").run()
    assert "Would become missing: 13 cells" in page_text(clean)               # 'NULL' also matches 'null'


def test_changed_settings_hide_apply(clean):
    widget(clean, "multiselect", "Columns to check").set_value(["age"]).run()
    widget(clean, "multiselect", "Markers to treat as missing").set_value(["NA"]).run()
    clean.button(key="refine_preview_standardize_missing").click().run()
    assert any(b.key == "refine_apply" for b in clean.button)
    widget(clean, "multiselect", "Markers to treat as missing").set_value(["NA", "null"]).run()
    assert "Selection changed" in page_text(clean)
    assert not any(b.key == "refine_apply" for b in clean.button)


def test_switching_operation_discards_the_preview(clean):
    widget(clean, "multiselect", "Columns to check").set_value(["age"]).run()
    widget(clean, "multiselect", "Markers to treat as missing").set_value(["NA"]).run()
    clean.button(key="refine_preview_standardize_missing").click().run()
    choose_missing_op(clean, "fill_missing")
    assert not any(b.key == "refine_apply" for b in clean.button)


def test_fill_mean_explains_numeric_parsing_and_rejects_text(clean):
    choose_missing_op(clean, "fill_missing")
    widget(clean, "multiselect", "Columns to fill").set_value(["annual_spend"]).run()
    widget(clean, "selectbox", "Fill with").set_value("mean").run()
    assert "Numbers are read from text" in page_text(clean)
    info = next(d.value for d in clean.dataframe if "Plain numbers" in d.value.columns)
    assert info["Missing cells"].tolist() == [7] and info["Other values"].iloc[0] > 0
    clean.button(key="refine_preview_fill_missing").click().run()
    assert any("not plain numbers" in e.value or "are not" in e.value for e in clean.error)
    assert not any(b.key == "refine_apply" for b in clean.button)
    widget(clean, "checkbox", "Calculate from plain numbers only, leaving other values as they are").check().run()
    clean.button(key="refine_preview_fill_missing").click().run()
    assert "Fill 7 missing cells in 'annual_spend' with the mean" in page_text(clean)
    assert "Fill values (frozen in the step)" in page_text(clean)
    confirm(clean)
    assert session_of(clean).steps[0].plan.resolved["cells"] == {"annual_spend": 7}


def test_fill_constant_and_forward_fill_warning(clean):
    choose_missing_op(clean, "fill_missing")
    widget(clean, "multiselect", "Columns to fill").set_value(["email"]).run()
    assert clean.button(key="refine_preview_fill_missing").disabled             # Needs a value
    widget(clean, "text_input", "Fill value").set_value("unknown@example.com").run()
    clean.button(key="refine_preview_fill_missing").click().run()
    sample = next(d.value for d in clean.dataframe if "email (after)" in d.value.columns)
    assert set(sample["email (after)"]) == {"unknown@example.com"}
    widget(clean, "selectbox", "Fill with").set_value("ffill").run()
    assert "Depends on row order" in page_text(clean)
    clean.button(key="refine_preview_fill_missing").click().run()
    assert "depends on the current row order" in page_text(clean)


def test_drop_rows_and_threshold_columns(clean):
    choose_missing_op(clean, "drop_missing_rows")
    widget(clean, "multiselect", "Columns to check for missing values").set_value(["email"]).run()
    clean.button(key="refine_preview_drop_missing_rows").click().run()
    text = page_text(clean)
    assert "Remove 12 rows with missing a value in 'email'." in text and "2.4% of 500 rows" in text
    confirm(clean)
    assert session_of(clean).shape == (488, 9)

    choose_missing_op(clean, "drop_missing_columns")
    widget(clean, "slider", "Drop columns with at least this share of missing values (%)").set_value(100).run()
    assert "0 columns at or above 100%" in page_text(clean)
    assert clean.button(key="refine_preview_drop_missing_columns").disabled


def test_indicator_flow_and_history(clean):
    choose_missing_op(clean, "add_missing_indicator")
    widget(clean, "selectbox", "Column").set_value("email").run()
    clean.button(key="refine_preview_add_missing_indicator").click().run()
    assert "Add column 'email_missing' after 'email'" in page_text(clean)
    confirm(clean)
    assert session_of(clean).columns[3] == "email_missing"
    clean.switch_page(HISTORY).run()
    steps = next(d.value for d in clean.dataframe if "Step" in d.value.columns)
    assert steps["Operation"].tolist() == ["Add missing-value indicator"]
    clean.button(key="refine_verify_replay").click().run()
    assert any("reproduced the current data exactly" in m.value for m in clean.success)
    clean.button(key="refine_undo").click().run()
    assert session_of(clean).shape == (500, 9)


def test_find_duplicates_distinguishes_groups_and_rows(clean):
    fp = session_of(clean).fingerprint
    clean.button(key="refine_find_duplicates").click().run()
    text = page_text(clean)
    assert "Duplicate groups" in text and "Rows in groups" in text
    examples = next(d.value for d in clean.dataframe if "Duplicate group" in d.value.columns)
    assert examples.index.name == "Original row"
    report = clean.session_state["refine_duplicate_report"][2]
    assert (report.groups, report.duplicate_rows) == (12, 12)
    assert session_of(clean).fingerprint == fp                               # Finding changes nothing


def test_key_based_removal_keep_last_with_confirmation(clean):
    widget(clean, "radio", "Match rows on").set_value("key").run()
    assert clean.button(key="refine_find_duplicates").disabled
    widget(clean, "multiselect", "Key columns").set_value(["customer_id"]).run()
    widget(clean, "checkbox", "Ignore case").check().run()
    clean.button(key="refine_find_duplicates").click().run()
    report = clean.session_state["refine_duplicate_report"][2]
    assert report.duplicate_rows == 20 and report.groups == 20
    widget(clean, "radio", "In each group, keep").set_value("last").run()
    clean.button(key="refine_preview_remove_duplicates").click().run()
    sample = next(d.value for d in clean.dataframe if "Outcome" in d.value.columns)
    assert set(sample["Outcome"]) == {"Kept", "Removed"}
    assert session_of(clean).shape == (500, 9)
    confirm(clean)
    s = session_of(clean)
    assert s.shape == (480, 9) and s.steps[0].plan.resolved["keep"] == "last"
    assert "Rows after removal" not in page_text(clean)                      # Old count no longer shown


def test_no_duplicates_is_reported_without_a_step(clean):
    clean.button(key="refine_preview_remove_duplicates").click().run()        # Exact copies first
    confirm(clean)
    widget(clean, "radio", "Match rows on").set_value("key").run()
    widget(clean, "multiselect", "Key columns").set_value(["customer_id", "email"]).run()
    clean.button(key="refine_find_duplicates").click().run()
    assert "No duplicates found" in page_text(clean)
    clean.button(key="refine_preview_remove_duplicates").click().run()
    assert any("no duplicates to remove" in m.value for m in clean.info)
    assert not any(b.key == "refine_apply" for b in clean.button)
    assert len(session_of(clean).steps) == 1
