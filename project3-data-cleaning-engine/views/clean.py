"""Clean page: choose a step, preview its exact effect, then confirm it.

The page never changes a table. "Preview" asks the cleaning session for a
plan; "Apply" only opens a confirmation dialog; the step is applied by the
dialog's confirm button callback, bound to that preview's token. A rerun
can therefore never apply anything, and a preview that no longer matches
the data is discarded instead of applied.

Steps are grouped in tabs. Columns, Missing values, Duplicates, Data
types and Text work; Outliers describes what a later phase adds. Every tab uses the same
preview panel and confirmation dialog (ui/clean_preview.py).
"""

from __future__ import annotations

import streamlit as st

from ui.clean_forms import columns_tab, duplicates_tab, missing_tab, placeholder_tab
from ui.clean_text import text_tab
from ui.clean_types import types_tab
from ui.components import no_dataset_state, page_header, show_flash, stat_card_html, stat_row
from ui.state import cached_profile, get_dataset, get_preview, get_session

HISTORY_PAGE = "views/history.py"

TABS = ("Columns", "Missing values", "Duplicates", "Data types", "Text", "Outliers")
PLANNED = {
    "Outliers": "Finding and treating outliers arrives in a later phase. It will use this same preview and "
                "confirmation flow.",
}


def render() -> None:
    page_header(
        "Clean",
        "Choose cleaning steps, preview exactly what each one would change, and confirm before it is applied.",
    )
    ds = get_dataset()
    if ds is None:
        no_dataset_state()
        return
    session = get_session(ds)
    preview = get_preview()          # Before show_flash, so a stale-preview notice shows now
    show_flash()

    rows, cols = session.shape
    stat_row([
        stat_card_html("Rows", f"{rows:,}", f"Original {ds.source.n_rows:,}"),
        stat_card_html("Columns", f"{cols:,}", f"Original {ds.source.n_columns:,}"),
        stat_card_html("Steps applied", f"{len(session.steps):,}", "Undo or reset on History"),
    ])

    tabs = dict(zip(TABS, st.tabs(list(TABS), key="refine_clean_tab")))
    with tabs["Columns"]:
        columns_tab(session, preview)
    with tabs["Missing values"]:
        missing_tab(ds, session, preview)
    with tabs["Duplicates"]:
        duplicates_tab(session, preview)
    with tabs["Data types"]:
        types_tab(ds, session, preview, cached_profile(ds))
    with tabs["Text"]:
        text_tab(session, preview)
    for name, text in PLANNED.items():
        with tabs[name]:
            placeholder_tab(text)

    if session.steps:
        st.page_link(HISTORY_PAGE, label="See history, undo or reset", icon=":material/history:")


render()
