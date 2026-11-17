"""End-to-end checks of the Streamlit shell using Streamlit's AppTest.

AppTest cannot drive st.file_uploader, so the upload flow is exercised
through the sample dataset button and ui.state.load_bytes, which the
uploader callback calls with the uploaded file's bytes.
"""

from html import unescape
from pathlib import Path

import pytest

APP = str(Path(__file__).resolve().parent.parent / "app_data_cleaner.py")
PAGES = {
    "views/upload.py": "Upload a dataset",
    "views/profile.py": "Profile & Quality",
    "views/clean.py": "Clean",
    "views/compare.py": "Compare",
    "views/history.py": "History",
    "views/export.py": "Export",
}


@pytest.fixture
def app():
    from streamlit import config
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP, default_timeout=60)
    # The app's config shows browsers only the exception type; tests need the message to explain a failure.
    config.set_option("client.showErrorDetails", "full")
    return at.run()


def page_title(at) -> str:
    for m in at.markdown:
        if 'class="refine-title"' in m.value:
            return unescape(m.value.split('class="refine-title">')[1].split("<")[0])
    return ""


def sidebar_text(at) -> str:
    return " ".join(m.value for m in at.sidebar.markdown)


def page_text(at) -> str:
    return " ".join(m.value for m in at.markdown)


def load_sample(at):
    return at.button(key="refine_load_sample").click().run()


def test_default_page_renders_without_errors(app):
    assert not app.exception and not app.error
    assert page_title(app) == "Upload a dataset"
    assert "No dataset loaded" in sidebar_text(app)
    assert any(e.label == "How REFINE handles your data" for e in app.expander)


def test_theme_css_is_injected(app):
    assert any("--refine-bg: #080F20" in m.value for m in app.markdown)


@pytest.mark.parametrize("path, title", list(PAGES.items()))
def test_every_page_renders_empty_state(app, path, title):
    app.switch_page(path).run()
    assert not app.exception and not app.error
    assert page_title(app) == title
    if path != "views/upload.py":
        assert "No dataset loaded" in page_text(app)


def test_sample_loads_and_updates_sidebar_on_same_run(app):
    load_sample(app)
    assert not app.exception and not app.error
    assert "messy_customers.csv" in sidebar_text(app)
    assert "500 rows × 9 columns" in sidebar_text(app)
    assert app.dataframe[0].value.shape == (100, 9)
    assert "No parsing adjustments" in page_text(app)
    assert "What is preserved" in page_text(app)


# Every page is built: History in Phase 3, Clean by Phase 6, Compare in Phase 7 and Export in Phase 8.
@pytest.mark.parametrize("path", list(PAGES))
def test_no_page_shows_placeholder_once_loaded(app, path):
    load_sample(app)
    app.switch_page(path).run()
    assert not app.exception
    assert "Not available yet" not in page_text(app)
    assert "messy_customers.csv" in sidebar_text(app)      # Dataset persists across pages


def test_reparse_with_other_delimiter_then_back(app):
    load_sample(app)
    delimiter = next(s for s in app.selectbox if s.label == "Delimiter")
    delimiter.set_value(";")
    next(b for b in app.button if b.label == "Re-read file").click().run()
    assert "500 rows × 1 column" in sidebar_text(app)
    assert "does not split any line" in page_text(app)

    delimiter = next(s for s in app.selectbox if s.label == "Delimiter")
    delimiter.set_value("Automatic")
    next(b for b in app.button if b.label == "Re-read file").click().run()
    assert "500 rows × 9 columns" in sidebar_text(app)


def test_close_dataset(app):
    load_sample(app)
    app.button(key="refine_close").click().run()
    assert "No dataset loaded" in sidebar_text(app)
    assert not app.dataframe


def test_failed_load_keeps_previous_dataset(app):
    """A file that fails to load shows the error and leaves the open dataset alone."""
    from refine.errors import LoadError
    from refine.loader import load_file
    from ui.state import ERROR_KEY, LoadFailure

    load_sample(app)
    # Record the same failure ui.state.load_bytes stores when the uploader
    # callback receives a broken file.
    with pytest.raises(LoadError) as err:
        load_file("broken.csv", b'a,b\n1,"never closed\n')
    app.session_state[ERROR_KEY] = LoadFailure("broken.csv", err.value.code, err.value.message)
    app.run()
    assert any("broken.csv could not be loaded" in e.value for e in app.error)
    assert any("previously loaded dataset is still open" in e.value for e in app.error)
    assert "messy_customers.csv" in sidebar_text(app)
