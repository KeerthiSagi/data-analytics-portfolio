"""The Compare page, end to end with Streamlit's AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text  # noqa: F401  (app is a fixture)
from tests.test_clean_pages import session_of
from tests.test_clean_phase5_pages import frames_with, run

COMPARE = "views/compare.py"
SECTIONS = ("Dataset overview", "Missing values", "Duplicates", "Columns and data types", "Data quality",
            "Value changes", "Column details", "Cleaning history")


@pytest.fixture
def compare(app):
    load_sample(app)
    app.switch_page(COMPARE).run()
    assert not app.exception, app.exception
    return app


def apply(at, op, params):
    s = session_of(at)
    s.apply(s.preview(op, params), confirmed=True)


def cleaned(at):
    apply(at, "remove_duplicates", {})
    apply(at, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    apply(at, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce"})
    apply(at, "drop_columns", {"columns": ["segment"]})
    return run(at)


def frame(at, column):
    frames = frames_with(at, column)
    assert frames, f"No table with column {column!r}"
    return frames[0]


def session_state(at):
    s = session_of(at)
    return (s.version, s.fingerprint, len(s.steps), len(s.audit_log), s.recipe_json())


def test_page_loads_with_every_section(compare):
    text = page_text(compare)
    for heading in SECTIONS:
        assert heading in text
    assert "Not available yet" not in text


def test_no_steps_shows_empty_states(compare):
    text = page_text(compare)
    assert "No cleaning steps applied" in text
    for message in ("No change in missing values", "No column changes", "No cell changed",
                    "No change in quality findings"):
        assert message in text
    overview = frame(compare, "Metric")
    assert set(overview["Change"]) == {"No change"}


def test_overview_metrics_render(compare):
    cleaned(compare)
    overview = frame(compare, "Metric").set_index("Metric")
    assert overview.loc["Rows", "Original"] == "500" and overview.loc["Rows", "Current"] == "488"
    assert overview.loc["Rows", "Change"] == "−12 (decrease)"
    assert overview.loc["Columns", "Current"] == "8"
    assert overview.loc["Duplicate rows", "Original"] == "12" and overview.loc["Duplicate rows", "Current"] == "0"
    assert overview.loc["Numeric columns", "Change"] == "+1 (increase)"
    text = page_text(compare)
    assert "500 → 488" in text
    assert "improve" not in text.lower() and "better" not in text.lower()


def test_missing_values_comparison_renders(compare):
    cleaned(compare)
    missing = frame(compare, "Original missing").set_index("Column")
    assert "age" in missing.index and missing.loc["age", "Status"] == "Retained"
    assert missing.loc["age", "Original missing"] == "2 (0.4%)"
    assert missing.loc["segment", "Status"] == "Removed" and missing.loc["segment", "Current missing"] == "Not in table"
    compare.toggle(key="refine_cmp_missing_changed").set_value(False)
    run(compare)
    assert len(frame(compare, "Original missing")) == 9          # 8 kept + 1 removed


def test_dtype_and_column_changes_render(compare):
    cleaned(compare)
    cols = frame(compare, "Original type").set_index("Column")
    assert cols.loc["age", "Change"] == "Type changed"
    assert cols.loc["age", "Current type"] == "Whole number (Int64)"
    assert cols.loc["segment", "Change"] == "Removed"


def test_value_changes_and_quality_render(compare):
    cleaned(compare)
    cells = frame(compare, "Made missing").set_index("Column")
    assert "customer_id" not in cells.index                        # Unchanged columns hidden by default
    assert cells.loc["age", "Compared as"] == "Text (type changed)"
    quality = frame(compare, "Category")
    assert "No longer detected" in set(quality["Status"])
    text = page_text(compare)
    assert "Rows removed" in text and "Not counted as cell changes" in text


def test_column_details_render_for_every_column(compare):
    cleaned(compare)
    from ui.state import COMPARISON_KEY

    assert "segment (removed)" in compare.selectbox(key="refine_cmp_column").options
    for name in [d.name for d in compare.session_state[COMPARISON_KEY][1].details]:
        compare.selectbox(key="refine_cmp_column").set_value(name)
        run(compare)
    compare.selectbox(key="refine_cmp_column").set_value("age")
    run(compare)
    stats = frame(compare, "Statistic").set_index("Statistic")
    assert {"Minimum", "Maximum", "Mean", "Median", "Standard deviation"} <= set(stats.index)
    assert "Examples of changed cells" in page_text(compare)
    compare.selectbox(key="refine_cmp_column").set_value("segment")
    run(compare)
    assert any("This column was removed" in c.value for c in compare.caption)


def test_history_renders(compare):
    cleaned(compare)
    history = frame(compare, "Operation")
    assert history["Step"].tolist() == [1, 2, 3, 4]
    assert history["Operation"].iloc[0] == "Remove duplicate rows"
    assert "segment" in history["Columns"].iloc[3]
    assert all("fingerprint" not in str(v).lower() for v in history.to_numpy().ravel())


def test_page_triggers_no_cleaning_and_has_no_buttons(compare):
    cleaned(compare)
    before = session_state(compare)
    assert len(compare.button) == 0
    for key in ("refine_cmp_missing_changed", "refine_cmp_quality_all", "refine_cmp_cells_changed"):
        compare.toggle(key=key).set_value(not compare.toggle(key=key).value)
        run(compare)
    compare.selectbox(key="refine_cmp_column").set_value("email")
    run(compare)
    assert session_state(compare) == before


def test_comparison_follows_undo_and_reset(compare):
    from ui.state import COMPARISON_KEY

    cleaned(compare)
    first = compare.session_state[COMPARISON_KEY][1]
    run(compare)
    assert compare.session_state[COMPARISON_KEY][1] is first          # Reused between reruns
    session_of(compare).undo()                                         # Restores 'segment'
    run(compare)
    assert compare.session_state[COMPARISON_KEY][1] is not first
    assert "Removed" not in set(frame(compare, "Original type")["Change"])
    session_of(compare).reset()
    run(compare)
    assert "No cleaning steps applied" in page_text(compare)
    assert set(frame(compare, "Metric")["Change"]) == {"No change"}


def test_original_profile_is_reused_after_a_new_step(compare):
    from ui.state import ORIGINAL_REPORTS_KEY

    apply(compare, "remove_duplicates", {})
    run(compare)
    original = compare.session_state[ORIGINAL_REPORTS_KEY][1]
    apply(compare, "drop_columns", {"columns": ["segment"]})
    run(compare)
    assert compare.session_state[ORIGINAL_REPORTS_KEY][1] is original


def test_profile_page_and_compare_agree(compare):
    cleaned(compare)
    from ui.state import COMPARISON_KEY, PROFILE_KEY

    comparison = compare.session_state[COMPARISON_KEY][1]
    compare.switch_page("views/profile.py").run()
    assert not compare.exception
    profile = compare.session_state[PROFILE_KEY][1]
    assert comparison.current_profile is profile
    assert comparison.metric("missing_cells").current == profile.missing_cells
    assert comparison.duplicates.current_duplicates == profile.duplicate_rows


def test_new_dataset_drops_the_comparison(compare):
    from refine.loader import load_file
    from ui.state import COMPARISON_KEY, load_bytes  # noqa: F401

    assert COMPARISON_KEY in compare.session_state
    compare.session_state["refine_dataset"] = load_file("small.csv", b"a,b\n1,2\n3,4\n")
    run(compare)
    overview = frame(compare, "Metric").set_index("Metric")
    assert overview.loc["Rows", "Original"] == "2" and overview.loc["Columns", "Current"] == "2"


def test_small_upload_with_empty_column(app):
    from refine.loader import load_file
    from ui.state import DATASET_KEY

    app.session_state[DATASET_KEY] = load_file("up.csv", b"a,b,\n1,NA,\n1,NA,\n2, x ,\n")
    app.switch_page(COMPARE).run()
    assert not app.exception, app.exception
    s = session_of(app)
    s.apply(s.preview("remove_duplicates", {}), confirmed=True)
    run(app)
    assert frame(app, "Metric").set_index("Metric").loc["Rows", "Current"] == "2"
