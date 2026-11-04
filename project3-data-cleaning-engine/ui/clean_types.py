"""The Clean page's Data types tab: explicit type conversion.

The user picks columns, a target type, the parsing rules and what happens
to values that cannot be converted. A read-only analysis shows, before
any preview, how many values would convert and which would fail and why.
The step itself uses the shared Preview -> Confirm flow.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.conversion import (
    BOOLEAN_PRESETS,
    DATE_FORMAT_LABELS,
    DATE_FORMATS,
    KIND_LABELS,
    TARGET_LABELS,
    TARGETS,
    check_custom_format,
    column_kind,
    convert_column,
    dtype_name,
)
from refine.operations import Preview, canonical_json, get_operation, jsonable
from refine.operations.convert_ops import PRESERVE_EXPLANATION, WHAT
from ui.clean_forms import _get, _preview_button, _show_preview_for, _small_table, wkey
from ui.components import callout, section

from ui.state import CONVERSION_ANALYSIS_KEY as ANALYSIS_KEY

ON_ERROR_LABELS = {
    "strict": "Refuse the step if any value cannot be converted",
    "coerce": "Convert invalid values to missing",
    "preserve": "Keep invalid values as their original text",
}
THOUSANDS_LABELS = {None: "None (1250)", ",": "Comma (1,250)", ".": "Period (1.250)", " ": "Space (1 250)",
                    "'": "Apostrophe (1'250)"}
DECIMAL_LABELS = {".": "Period (3.14)", ",": "Comma (3,14)"}
PERCENT_LABELS = {"reject": "Refuse values with %", "fraction": "As a fraction: 12.5% → 0.125",
                  "number": "As the number written: 12.5% → 12.5"}
FRACTION_LABELS = {"reject": "Refuse values with a fractional part", "round_half_even": "Round half to even (2.5 → 2)",
                   "round_half_up": "Round half away from zero (2.5 → 3)"}
TIME_LABELS = {"reject": "Refuse values that have a time of day", "drop": "Keep only the date (the time is dropped)"}
TZ_LABELS = {"reject": "Refuse values with a time zone (keep times as written)",
             "to_utc": "Convert values with a time zone offset to UTC"}
DECIMAL_NOTE = ("Decimal numbers are stored as Float64, binary floating point with about 15-17 significant digits. "
                "Most decimal fractions, such as 0.1, are stored as the nearest binary value; REFINE warns when a "
                "value cannot be read back exactly. Exact decimal arithmetic (for example for currency) is not "
                "provided.")
DATE_NOTE = ("REFINE never guesses whether 05/06/2026 is 5 June or 6 May: choose month-first or day-first. "
             "Two-digit years and AM/PM times are refused. Dates are stored without a time of day; dates with "
             "times have no time zone unless you convert offsets to UTC, and nothing is ever shifted silently.")


def _types_table(session, data: pd.DataFrame, profile) -> pd.DataFrame:
    rows = []
    for c in session.columns:
        kind = column_kind(data[c].dtype)
        inferred = profile.column(c).inference.label if profile is not None else ""
        rows.append({"Column": c, "Stored as": dtype_name(data[c].dtype), "Kind": KIND_LABELS[kind],
                     "Profile suggests": inferred})
    return pd.DataFrame(rows)


def _numeric_widgets(session, target: str) -> None:
    a, b = st.columns(2)
    with a:
        st.selectbox("Thousands separator", list(THOUSANDS_LABELS), key=wkey(session, "ct_thousands"),
                     format_func=THOUSANDS_LABELS.get,
                     help="Off by default. When chosen, groups must have exactly three digits: 1,25,000 is refused.")
    with b:
        st.selectbox("Decimal separator", list(DECIMAL_LABELS), key=wkey(session, "ct_decimal"),
                     format_func=DECIMAL_LABELS.get)
    a, b = st.columns(2)
    with a:
        st.checkbox("Read currency symbols ($ € £ ¥ ₹)", key=wkey(session, "ct_currency"),
                    help="$1,250.50 reads as 1250.50. The symbol is not kept.")
    with b:
        st.checkbox("Read (250.75) as negative", key=wkey(session, "ct_parens"),
                    help="Accounting style: a value in parentheses is negative.")
    st.selectbox("Percentages", list(PERCENT_LABELS), key=wkey(session, "ct_percent"),
                 format_func=PERCENT_LABELS.get)
    if target == "integer":
        st.selectbox("Values with a fractional part (12.7)", list(FRACTION_LABELS), key=wkey(session, "ct_fraction"),
                     format_func=FRACTION_LABELS.get,
                     help="Whole-number columns never truncate silently: 12.7 is refused unless you choose rounding.")
    else:
        callout("About decimal numbers", DECIMAL_NOTE, "neutral")


def _date_widgets(session, target: str) -> None:
    callout("Dates are never guessed", DATE_NOTE, "neutral")
    formats = st.multiselect("Date formats in this column", list(DATE_FORMATS), key=wkey(session, "ct_formats"),
                             format_func=DATE_FORMAT_LABELS.get, placeholder="Choose one or more formats",
                             help="Year-first, one of month-first or day-first, and month names can be combined; "
                                  "they never overlap. A custom format is used on its own.")
    if "custom" in formats:
        st.text_input("Custom format", key=wkey(session, "ct_custom"), placeholder="%d.%m.%Y %H:%M",
                      help="Supported: %Y %m %d %H %M %S %f %z. Month names and 2-digit years are not.")
    if target == "date":
        st.radio("Values with a time of day", list(TIME_LABELS), key=wkey(session, "ct_time"),
                 format_func=TIME_LABELS.get)
    else:
        st.radio("Time zones", list(TZ_LABELS), key=wkey(session, "ct_tz"), format_func=TZ_LABELS.get,
                 help="With UTC, every value must include an offset such as Z or +02:00; values are converted "
                      "to the same instant in UTC and the original offset is not kept.")


def _boolean_widgets(session) -> None:
    st.multiselect("Mappings to use", list(BOOLEAN_PRESETS), key=wkey(session, "ct_presets"),
                   placeholder="For example: yes/no",
                   help="Each pair maps the first value to true and the second to false.")
    a, b = st.columns(2)
    with a:
        st.text_input("Other values meaning true", key=wkey(session, "ct_true"), placeholder="active, on",
                      help="Separate values with commas.")
    with b:
        st.text_input("Other values meaning false", key=wkey(session, "ct_false"), placeholder="inactive, off")
    st.checkbox("Match case exactly", key=wkey(session, "ct_bool_case"),
                help="When off, 'Yes', 'YES' and 'yes' all match 'yes'. Surrounding spaces are always ignored.")


def _split(text: str) -> list[str]:
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def conversion_settings(session) -> dict:
    """The settings currently in the widgets, as the operation expects them."""
    target = _get(wkey(session, "ct_target"), "integer")
    out: dict = {"columns": list(_get(wkey(session, "ct_columns"), [])), "target": target,
                 "on_error": _get(wkey(session, "ct_on_error"), "strict")}
    if not out["columns"]:
        return out
    data = session.data
    text_source = all(column_kind(data[c].dtype) == "text" for c in out["columns"] if c in data.columns)
    if target in ("integer", "decimal"):
        if text_source:
            out.update({
                "thousands_separator": st.session_state.get(wkey(session, "ct_thousands")),
                "decimal_separator": _get(wkey(session, "ct_decimal"), "."),
                "currency": bool(_get(wkey(session, "ct_currency"), False)),
                "parentheses_negative": bool(_get(wkey(session, "ct_parens"), False)),
                "percent": _get(wkey(session, "ct_percent"), "reject"),
            })
        if target == "integer":
            out["fraction"] = _get(wkey(session, "ct_fraction"), "reject")
    elif target in ("date", "datetime"):
        if text_source:
            formats = [f for f in DATE_FORMATS if f in (_get(wkey(session, "ct_formats"), []))]
            out["date_formats"] = formats
            if "custom" in formats:
                out["custom_format"] = str(_get(wkey(session, "ct_custom"), ""))
        if target == "date":
            out["time_of_day"] = _get(wkey(session, "ct_time"), "reject")
        else:
            out["timezone"] = _get(wkey(session, "ct_tz"), "reject")
    elif target == "boolean":
        true_values, false_values = [], []
        for preset in _get(wkey(session, "ct_presets"), []):
            t, f = BOOLEAN_PRESETS[preset]
            true_values += [v for v in t if v not in true_values]
            false_values += [v for v in f if v not in false_values]
        true_values += [v for v in _split(_get(wkey(session, "ct_true"), "")) if v not in true_values]
        false_values += [v for v in _split(_get(wkey(session, "ct_false"), "")) if v not in false_values]
        out.update({"true_values": true_values, "false_values": false_values,
                    "case_sensitive": bool(_get(wkey(session, "ct_bool_case"), False))})
    return out


def _not_ready(s: dict, data: pd.DataFrame) -> str | None:
    if not s["columns"]:
        return "Choose at least one column to convert."
    if s["on_error"] == "preserve":
        return "preserve"
    if s["target"] in ("date", "datetime") and "date_formats" in s:
        if not s["date_formats"]:
            return "Choose at least one date format."
        if "custom" in s["date_formats"]:
            return check_custom_format(s.get("custom_format", ""))
    if s["target"] == "boolean" and not (s["true_values"] and s["false_values"]):
        return "Choose or enter at least one value meaning true and one meaning false."
    return None


def _analysis(session, s: dict):
    """Per-column conversion statistics for the current settings (read-only, cached per data state)."""
    key = (session.fingerprint, canonical_json(jsonable(s)))
    cached = st.session_state.get(ANALYSIS_KEY)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        get_operation("convert_type").validate(dict(s, on_error="coerce"), session.data)
    except Exception as exc:          # Shown to the user as the reason nothing is analyzed
        result = ("error", getattr(exc, "message", str(exc)))
    else:
        data = session.data
        result = ("ok", [convert_column(data[c], s["target"], s)[1] for c in s["columns"]])
    st.session_state[ANALYSIS_KEY] = (key, result)
    return result


def _show_analysis(result) -> None:
    status, value = result
    if status == "error":
        callout("Cannot convert with these settings", value, "caution")
        return
    st.markdown("**What would happen** · counted on the current data, nothing is changed")
    _small_table(pd.DataFrame([{
        "Column": st_.column, "Stored as": st_.source_dtype, "To": st_.target_dtype, "Rows": st_.rows,
        "Missing now": st_.existing_missing, "Would convert": st_.converted, "Cannot convert": st_.failed,
        "Cannot convert %": round(st_.failed_pct, 1), "Ambiguous": st_.ambiguous,
    } for st_ in value]))
    failures = [{"Column": st_.column, "Value": f.before, "Cells": f.count, "Why": f.reason}
                for st_ in value for f in st_.failures]
    if failures:
        st.markdown("**Values that cannot be converted** · most frequent first")
        _small_table(pd.DataFrame(failures))
    examples = [{"Column": st_.column, "Before": e.before, "After": e.after, "Cells": e.count}
                for st_ in value for e in st_.examples[:5]]
    if examples:
        st.markdown("**Examples of converted values**")
        _small_table(pd.DataFrame(examples))


def types_tab(ds, session, preview: Preview | None, profile=None) -> None:
    op = get_operation("convert_type")
    data = session.data
    with section("Data types", key="clean-types"):
        st.caption(op.description)
        with st.expander("Current column types", expanded=False):
            _small_table(_types_table(session, data, profile), rows=10)
        columns = st.multiselect("Columns to convert", list(session.columns), key=wkey(session, "ct_columns"),
                                 placeholder="Choose one or more columns of the same kind")
        target = st.selectbox("Convert to", list(TARGETS), index=1, key=wkey(session, "ct_target"),
                              format_func=TARGET_LABELS.get)
        kinds = {column_kind(data[c].dtype) for c in columns}
        text_source = kinds == {"text"}
        if columns and not text_source:
            st.caption("The selected columns are already typed, so text parsing options do not apply. Their values "
                       "are read as REFINE writes them (1250.5, 2026-01-15).")
        if target in ("integer", "decimal"):
            if text_source:
                _numeric_widgets(session, target)
            elif target == "integer":
                st.selectbox("Values with a fractional part (12.7)", list(FRACTION_LABELS),
                             key=wkey(session, "ct_fraction"), format_func=FRACTION_LABELS.get)
        elif target in ("date", "datetime"):
            if text_source:
                _date_widgets(session, target)
            elif target == "date":
                st.radio("Values with a time of day", list(TIME_LABELS), key=wkey(session, "ct_time"),
                         format_func=TIME_LABELS.get)
        elif target == "boolean":
            _boolean_widgets(session)
        st.radio("Values that cannot be converted", list(ON_ERROR_LABELS), key=wkey(session, "ct_on_error"),
                 format_func=ON_ERROR_LABELS.get)

        def settings() -> dict:
            return conversion_settings(session)

        s = settings()
        problem = _not_ready(s, data)
        if problem == "preserve":
            callout("Not possible for typed columns",
                    PRESERVE_EXPLANATION.format(target=TARGET_LABELS[target], what=WHAT[target]), "caution")
        elif problem:
            st.caption(problem)
        else:
            _show_analysis(_analysis(session, s))
        _preview_button("convert_type", settings, key="refine_preview_convert_type", disabled=problem is not None,
                        label="Preview conversion")
        st.caption("Nothing is converted until you preview the step and confirm it.")
    _show_preview_for("convert_type", preview, settings)
