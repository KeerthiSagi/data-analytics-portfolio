"""Export page: download the current data, its cleaning records and reports. Read-only.

Downloads never change the session. Each file is built when its button is
clicked (see ui/exports.py) and the buttons do not rerun the page, so a
pending preview on the Clean page is left as it was. The notes shown here
come from a read-only check of the current data, cached by its
fingerprint.
"""

from __future__ import annotations

import streamlit as st

from refine.export import FORMULA_PREFIX, ExportCheck
from refine.notes import plural, verb
from refine.session import RECIPE_VERSION
from refine.xlsx_writer import DEFAULT_SHEET_NAME, EXCEL_MAX_CELL_CHARS
from ui.components import callout, no_dataset_state, page_header, section, stat_card_html, stat_row
from ui.exports import Download, dataset_downloads, record_downloads, report_downloads
from ui.state import export_comparison_slot, get_dataset, get_export_check, get_rules, get_session, pending_preview

ESCAPE_KEY = "refine_export_escape_formulas"


def _button(d: Download, icon: str, *, disabled: bool = False) -> None:
    st.download_button(d.label, data=d.build, file_name=d.file_name, mime=d.mime, key=d.key, icon=icon,
                       on_click="ignore", disabled=disabled, width="stretch")
    st.caption(f"File: `{d.file_name}`")


def _names(columns: tuple[str, ...], limit: int = 4) -> str:
    shown = ", ".join(columns[:limit])
    return shown + (f" and {len(columns) - limit} more" if len(columns) > limit else "")


def _excel_notes(check: ExportCheck) -> list[str]:
    x = check.excel
    notes = []
    if x.truncated_text:
        notes.append(f"{plural(x.truncated_text, 'text cell')} longer than Excel's limit of "
                     f"{EXCEL_MAX_CELL_CHARS:,} characters {verb(x.truncated_text, 'is', 'are')} cut to that "
                     "length.")
    if x.control_characters:
        notes.append(f"{plural(x.control_characters, 'text cell')} {verb(x.control_characters, 'has', 'have')} "
                     "control characters Excel cannot store; they are replaced with \ufffd.")
    if x.integers_as_text:
        notes.append(f"{plural(x.integers_as_text, 'whole number')} with 16 or more digits "
                     f"{verb(x.integers_as_text, 'is', 'are')} written as text, because Excel keeps only "
                     "15 digits.")
    if x.decimals_rounded:
        notes.append(f"{plural(x.decimals_rounded, 'decimal value')} {verb(x.decimals_rounded, 'needs', 'need')} "
                     "more than 15 significant digits; Excel rounds them to 15 when it opens the file.")
    if x.infinite_as_text:
        notes.append(f"{plural(x.infinite_as_text, 'infinite value')} {verb(x.infinite_as_text, 'is', 'are')} "
                     "written as the text inf or -inf.")
    if x.dates_as_text:
        notes.append(f"{plural(x.dates_as_text, 'date')} before 1 March 1900 {verb(x.dates_as_text, 'is', 'are')} "
                     "written as text, which Excel's date system cannot hold.")
    if x.times_below_ms:
        notes.append(f"{plural(x.times_below_ms, 'time')} {verb(x.times_below_ms, 'has', 'have')} microseconds; "
                     "Excel shows times to the millisecond.")
    if x.utc_columns:
        notes.append(f"Times in {_names(x.utc_columns)} are written in UTC without a time zone, which Excel "
                     "does not store.")
    if x.text_columns_other:
        n = len(x.text_columns_other)
        notes.append(f"{_names(x.text_columns_other)} {verb(n, 'has', 'have')} a storage type Excel has no type "
                     f"for, so {verb(n, 'it is', 'they are')} written as text.")
    return notes


