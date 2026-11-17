"""The Clean page's Outliers tab: detect outliers in one numeric column, then treat them.

The user picks a column, a method and its parameters. A read-only
detection summary (boundaries, counts) is shown before any preview. The
treatment itself uses the shared Preview -> Confirm flow, so the plan
freezes the boundaries the user saw and nothing changes before confirmation.
"""

from __future__ import annotations

import streamlit as st

from refine.errors import CleaningError
from refine.notes import plural
from refine.operations import Preview, canonical_json, get_operation, jsonable
from refine.operations.outlier_ops import TREATMENT_LABELS, TREATMENTS
from refine.outliers import (
    DEFAULTS,
    METHOD_KEYS,
    METHOD_LABELS,
    METHODS,
    detect_outliers,
    eligible_columns,
    fmt,
)
from ui.clean_forms import _get, _preview_button, _show_preview_for, wkey
from ui.components import callout, section, stat_card_html, stat_row
from ui.state import OUTLIER_ANALYSIS_KEY

METHOD_HELP = {
    "iqr": "Outside Q1 − k × IQR and Q3 + k × IQR, where IQR = Q3 − Q1. Quartiles use linear interpolation.",
    "zscore": "Outside mean ± threshold × standard deviation (population, ddof = 0), that is |z| > threshold.",
    "percentile": "Below the lower or above the upper percentile value (linear interpolation).",
}
TREATMENT_HELP = {
    "cap": "Values below the lower boundary become the lower boundary; values above the upper become the upper.",
    "set_missing": "Outliers become missing values. Existing missing values are unchanged.",
    "remove_rows": "Rows holding an outlier in this column are removed. Rows where it is missing are kept.",
    "replace_value": "Outliers become the number you enter.",
}
NO_COLUMNS = ("Outlier detection works on whole-number and decimal columns, and this table has none. Every column "
              "REFINE loads is text: convert the columns that hold numbers on the Data types tab first. True / "
              "false columns are not checked.")
RULE_NOTE = ("A value is an outlier only when it is strictly below the lower or above the upper boundary; a value "
             "equal to a boundary is not. Missing values are never outliers.")


def outlier_settings(session) -> dict:
    """The settings currently in the widgets, as the operation expects them."""
    method = _get(wkey(session, "ol_method"), "iqr")
    out: dict = {"column": st.session_state.get(wkey(session, "ol_column")), "method": method}
    for k in METHOD_KEYS.get(method, ()):
        out[k] = _get(wkey(session, f"ol_{k}"), DEFAULTS[k])
    out["treatment"] = _get(wkey(session, "ol_treatment"), "cap")
    if out["treatment"] == "replace_value":
        out["value"] = str(_get(wkey(session, "ol_value"), ""))
    return out


def _detection(session, data, s: dict):
    """("ok", OutlierDetection) or ("error", message), cached per data state and settings."""
    settings = {k: s[k] for k in METHOD_KEYS[s["method"]]}
    key = (session.fingerprint, s["column"], s["method"], canonical_json(jsonable(settings)))
    cached = st.session_state.get(OUTLIER_ANALYSIS_KEY)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        found = detect_outliers(data[s["column"]], s["method"], settings)
    except CleaningError as exc:
        result = ("error", exc.message)
    else:
        result = ("ok", found) if found.ok else ("error", found.reason)
    st.session_state[OUTLIER_ANALYSIS_KEY] = (key, result)
    return result


def _statistics_text(found) -> str:
    s = found.statistics
    if found.method == "iqr":
        return f"Q1 {fmt(s['q1'])} · Q3 {fmt(s['q3'])} · IQR {fmt(s['iqr'])}"
    if found.method == "zscore":
        return f"Mean {fmt(s['mean'])} · standard deviation {fmt(s['std'])} (population, ddof = 0)"
    return f"Lower percentile value {fmt(s['lower_value'])} · upper percentile value {fmt(s['upper_value'])}"


