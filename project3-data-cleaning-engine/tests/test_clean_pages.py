"""Clean and History pages end to end with Streamlit's AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text, sidebar_text  # noqa: F401  (app is a fixture)

CLEAN = "views/clean.py"
HISTORY = "views/history.py"
PROFILE = "views/profile.py"


def session_of(at):
    from ui.state import CLEANING_KEY
    return at.session_state[CLEANING_KEY][1]


def columns_widget(at):
    return next(m for m in at.multiselect if m.label == "Columns to drop")


@pytest.fixture
def clean(app):
    load_sample(app)
    app.switch_page(CLEAN).run()
    assert not app.exception, app.exception
    return app


def preview(at, cols):
    columns_widget(at).set_value(cols).run()
    at.button(key="refine_preview").click().run()
    assert not at.exception, at.exception
    return at


def apply_confirmed(at, cols):
    preview(at, cols)
    at.button(key="refine_apply").click().run()
    at.button(key="refine_confirm_apply").click().run()
    assert not at.exception, at.exception
    return at


def test_clean_page_renders(clean):
    text = page_text(clean)
    assert "Choose a step" in text and "Not available yet" in text      # Later steps still planned
    assert next(s for s in clean.selectbox if s.label == "Operation").value == "drop_columns"
    assert columns_widget(clean).options == ["customer_id", "full_name", "email", "signup_date", "country",
                                              "age", "annual_spend", "is_active", "segment"]
    assert clean.button(key="refine_preview").disabled                  # Nothing selected yet


def test_preview_shows_impact_and_changes_nothing(clean):
    fp = session_of(clean).fingerprint
    preview(clean, ["email", "age"])
    text = page_text(clean)
    assert "Drop 2 columns: 'email', 'age'." in text
    assert "500 → 500" in text and "9 → 7" in text
    assert "Possible data loss" in text
    sample = next(d.value for d in clean.dataframe if list(d.value.columns) == ["email", "age"])
    assert sample.index.name == "Original row" and len(sample) == 10
    assert session_of(clean).fingerprint == fp and session_of(clean).shape == (500, 9)
    assert "500 rows × 9 columns" in sidebar_text(clean)


def test_reruns_never_apply_a_preview(clean):
    preview(clean, ["email"])
    for _ in range(3):
        clean.run()
    assert session_of(clean).steps == ()
    clean.button(key="refine_apply").click().run()                     # Opens the dialog only
    assert session_of(clean).steps == ()
    assert clean.button(key="refine_confirm_apply")                    # Dialog is showing
    clean.run()
    assert session_of(clean).steps == ()


def test_confirm_applies_once(clean):
    apply_confirmed(clean, ["email", "age"])
    s = session_of(clean)
    assert s.shape == (500, 7) and len(s.steps) == 1
    assert any("Applied step 1" in m.value for m in clean.success)
    assert "500 rows × 7 columns" in sidebar_text(clean)
    assert "1 cleaning step applied" in sidebar_text(clean)
    assert "email" not in columns_widget(clean).options
    assert columns_widget(clean).value == []                          # Fresh selection after a change
    for _ in range(2):
        clean.run()
    assert len(session_of(clean).steps) == 1


def test_cancel_applies_nothing(clean):
    preview(clean, ["email"])
    clean.button(key="refine_apply").click().run()
    clean.button(key="refine_confirm_cancel").click().run()
    assert session_of(clean).steps == ()


def test_changed_selection_hides_apply(clean):
    preview(clean, ["email"])
    columns_widget(clean).set_value(["email", "age"]).run()
    assert "Selection changed" in page_text(clean)
    assert not any(b.key == "refine_apply" for b in clean.button)


def test_invalid_selection_reports_error(clean):
    every = columns_widget(clean).options
    preview(clean, every)
    assert any("At least one column must remain" in e.value for e in clean.error)
    assert session_of(clean).steps == ()


def test_preview_discarded_after_undo(clean):
    apply_confirmed(clean, ["email"])
    preview(clean, ["age"])
    clean.switch_page(HISTORY).run()
    clean.button(key="refine_undo").click().run()
    clean.switch_page(CLEAN).run()
    assert any("preview was discarded" in w.value for w in clean.warning)
    assert not any(b.key == "refine_apply" for b in clean.button)
    assert session_of(clean).shape == (500, 9)


def test_history_lists_steps_and_audit(clean):
    apply_confirmed(clean, ["email"])
    apply_confirmed(clean, ["age"])
    clean.switch_page(HISTORY).run()
    assert not clean.exception
    steps = next(d.value for d in clean.dataframe if "Step" in d.value.columns)
    assert steps["Operation"].tolist() == ["Drop columns", "Drop columns"]
    assert steps["Columns"].tolist() == ["9 → 8", "8 → 7"]
    audit = next(d.value for d in clean.dataframe if "Event" in d.value.columns)
    assert audit["Event"].tolist() == ["Applied", "Applied"]
    assert clean.button(key="refine_undo").label == "Undo step 2: Drop columns"
    assert '"operation": "drop_columns"' in " ".join(c.value for c in clean.code)


def test_undo_reset_and_verify_from_history(clean):
    apply_confirmed(clean, ["email"])
    apply_confirmed(clean, ["age"])
    clean.switch_page(HISTORY).run()
    clean.button(key="refine_verify_replay").click().run()
    assert any("reproduced the current data exactly" in m.value for m in clean.success)

    clean.button(key="refine_undo").click().run()
    assert session_of(clean).shape == (500, 8)
    assert "500 rows × 8 columns" in sidebar_text(clean)

    clean.button(key="refine_reset").click().run()                    # Opens confirmation only
    assert session_of(clean).shape == (500, 8)
    clean.button(key="refine_cancel_reset").click().run()
    assert session_of(clean).shape == (500, 8)
    clean.button(key="refine_reset").click().run()
    clean.button(key="refine_confirm_reset").click().run()
    s = session_of(clean)
    assert s.shape == (500, 9) and s.steps == () and not s.is_modified
    audit = next(d.value for d in clean.dataframe if "Event" in d.value.columns)
    assert audit["Event"].tolist() == ["Reset", "Undone", "Applied", "Applied"]
    assert "No steps applied" in page_text(clean)


def test_history_empty_state(clean):
    clean.switch_page(HISTORY).run()
    assert "No steps applied" in page_text(clean) and "No events yet" in page_text(clean)
    assert clean.button(key="refine_verify_replay").disabled


def test_profile_reflects_cleaned_data_and_refreshes(clean):
    from ui.state import PROFILE_KEY

    clean.switch_page(PROFILE).run()
    first_key = clean.session_state[PROFILE_KEY][0]
    clean.switch_page(CLEAN).run()
    apply_confirmed(clean, ["email", "age"])
    clean.switch_page(PROFILE).run()
    assert not clean.exception
    table = next(d.value for d in clean.dataframe if "Suggested type" in d.value.columns)
    assert len(table) == 7 and "email" not in set(table["Column"])
    assert "Profiling the cleaned data" in page_text(clean)
    applied_key = clean.session_state[PROFILE_KEY][0]
    assert applied_key != first_key
    cached = clean.session_state[PROFILE_KEY][1]
    clean.run()
    assert clean.session_state[PROFILE_KEY][1] is cached               # Unchanged data: no recompute

    clean.switch_page(HISTORY).run()
    clean.button(key="refine_undo").click().run()
    clean.switch_page(PROFILE).run()
    table = next(d.value for d in clean.dataframe if "Suggested type" in d.value.columns)
    assert len(table) == 9
    assert clean.session_state[PROFILE_KEY][0] == first_key


def test_upload_page_warns_that_reread_discards_steps(clean):
    apply_confirmed(clean, ["email"])
    clean.switch_page("views/upload.py").run()
    assert "Cleaning steps in progress" in page_text(clean)
    assert clean.dataframe[0].value.shape == (100, 9)                 # Upload preview shows data as loaded


def test_loading_new_dataset_starts_new_session(clean):
    apply_confirmed(clean, ["email"])
    preview(clean, ["age"])
    old = session_of(clean)
    load_sample(clean.switch_page("views/upload.py").run())
    clean.switch_page(CLEAN).run()
    new = session_of(clean)
    assert new is not old and new.steps == () and new.shape == (500, 9)
    assert not any(b.key == "refine_apply" for b in clean.button)     # Old preview is gone


def test_closing_dataset_clears_session(clean):
    from ui.state import CLEANING_KEY

    apply_confirmed(clean, ["email"])
    clean.switch_page("views/upload.py").run()
    clean.button(key="refine_close").click().run()
    assert CLEANING_KEY not in clean.session_state
    clean.switch_page(HISTORY).run()
    assert "No dataset loaded" in page_text(clean)
