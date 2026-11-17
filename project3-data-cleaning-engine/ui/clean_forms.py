"""Settings forms for the Clean page's Columns, Missing values and Duplicates tabs.

Each form draws its widgets, then a Preview button whose callback reads
the settings from the widgets at click time (a `settings` function) and
asks the session for a preview. Under the form, a pending preview for the
same operation is shown with the shared Apply flow (ui/clean_preview.py).

Widget keys include the session's state version, so after apply, undo or
reset every form starts fresh and never offers a column that no longer
exists.

Value widgets use persist_state="session", so their settings survive a
visit to another page. Without it Streamlit clears them on a page switch,
and a pending preview could no longer be applied: the form would differ
from the preview's settings, as if the user had changed them.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import streamlit as st

from refine.duplicates import DuplicateReport
from refine.missing import (
    EMPTY_TEXT,
    PRESET_TOKENS,
    WHITESPACE,
    parse_plain_numbers,
    scan_markers,
    suggest_markers,
)
from refine.notes import plural
from refine.operations import Preview, get_operation
from refine.operations.missing_ops import INDICATOR_FALSE, INDICATOR_TRUE, indicator_name
from ui.clean_preview import render_preview
from ui.components import callout, section, stat_card_html, stat_row
from ui.state import (
    discard_preview,
    find_duplicates_now,
    get_duplicate_report,
    get_missing_summary,
    request_preview,
)

MISSING_DEFINITION = (
    "REFINE treats a cell as missing when it has no value or holds empty text. Text such as NA, null or "
    "a space is not missing until you choose it in Standardize missing values."
)
NUMERIC_NOTE = (
    "In text columns, the mean and median are calculated from the values that read as plain numbers, such "
    "as 42 or -3.5. Values such as $1,200, 1,200, 12% or 'unknown' are not plain numbers. The column is not "
    "converted: only the missing cells receive the calculated value, written as text. Columns converted to "
    "numbers on the Data types tab are filled with a number and keep their type."
)
MISSING_OPERATIONS = ("standardize_missing", "drop_missing_rows", "drop_missing_columns", "fill_missing",
                      "add_missing_indicator")


def wkey(session, name: str) -> str:
    return f"refine_{name}_{session.session_id[:8]}_{session.version}"


def _get(key: str, default=None):
    value = st.session_state.get(key, default)
    return default if value is None else value


def _preview_button(op_id: str, settings: Callable[[], dict], *, key: str, disabled: bool,
                    label: str = "Preview") -> None:
    st.button(label, icon=":material/visibility:", key=key, on_click=request_preview,
              args=(op_id, settings), disabled=disabled)


def _show_preview_for(op_id: str, preview: Preview | None, settings: Callable[[], dict]) -> None:
    if preview is not None and preview.plan.operation == op_id:
        render_preview(preview, settings())


def _small_table(frame: pd.DataFrame, rows: int = 8) -> None:
    st.dataframe(frame, hide_index=True, width="stretch", height=min(38 + 35 * rows, 38 + 35 * max(len(frame), 1)))


# ------------------------------------------------------------------
# Columns tab (Drop columns, from Phase 3)
# ------------------------------------------------------------------

def columns_tab(session, preview: Preview | None) -> None:
    op = get_operation("drop_columns")
    with section("Choose a step", key="clean-choose"):
        st.selectbox("Operation", [op.op_id], key="refine_clean_operation", format_func=lambda _: op.label,
                     persist_state="session")
        st.caption(op.description)
        key = f"refine_drop_columns_{session.session_id[:8]}_{session.version}"
        selection = st.multiselect(
            "Columns to drop", list(session.columns), key=key,
            placeholder="Choose one or more columns",
            help="Only the columns you choose are removed. At least one column must remain.", persist_state="session",
        )

        def settings() -> dict:
            return {"columns": list(st.session_state.get(key) or [])}

        _preview_button("drop_columns", settings, key="refine_preview", disabled=not selection)
        if not selection:
            st.caption("Choose at least one column to preview.")
    _show_preview_for("drop_columns", preview, settings)


# ------------------------------------------------------------------
# Missing values tab
# ------------------------------------------------------------------

def _standardize_form(session, data: pd.DataFrame, preview) -> None:
    k_cols, k_empty, k_ws = wkey(session, "sm_columns"), wkey(session, "sm_empty"), wkey(session, "sm_ws")
    k_tokens, k_custom, k_case = wkey(session, "sm_tokens"), wkey(session, "sm_custom"), wkey(session, "sm_case")
    text_cols = [c for c in session.columns if isinstance(data[c].dtype, pd.StringDtype)]
    columns = st.multiselect("Columns to check", text_cols, key=k_cols, placeholder="Choose one or more columns",
                             persist_state="session")

    found = suggest_markers(data, columns) if columns else ()
    if columns:
        if found:
            st.markdown("**Found in these columns** · suggestions only, nothing is selected for you")
            _small_table(pd.DataFrame([{
                "Column": m.column, "Text found": repr(m.value) if m.value.strip() == "" else m.value,
                "Kind": m.marker if m.marker in (EMPTY_TEXT, WHITESPACE) else "Null-like text", "Cells": m.count,
            } for m in found]))
        else:
            callout("No common markers found", "These columns hold no empty text, whitespace-only text or "
                    "null-like text such as NA. You can still enter your own markers below.", "neutral")

    a, b = st.columns(2)
    with a:
        st.checkbox("Empty text (\"\")", key=k_empty,
                    help="Already counted as missing; this stores it as no value.", persist_state="session")
    with b:
        st.checkbox("Whitespace-only text", key=k_ws, help="Cells holding only spaces, tabs or line breaks.",
                    persist_state="session")
    options = list(PRESET_TOKENS)
    for m in found:
        t = m.value.strip()
        if t and t not in options:
            options.append(t)
    st.multiselect("Markers to treat as missing", options, key=k_tokens, placeholder="Choose markers",
                   help="Compared after removing surrounding spaces. Includes common markers and the null-like "
                        "text found above.", persist_state="session")
    st.text_area("Other markers, one per line", key=k_custom, height=80,
                 placeholder="For example: unknown\n#N/A",
                 help="Any other text that means 'no value' in these columns.", persist_state="session")
    st.checkbox("Match case exactly", key=k_case, value=True,
                help="When on, 'NA' does not match 'na'. When off, any capitalization matches.",
                persist_state="session")

    def settings() -> dict:
        custom = [line.strip() for line in str(_get(k_custom, "")).splitlines() if line.strip()]
        tokens = list(_get(k_tokens, []))
        tokens += [t for t in custom if t not in tokens]
        return {"columns": list(_get(k_cols, [])), "tokens": tokens,
                "case_sensitive": bool(st.session_state.get(k_case, True)),
                "empty_text": bool(_get(k_empty, False)), "whitespace_only": bool(_get(k_ws, False))}

    s = settings()
    ready = bool(s["columns"]) and bool(s["tokens"] or s["empty_text"] or s["whitespace_only"])
    if ready:
        matches = scan_markers(data, s["columns"], s["tokens"], case_sensitive=s["case_sensitive"],
                               empty_text=s["empty_text"], whitespace_only=s["whitespace_only"])
        total = sum(m.count for m in matches)
        st.markdown(f"**Would become missing: {plural(total, 'cell')}**")
        if matches:
            _small_table(pd.DataFrame([{"Column": m.column, "Marker": m.marker,
                                        "Text found": repr(m.value) if m.value.strip() == "" else m.value,
                                        "Cells": m.count} for m in matches]))
        else:
            st.caption("No cell matches these markers with the current settings.")
    elif s["columns"]:
        st.caption("Choose at least one marker to see how many cells match.")
    _preview_button("standardize_missing", settings, key="refine_preview_standardize_missing", disabled=not ready)
    _show_preview_for("standardize_missing", preview, settings)


HOW_LABELS = {"any": "Any selected column is missing", "all": "All selected columns are missing",
              "min_present": "Fewer than a minimum number of selected columns have a value"}


def _drop_rows_form(session, preview) -> None:
    k_cols, k_how, k_min = wkey(session, "dr_columns"), wkey(session, "dr_how"), wkey(session, "dr_min")
    columns = st.multiselect("Columns to check for missing values", list(session.columns), key=k_cols,
                             placeholder="Choose one or more columns", persist_state="session")
    how = st.radio("Remove a row when", list(HOW_LABELS), key=k_how, format_func=HOW_LABELS.get,
                   persist_state="session")
    if how == "min_present":
        st.number_input("Minimum number of selected columns that must have a value", min_value=1,
                        value=1, step=1, key=k_min, disabled=not columns,
                        help="A row is removed when fewer of the selected columns than this have a value.",
                        persist_state="session")

    def settings() -> dict:
        h = _get(k_how, "any")
        out = {"columns": list(_get(k_cols, [])), "how": h}
        if h == "min_present":
            out["min_present"] = int(_get(k_min, 1))
        return out

    _preview_button("drop_missing_rows", settings, key="refine_preview_drop_missing_rows", disabled=not columns)
    if not columns:
        st.caption("Choose at least one column to preview.")
    _show_preview_for("drop_missing_rows", preview, settings)


def _drop_columns_form(ds, session, preview) -> None:
    k_t = wkey(session, "dc_threshold")
    threshold = st.slider("Drop columns with at least this share of missing values (%)", 1, 100, 50, 1, key=k_t,
                          persist_state="session")
    summary = get_missing_summary(ds)
    table = pd.DataFrame([{"Column": c.name, "Missing cells": c.missing, "Missing %": round(c.pct, 1),
                           "Would be dropped": "Yes" if c.rows and c.pct >= threshold else "No"}
                          for c in summary])
    qualifying = int((table["Would be dropped"] == "Yes").sum()) if len(table) else 0
    st.markdown(f"**{plural(qualifying, 'column')} at or above {threshold}%**")
    _small_table(table, rows=10)

    def settings() -> dict:
        return {"threshold": float(_get(k_t, 50))}

    _preview_button("drop_missing_columns", settings, key="refine_preview_drop_missing_columns",
                    disabled=not qualifying)
    if not qualifying:
        st.caption("No column qualifies at this threshold.")
    _show_preview_for("drop_missing_columns", preview, settings)


FILL_LABELS = {"constant": "A fixed value", "mean": "Mean (numbers only)", "median": "Median (numbers only)",
               "mode": "Most frequent value", "ffill": "Previous value (forward fill)",
               "bfill": "Next value (backward fill)"}
ROUNDING = [None, 0, 1, 2, 3, 4, 6]


def _fill_form(ds, session, data: pd.DataFrame, preview) -> None:
    k_cols, k_method = wkey(session, "fm_columns"), wkey(session, "fm_method")
    k_value, k_dec, k_excl = wkey(session, "fm_value"), wkey(session, "fm_decimals"), wkey(session, "fm_exclude")
    columns = st.multiselect("Columns to fill", list(session.columns), key=k_cols,
                             placeholder="Choose one or more columns", persist_state="session")
    method = st.selectbox("Fill with", list(FILL_LABELS), key=k_method, format_func=FILL_LABELS.get,
                          persist_state="session")
    if method == "constant":
        st.text_input("Fill value", key=k_value, placeholder="For example: Unknown or 0",
                      help="Written exactly as typed into every missing cell of the selected columns.",
                      persist_state="session")
    elif method in ("mean", "median"):
        callout("Numbers are read from text", NUMERIC_NOTE, "neutral")
        st.selectbox("Round to", ROUNDING, index=3, key=k_dec,
                     format_func=lambda d: "Full precision" if d is None else plural(d, "decimal place"),
                     persist_state="session")
        st.checkbox("Calculate from plain numbers only, leaving other values as they are", key=k_excl,
                    help="Without this, the step is refused when a column holds values that are not plain "
                         "numbers, so nothing is calculated from part of a column by accident.",
                         persist_state="session")
    elif method in ("ffill", "bfill"):
        callout("Depends on row order",
                "Each missing cell receives the value from the nearest row "
                + ("above" if method == "ffill" else "below")
                + " that has one, in the current row order. Missing cells with no value "
                + ("above" if method == "ffill" else "below") + " stay missing.", "caution")

    if columns:
        summary = {c.name: c for c in get_missing_summary(ds)}
        rows = []
        for c in columns:
            row = {"Column": c, "Missing cells": summary[c].missing, "Missing %": round(summary[c].pct, 1)}
            if method in ("mean", "median"):
                parsed = parse_plain_numbers(data[c])
                row["Plain numbers"] = parsed.plain
                row["Other values"] = parsed.other
                row["Examples of other values"] = ", ".join(parsed.other_examples[:3])
            rows.append(row)
        _small_table(pd.DataFrame(rows))

    def settings() -> dict:
        m = _get(k_method, "constant")
        out = {"columns": list(_get(k_cols, [])), "method": m}
        if m == "constant":
            out["value"] = str(_get(k_value, ""))
        elif m in ("mean", "median"):
            out["decimals"] = st.session_state.get(k_dec, 2)
            out["non_numeric"] = "exclude" if _get(k_excl, False) else "reject"
        return out

    s = settings()
    ready = bool(columns) and (method != "constant" or bool(s.get("value")))
    _preview_button("fill_missing", settings, key="refine_preview_fill_missing", disabled=not ready)
    if not columns:
        st.caption("Choose at least one column to preview.")
    elif not ready:
        st.caption("Enter a fill value to preview.")
    _show_preview_for("fill_missing", preview, settings)


def _indicator_form(session, preview) -> None:
    k_col, k_name = wkey(session, "mi_column"), wkey(session, "mi_name")
    column = st.selectbox("Column", list(session.columns), key=k_col, index=None, placeholder="Choose a column",
                          persist_state="session")
    default = indicator_name(column, session.columns) if column else "<column>_missing"
    st.text_input("New column name (optional)", key=k_name, placeholder=default,
                  help=f"Leave empty to use {default}. A number is added if that name is taken.",
                  persist_state="session")
    st.caption(f"The new column is placed right after the chosen one and holds '{INDICATOR_TRUE}' where the "
               f"value is missing and '{INDICATOR_FALSE}' elsewhere. Add it before filling, so it records "
               "which values were originally missing.")

    def settings() -> dict:
        name = str(_get(k_name, "")).strip()
        return {"column": _get(k_col), "name": name or None}

    _preview_button("add_missing_indicator", settings, key="refine_preview_add_missing_indicator",
                    disabled=column is None)
    _show_preview_for("add_missing_indicator", preview, settings)


def missing_tab(ds, session, preview: Preview | None) -> None:
    labels = {op: get_operation(op).label for op in MISSING_OPERATIONS}
    with section("Missing values", key="clean-missing"):
        op_id = st.selectbox("Missing-value operation", list(labels), key="refine_missing_operation",
                             format_func=labels.get, on_change=discard_preview, persist_state="session")
        st.caption(get_operation(op_id).description)
        callout("What counts as missing", MISSING_DEFINITION, "neutral")
        data = session.data
        if op_id == "standardize_missing":
            _standardize_form(session, data, preview)
        elif op_id == "drop_missing_rows":
            _drop_rows_form(session, preview)
        elif op_id == "drop_missing_columns":
            _drop_columns_form(ds, session, preview)
        elif op_id == "fill_missing":
            _fill_form(ds, session, data, preview)
        else:
            _indicator_form(session, preview)


# ------------------------------------------------------------------
# Duplicates tab
# ------------------------------------------------------------------

MATCH_LABELS = {"all": "All columns (rows identical in every column)", "key": "Key columns I choose"}
DUPLICATE_TERMS = (
    "A duplicate group is a set of rows that match each other. Duplicate rows are the extra copies: every "
    "row in a group except the one kept. Removing duplicates removes exactly the duplicate rows. A cell "
    "with no value matches another cell with no value, but not empty text."
)


def _report_section(report: DuplicateReport, session, data: pd.DataFrame) -> None:
    stat_row([
        stat_card_html("Duplicate groups", f"{report.groups:,}", "Sets of 2 or more matching rows"),
        stat_card_html("Duplicate rows", f"{report.duplicate_rows:,}",
                       f"Extra copies: {report.duplicate_pct:.1f}% of {report.rows:,} rows"),
        stat_card_html("Rows in groups", f"{report.rows_in_groups:,}",
                       f"{report.rows_in_groups_pct:.1f}%, including the rows kept"),
        stat_card_html("Rows after removal", f"{report.rows_after:,}", f"Of {report.rows:,}"),
    ])
    if not report.groups:
        callout("No duplicates found", "No two rows match on these columns with these settings.", "neutral")
        return
    ids = session.row_ids
    frames = []
    for group in report.examples:
        pos = np.array(group.positions, dtype=np.int64)
        part = data.iloc[pos][list(report.columns)].copy()
        part.insert(0, "Duplicate group", group.number)
        part.index = pd.Index(ids[pos] + 1, name="Original row")
        frames.append(part)
    st.caption(f"Examples: the first {plural(len(report.examples), 'duplicate group')}, showing the matched "
               "columns with their stored values. 'Original row' is the row's position in the uploaded table.")
    st.dataframe(pd.concat(frames), width="stretch", height=min(400, 38 + 35 * sum(len(f) for f in frames)))


def duplicates_tab(session, preview: Preview | None) -> None:
    k_match, k_cols = wkey(session, "dup_match"), wkey(session, "dup_columns")
    k_case, k_ws, k_keep = wkey(session, "dup_case"), wkey(session, "dup_ws"), wkey(session, "dup_keep")
    op = get_operation("remove_duplicates")
    with section("Duplicates", key="clean-duplicates"):
        st.caption(op.description)
        callout("Groups and rows", DUPLICATE_TERMS, "neutral")
        match = st.radio("Match rows on", list(MATCH_LABELS), key=k_match, format_func=MATCH_LABELS.get,
                         persist_state="session")
        if match == "key":
            st.multiselect("Key columns", list(session.columns), key=k_cols,
                           placeholder="For example: customer_id",
                           help="Rows with equal values in all of these columns match, even if other columns differ.",
                           persist_state="session")
        a, b = st.columns(2)
        with a:
            st.checkbox("Ignore case", key=k_case, help="'ANN@X.COM' matches 'ann@x.com'.", persist_state="session")
        with b:
            st.checkbox("Ignore extra spaces", key=k_ws,
                        help="Spaces at the start or end are ignored and runs of spaces count as one.",
                        persist_state="session")
        st.caption("These options change how rows are compared only. Stored values are never changed by matching.")

        def match_settings() -> dict:
            key_cols = list(_get(k_cols, [])) if _get(k_match, "all") == "key" else None
            return {"columns": key_cols, "ignore_case": bool(_get(k_case, False)),
                    "normalize_whitespace": bool(_get(k_ws, False))}

        s = match_settings()
        ready = s["columns"] is None or bool(s["columns"])
        st.button("Find duplicates", icon=":material/search:", key="refine_find_duplicates",
                  on_click=find_duplicates_now, args=(match_settings,), disabled=not ready)
        if not ready:
            st.caption("Choose at least one key column.")
        report = get_duplicate_report(s) if ready else None
        if report is not None:
            _report_section(report, session, session.data)

        st.markdown("**Remove duplicates**")
        st.radio("In each group, keep", ["first", "last"], key=k_keep, horizontal=True,
                 format_func=lambda k: f"The {k} row (in current row order)", persist_state="session")

        def settings() -> dict:
            return {**match_settings(), "keep": _get(k_keep, "first")}

        _preview_button("remove_duplicates", settings, key="refine_preview_remove_duplicates", disabled=not ready,
                        label="Preview removal")
        st.caption("Duplicates are never removed automatically: preview the removal, then confirm it.")
    _show_preview_for("remove_duplicates", preview, settings)


def placeholder_tab(what: str) -> None:
    callout("Not available yet", what, "neutral")
