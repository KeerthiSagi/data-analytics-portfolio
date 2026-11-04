"""The Profile & Quality page, end to end with Streamlit's AppTest."""

import pytest

from tests.test_app import app, load_sample, page_text, sidebar_text  # noqa: F401  (app is a fixture)

PROFILE = "views/profile.py"


@pytest.fixture
def profiled(app):
    load_sample(app)
    app.switch_page(PROFILE).run()
    assert not app.exception, app.exception
    return app


def test_page_renders_overview_quality_and_charts(profiled):
    text = page_text(profiled)
    for heading in ("Dataset overview", "Quality checks", "Column profile", "Inspect a column",
                    "Load notes and warnings", "Missing and missing-like values"):
        assert heading in text
    assert "Not available yet" not in text
    assert "messy_customers.csv" in sidebar_text(profiled)
    assert len(profiled.get("plotly_chart")) >= 3       # Missing, types, top values


def test_no_overall_score_and_caveat_shown(profiled):
    text = page_text(profiled).lower()
    assert "quality score" not in text.replace("no overall quality score", "")
    assert "does not mean the data is correct" in text


def test_issue_table_has_counts_and_denominators(profiled):
    issues = next(d.value for d in profiled.dataframe if "Severity" in d.value.columns)
    assert {"Affected", "Out of", "Counted out of", "%"} <= set(issues.columns)
    dup = issues[issues["Category"] == "Duplicate rows"].iloc[0]
    assert dup["Affected"] == 12 and dup["Out of"] == 500 and dup["Counted out of"] == "rows"


def test_column_profile_table_lists_every_column(profiled):
    table = next(d.value for d in profiled.dataframe if "Suggested type" in d.value.columns)
    assert len(table) == 9
    assert table.set_index("Column").loc["signup_date", "Suggested type"] == "Date"


def test_inspect_other_column(profiled):
    profiled.selectbox(key="refine_inspect_column").set_value("age").run()
    assert not profiled.exception
    text = page_text(profiled)
    assert "Numeric statistics" in text


def test_sample_rules_are_checked_and_removable(profiled):
    issues = next(d.value for d in profiled.dataframe if "Severity" in d.value.columns)
    assert (issues["Category"] == "Validity rule").sum() >= 1
    remove = [b for b in profiled.button if b.label == "Remove"]
    for _ in range(len(remove)):
        next(b for b in profiled.button if b.label == "Remove").click().run()
    assert "No validity rules are defined" in page_text(profiled)


def test_works_with_uploaded_file_with_empty_column(app):
    from ui.state import DATASET_KEY
    from refine.loader import load_file

    app.session_state[DATASET_KEY] = load_file("up.csv", b"a,b,\n1,NA,\n1,NA,\n2, x ,\n")
    app.switch_page(PROFILE).run()
    assert not app.exception, app.exception
    text = page_text(app)
    assert "column_3" in text           # Empty unnamed column surfaced in issues and notes
    issues = next(d.value for d in app.dataframe if "Severity" in d.value.columns)
    assert "Empty column" in set(issues["Category"])


def test_every_sample_column_can_be_inspected(profiled):
    for name in ["customer_id", "full_name", "email", "signup_date", "country", "age",
                 "annual_spend", "is_active", "segment"]:
        profiled.selectbox(key="refine_inspect_column").set_value(name).run()
        assert not profiled.exception, (name, profiled.exception)


def test_add_range_rule_through_the_form(app):
    from refine.loader import load_file
    from ui.state import DATASET_KEY

    ds = load_file("scores.csv", b"score\n" + b"\n".join(str(v).encode() for v in [5, 50, 150, 7, 9, 11]) + b"\n")
    app.session_state[DATASET_KEY] = ds
    app.switch_page(PROFILE).run()
    k = f"refine_rule_{ds.source.sha256[:12]}"
    app.number_input(key=f"{k}_min").set_value(0)
    app.number_input(key=f"{k}_max").set_value(100)
    app.button(key=f"{k}_add").click().run()
    assert not app.exception
    issues = next(d.value for d in app.dataframe if "Severity" in d.value.columns)
    rule = issues[issues["Category"] == "Validity rule"].iloc[0]
    assert (rule["Affected"], rule["Out of"]) == (1, 6)


def test_profile_cached_between_reruns(profiled):
    from ui.state import PROFILE_KEY

    first = profiled.session_state[PROFILE_KEY][1]
    profiled.run()
    assert profiled.session_state[PROFILE_KEY][1] is first