def _dataset_section(ds, session, check: ExportCheck) -> None:
    with section("Cleaned dataset", key="export-dataset",
                 caption="Every row and column of the current data, in order, with a header row and no index "
                         "column."):
        escape = False
        left, right = st.columns(2)
        with left:
            st.markdown("**CSV**")
            if check.formula_like_cells:
                callout(f"{plural(check.formula_like_cells, 'cell')} could run as spreadsheet formulas",
                        f"Text starting with =, +, -, @ or a tab in {_names(check.formula_like_columns)}. A "
                        "spreadsheet app may run it as a formula when it opens the CSV. Escaping writes these "
                        f"cells with a leading apostrophe ({FORMULA_PREFIX}), so they open as text but no longer "
                        "hold exactly the same value. The Excel download keeps them as text without changes.",
                        "caution")
                escape = st.toggle("Escape formula-like text", value=False, key=ESCAPE_KEY,
                                   help="Adds an apostrophe before each formula-like value in the CSV. "
                                        "The data in REFINE is not changed.")
            csv_download, excel_download = dataset_downloads(ds, session, escape_formulas=escape)
            _button(csv_download, ":material/csv:")
            st.caption("UTF-8, comma-separated. Every value is written as text, so column types are not kept: "
                       "numbers as digits, true/false, dates as 2026-01-15. Missing values and empty text are both "
                       "empty fields."
                       + (f" {plural(check.formula_like_cells, 'cell')} will start with an apostrophe."
                          if escape else ""))
        with right:
            st.markdown("**Excel**")
            if not check.excel.fits:
                callout("Too large for an Excel worksheet", check.excel.limit_error or "", "negative")
            notes = _excel_notes(check)
            if notes:
                callout("Some values are stored differently in Excel", " ".join(notes), "caution")
            _button(excel_download, ":material/table_view:", disabled=not check.excel.fits)
            st.caption(f"One worksheet, \"{DEFAULT_SHEET_NAME}\", with column types kept. Text is always written "
                       "as text, never as a formula.")


def _records_section(ds, session) -> None:
    steps, events = len(session.steps), len(session.audit_log)
    history, recipe = record_downloads(ds, session)
    with section("Cleaning records", key="export-records",
                 caption="JSON files from the session's own records."):
        left, right = st.columns(2)
        with left:
            st.markdown("**History**")
            st.caption(f"{plural(events, 'audit event')} (applied, undone and reset) and "
                       f"{plural(steps, 'active step')}, each with its settings, counts, warnings and time (UTC)."
                       if events else "No steps have been applied, so the history is empty.")
            _button(history, ":material/history:")
        with right:
            st.markdown("**Recipe**")
            st.caption((f"{plural(steps, 'step')} with every frozen value and fingerprint, in the format the "
                        "History page shows." if steps else
                        "No steps: replaying this recipe gives back the original data.")
                       + f" Recipe version {RECIPE_VERSION}. It replays only on the same original table, which "
                         "REFINE checks by fingerprint. REFINE has no page for importing a recipe yet.")
            _button(recipe, ":material/receipt_long:")


def _reports_section(ds, session) -> None:
    quality, comparison = report_downloads(ds, session, get_rules(ds), slot=export_comparison_slot(ds))
    with section("Reports", key="export-reports",
                 caption="JSON, from the same profile, quality checks and comparison as the Profile & Quality and "
                         "Compare pages. Changes are given as increases or decreases; there is no overall score."):
        left, right = st.columns(2)
        with left:
            st.markdown("**Data quality**")
            st.caption("Profile measures and quality findings of the original and current data, and how they "
                       "changed.")
            _button(quality, ":material/query_stats:")
        with right:
            st.markdown("**Comparison**")
            st.caption("Overview, missing values, duplicates, columns and types, findings, cell changes and column "
                       "statistics. Changed cells are summarized with up to 5 examples per column.")
            _button(comparison, ":material/compare_arrows:")


def render() -> None:
    page_header("Export", "Download the current data, its cleaning records and reports. Downloading changes "
                          "nothing.")
    ds = get_dataset()
    if ds is None:
        no_dataset_state()
        return
    session = get_session(ds)
    check = get_export_check(ds)
    rows, cols = session.shape
    stat_row([
        stat_card_html("Rows", f"{rows:,}", f"Original {ds.source.n_rows:,}"),
        stat_card_html("Columns", f"{cols:,}", f"Original {ds.source.n_columns:,}"),
        stat_card_html("Steps applied", f"{len(session.steps):,}", "In the current data"),
        stat_card_html("Data state", session.fingerprint[:12], "Fingerprint of the current data"),
    ])
    preview = pending_preview()
    if preview is not None:
        callout("A previewed step is not applied",
                f"\"{preview.plan.label}\" is waiting for confirmation on the Clean page. The downloads contain "
                "the confirmed data without it.", "neutral")
    if not session.steps:
        callout("No cleaning steps applied", "The downloads contain the data as it was loaded.", "neutral")
    _dataset_section(ds, session, check)
    _records_section(ds, session)
    _reports_section(ds, session)


render()
