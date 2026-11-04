"""The Clean page's Text tab: trimming, capitalization, spacing, Unicode,
non-printing characters, find and replace, and category mapping.

Only text columns are offered. Category mappings are built explicitly:
the user picks observed values and the canonical value they map to (or
adds the case/spacing suggestions with a button); nothing is merged
until the mapping is previewed and confirmed.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.conversion import column_kind
from refine.notes import plural, quote
from refine.operations import Preview, get_operation
from refine.operations.text_ops import TEXT_OPERATION_IDS
from refine.text import CASE_LABELS, CASES, INVISIBLE_FORMAT, map_distinct, suggest_categories, value_frequencies
from ui.clean_forms import _get, _preview_button, _show_preview_for, _small_table, wkey
from ui.components import callout, section
from ui.state import discard_preview

MAX_LISTED_VALUES = 500
CASE_HELP = {
    "lower": "'WICHITA' → 'wichita'",
    "upper": "'wichita' → 'WICHITA'",
    "title": "'wIchItA' → 'Wichita'. Lower-cases the rest of each word, so check names such as McDonald and "
             "acronyms such as USA.",
    "casefold": "Like lowercase, but also folds letters for matching ('Straße' → 'strasse').",
}


def _shown(value: str) -> str:
    """A value with invisible differences made visible, for option lists."""
    return repr(value) if value != value.strip() or "  " in value or not value.isprintable() else value


def _columns(session, data, key: str) -> list[str]:
    text_cols = [c for c in session.columns if column_kind(data[c].dtype) == "text"]
    typed = [c for c in session.columns if c not in text_cols]
    chosen = st.multiselect("Text columns", text_cols, key=key, placeholder="Choose one or more columns")
    if typed:
        st.caption(f"Typed columns are not listed ({', '.join(map(quote, typed[:6]))}"
                   f"{' and more' if len(typed) > 6 else ''}): text rules would have to turn their values into "
                   "text. Convert a column to text first on the Data types tab if that is what you want.")
    return chosen


def _live_counts(data, columns, rule) -> None:
    if not columns:
        return
    rows = []
    for c in columns:
        change = map_distinct(data[c], rule, example_limit=3)
        rows.append({"Column": c, "Cells that change": change.cells, "Distinct values that change": change.distinct,
                     "Examples": "; ".join(f"{_shown(b)} → {_shown(a)}" for b, a, _ in change.examples)})
    total = sum(r["Cells that change"] for r in rows)
    st.markdown(f"**Would change: {plural(total, 'cell')}**")
    _small_table(pd.DataFrame(rows))


def _rule_for(op_id: str, s: dict):
    """The text rule for the settings, through the operation itself (so the counts always agree)."""
    op = get_operation(op_id)
    return op.rule(s)


def _simple_form(op_id: str, session, data, preview) -> None:
    k_cols = wkey(session, f"tx_{op_id}_columns")
    columns = _columns(session, data, k_cols)
    extra_keys: dict[str, str] = {}
    if op_id == "change_case":
        k = wkey(session, "tx_case")
        case = st.radio("Capitalization", list(CASES), key=k, format_func=CASE_LABELS.get, horizontal=True)
        st.caption(CASE_HELP[case])
        extra_keys["case"] = k
    elif op_id == "collapse_whitespace":
        k = wkey(session, "tx_all_ws")
        st.checkbox("Also treat tabs, line breaks and no-break spaces as spaces", key=k,
                    help="Off: only runs of ordinary spaces are collapsed ('New    York' → 'New York'). On: any "
                         "run of whitespace becomes one space.")
        extra_keys["all_whitespace"] = k
    elif op_id == "normalize_unicode":
        k = wkey(session, "tx_form")
        st.radio("Normalization form", ["NFKC", "NFC"], key=k, horizontal=True,
                 format_func={"NFKC": "NFKC (compatibility: full-width ＡＢＣ → ABC)",
                              "NFC": "NFC (canonical: joins accents only)"}.get)
        extra_keys["form"] = k
    elif op_id == "remove_control_characters":
        k1, k2 = wkey(session, "tx_keep_ws"), wkey(session, "tx_invisible")
        st.checkbox("Keep tabs and line breaks", key=k1, value=True)
        st.checkbox("Also remove invisible formatting characters", key=k2,
                    help="Removes only: " + ", ".join(INVISIBLE_FORMAT.values()) + ". Zero-width joiners, "
                         "which several languages and emoji need, are kept.")
        extra_keys["keep_whitespace"], extra_keys["remove_invisible"] = k1, k2
    elif op_id == "find_replace":
        k_find, k_rep = wkey(session, "tx_find"), wkey(session, "tx_replace")
        k_match, k_case = wkey(session, "tx_match"), wkey(session, "tx_case_sensitive")
        a, b = st.columns(2)
        with a:
            st.text_input("Find", key=k_find, placeholder="Text to find", help="Matched literally, spaces included.")
        with b:
            st.text_input("Replace with", key=k_rep, placeholder="Leave empty to delete the text")
        st.radio("Match", ["whole_cell", "substring"], key=k_match, horizontal=True,
                 format_func={"whole_cell": "Whole cell equals the text", "substring": "Every occurrence inside cells"}.get)
        st.checkbox("Match case exactly", key=k_case, value=True)
        extra_keys.update(find=k_find, replace=k_rep, match=k_match, case_sensitive=k_case)

    defaults = {"case": "lower", "all_whitespace": False, "form": "NFKC", "keep_whitespace": True,
                "remove_invisible": False, "find": "", "replace": "", "match": "whole_cell", "case_sensitive": True}

    def settings() -> dict:
        out = {"columns": list(_get(k_cols, []))}
        for name, key in extra_keys.items():
            value = st.session_state.get(key, defaults[name])
            out[name] = defaults[name] if value is None else value
        if op_id == "find_replace":
            out["find"], out["replace"] = str(out["find"]), str(out["replace"])
        return out

    s = settings()
    ready = bool(columns) and (op_id != "find_replace" or bool(s.get("find")))
    if ready:
        try:
            rule = _rule_for(op_id, s)
        except Exception:
            rule = None
        if rule is not None:
            _live_counts(data, columns, rule)
    elif columns and op_id == "find_replace":
        st.caption("Enter the text to find.")
    _preview_button(op_id, settings, key=f"refine_preview_{op_id}", disabled=not ready)
    if not columns:
        st.caption("Choose at least one text column to preview.")
    _show_preview_for(op_id, preview, settings)


# ---- Category mapping ----------------------------------------------

def _pairs_key(session, column: str) -> str:
    return wkey(session, f"mc_pairs_{column}")


def add_pairs(current: dict[str, str], pairs: list[tuple[str, str]]) -> dict[str, str]:
    """The mapping after adding pairs; the latest choice wins.

    Mapping s -> t also points values that were mapped to s at t, and
    makes t canonical (an earlier mapping of t itself is dropped), so the
    mapping never chains. Every resulting pair is shown before preview.
    """
    out = dict(current)
    for source, target in pairs:
        out = {s: (target if t == source else t) for s, t in out.items()}
        out.pop(target, None)
        out[source] = target
        out = {s: t for s, t in out.items() if s != t}
    return out


def _add_pairs(key: str, pairs: list[tuple[str, str]]) -> None:
    st.session_state[key] = add_pairs(st.session_state.get(key, {}), pairs)


def _add_selected(key: str, values_key: str, target_key: str) -> None:
    target = str(st.session_state.get(target_key) or "")
    if not target.strip():
        return
    _add_pairs(key, [(v, target) for v in st.session_state.get(values_key) or []])
    st.session_state[values_key] = []
    discard_preview()


def _add_suggestions(key: str, suggestions: dict[str, str]) -> None:
    _add_pairs(key, list(suggestions.items()))
    discard_preview()


def _clear_pairs(key: str) -> None:
    st.session_state[key] = {}
    discard_preview()


def _categories_form(session, data, preview) -> None:
    text_cols = [c for c in session.columns if column_kind(data[c].dtype) == "text"]
    k_col = wkey(session, "mc_column")
    column = st.selectbox("Column", text_cols, key=k_col, index=None, placeholder="Choose a text column")

    def settings() -> dict:
        col = st.session_state.get(k_col)
        pairs = st.session_state.get(_pairs_key(session, col), {}) if col else {}
        return {"column": col, "mapping": [[s, t] for s, t in pairs.items()]}

    if column is None:
        st.caption("Choose a column to see its values.")
        _show_preview_for("map_categories", preview, settings)
        return
    freqs = value_frequencies(data[column])
    suggestions = suggest_categories(freqs)
    key = _pairs_key(session, column)
    pairs: dict[str, str] = st.session_state.get(key, {})

    st.markdown(f"**Observed values** · {plural(len(freqs), 'distinct value')}, most frequent first")
    table = pd.DataFrame([{"Value": _shown(v), "Cells": n, "Suggested (case/spacing)": suggestions.get(v, ""),
                           "Mapped to": pairs.get(v, "")} for v, n in freqs[:MAX_LISTED_VALUES]])
    _small_table(table, rows=10)
    if len(freqs) > MAX_LISTED_VALUES:
        st.caption(f"Showing the {MAX_LISTED_VALUES} most frequent values.")
    if suggestions:
        st.caption(f"{plural(len(suggestions), 'value')} differ from a more frequent value only in case or spacing. "
                   "These are suggestions: nothing is mapped unless you add them.")
        st.button("Add the suggested mappings", key=wkey(session, "mc_add_suggestions"),
                  icon=":material/auto_fix_high:", on_click=_add_suggestions, args=(key, suggestions))

    k_values, k_target = wkey(session, "mc_values"), wkey(session, "mc_target")
    options = [v for v, _ in freqs[:MAX_LISTED_VALUES]]
    st.multiselect("Values to map", options, key=k_values, format_func=_shown,
                   placeholder="Choose observed values")
    st.text_input("Canonical value", key=k_target, placeholder="For example: New York",
                  help="The chosen values are replaced by exactly this text.")
    ready_add = bool(st.session_state.get(k_values)) and bool(str(_get(k_target, "")).strip())
    st.button("Add mapping", key=wkey(session, "mc_add"), icon=":material/add:", disabled=not ready_add,
              on_click=_add_selected, args=(key, k_values, k_target))

    if pairs:
        st.markdown(f"**Mappings to apply: {len(pairs):,}**")
        _small_table(pd.DataFrame([{"From": _shown(s), "To": t} for s, t in pairs.items()]))
        st.button("Clear mappings", key=wkey(session, "mc_clear"), on_click=_clear_pairs, args=(key,))
    else:
        st.caption("No mappings yet. Unmapped values are always kept as they are.")
    _preview_button("map_categories", settings, key="refine_preview_map_categories", disabled=not pairs)
    _show_preview_for("map_categories", preview, settings)


def text_tab(session, preview: Preview | None) -> None:
    labels = {op: get_operation(op).label for op in TEXT_OPERATION_IDS}
    with section("Text", key="clean-text"):
        op_id = st.selectbox("Text operation", list(labels), key="refine_text_operation", format_func=labels.get,
                             on_change=discard_preview)
        st.caption(get_operation(op_id).description)
        callout("Missing values are kept", "Cells with no value stay missing. A rule that leaves a cell empty "
                "(for example trimming a cell that holds only spaces) makes it empty text, which REFINE counts as "
                "missing; the preview says how many.", "neutral")
        data = session.data
        if op_id == "map_categories":
            _categories_form(session, data, preview)
        else:
            _simple_form(op_id, session, data, preview)