def _show_detection(found) -> None:
    st.markdown("**Detection summary** · counted on the current data, nothing is changed")
    stat_row([
        stat_card_html("Lower boundary", fmt(found.lower), f"{plural(found.n_below, 'value')} below"),
        stat_card_html("Upper boundary", fmt(found.upper), f"{plural(found.n_above, 'value')} above"),
        stat_card_html("Outliers", f"{found.outliers:,}", f"{found.pct:.1f}% of {found.evaluated:,} values checked"),
        stat_card_html("Missing values", f"{found.missing:,}", "Not checked, never outliers"),
    ])
    st.caption(_statistics_text(found))
    for note in found.notes:
        callout("Check the boundaries", note, "caution")


def outliers_tab(session, preview: Preview | None) -> None:
    op = get_operation("treat_outliers")
    data = session.data
    with section("Outliers", key="clean-outliers"):
        st.caption(op.description)
        columns = eligible_columns(data)
        if not columns:
            callout("No numeric columns", NO_COLUMNS, "neutral")
            return
        st.selectbox("Numeric column", columns, index=None, key=wkey(session, "ol_column"),
                     placeholder="Choose a whole-number or decimal column",
                     help="Only whole-number and decimal columns are listed. Text columns are not parsed here.",
                     persist_state="session")
        method = st.selectbox("Detection method", list(METHODS), key=wkey(session, "ol_method"),
                              format_func=METHOD_LABELS.get, persist_state="session")
        st.caption(METHOD_HELP[method])
        if method == "iqr":
            st.number_input("IQR multiplier (k)", min_value=0.0, max_value=100.0, value=DEFAULTS["multiplier"],
                            step=0.5, key=wkey(session, "ol_multiplier"),
                            help="1.5 is the usual choice; 3 finds only extreme values. Must be more than 0.",
                            persist_state="session")
        elif method == "zscore":
            st.number_input("Z-score threshold", min_value=0.0, max_value=100.0, value=DEFAULTS["threshold"],
                            step=0.5, key=wkey(session, "ol_threshold"),
                            help="Values with |z| above this are outliers. Must be more than 0.",
                            persist_state="session")
        else:
            a, b = st.columns(2)
            with a:
                st.number_input("Lower percentile", min_value=0.0, max_value=100.0,
                                value=DEFAULTS["lower_percentile"], step=0.5, key=wkey(session, "ol_lower_percentile"),
                                persist_state="session")
            with b:
                st.number_input("Upper percentile", min_value=0.0, max_value=100.0,
                                value=DEFAULTS["upper_percentile"], step=0.5, key=wkey(session, "ol_upper_percentile"),
                                persist_state="session")
        st.caption(RULE_NOTE)

        s = outlier_settings(session)
        problem = None
        if s["column"] is None:
            problem = "Choose a column to check for outliers."
            st.caption(problem)
        else:
            status, value = _detection(session, data, s)
            if status == "error":
                problem = value
                callout("Cannot detect outliers with these settings", value, "caution")
            else:
                _show_detection(value)
                if not value.outliers:
                    problem = "No value is outside the boundaries, so there is nothing to treat."
                    st.caption(problem)

        st.markdown("**Treatment**")
        treatment = st.radio("What to do with the outliers", list(TREATMENTS), key=wkey(session, "ol_treatment"),
                             format_func=TREATMENT_LABELS.get, persist_state="session")
        st.caption(TREATMENT_HELP[treatment])
        if treatment == "replace_value":
            st.text_input("Replacement value", key=wkey(session, "ol_value"), placeholder="For example: 0",
                          help="A plain number such as 0, 42 or -3.5. Whole-number columns need a whole number.",
                          persist_state="session")
            if problem is None and not str(_get(wkey(session, "ol_value"), "")).strip():
                problem = "Enter the replacement value."
                st.caption(problem)

        def settings() -> dict:
            return outlier_settings(session)

        _preview_button("treat_outliers", settings, key="refine_preview_treat_outliers", disabled=problem is not None,
                        label="Preview treatment")
        st.caption("Nothing is changed until you preview the treatment and confirm it.")
    _show_preview_for("treat_outliers", preview, settings)

