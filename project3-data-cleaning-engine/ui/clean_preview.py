"""The preview panel and confirmation dialog shared by every cleaning operation.

A preview never changes data. "Apply step…" only opens the dialog; the
step is applied by the dialog's confirm button callback, bound to that
preview's token (ui/state.confirm_apply).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.notes import plural, quote
from refine.operations import Preview, canonical_json, jsonable
from ui.components import callout, section, stat_card_html, stat_row
from ui.state import confirm_apply, discard_preview, preview_request

LOSS_OPERATIONS = {"drop_columns", "drop_missing_rows", "drop_missing_columns", "remove_duplicates"}
TEXT_OPERATIONS = {"trim_whitespace", "change_case", "collapse_whitespace", "normalize_unicode",
                   "remove_control_characters", "find_replace", "map_categories"}


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "0.0%"


def _shape_cards(impact) -> list[str]:
    removed_rows = impact.rows_before - impact.rows_after
    cols = impact.columns_after - impact.columns_before
    return [
        stat_card_html("Rows", f"{impact.rows_before:,} → {impact.rows_after:,}",
                       "Unchanged" if not removed_rows else f"{removed_rows:,} removed"),
        stat_card_html("Columns", f"{impact.columns_before:,} → {impact.columns_after:,}",
                       "Unchanged" if not cols else (f"{cols:,} added" if cols > 0 else f"{-cols:,} removed")),
    ]


def impact_cards(preview: Preview) -> list[str]:
    """KPI cards for a preview, chosen by operation."""
    op, impact, r = preview.plan.operation, preview.impact, preview.plan.resolved
    c = impact.counts
    rows = impact.rows_before
    if op == "drop_columns":
        return [
            *_shape_cards(impact)[:1],
            stat_card_html("Columns", f"{impact.columns_before:,} → {impact.columns_after:,}",
                           f"{plural(c.get('columns_removed', 0), 'column')} removed"),
            stat_card_html("Values removed", f"{c.get('values_removed', 0):,}",
                           f"Non-missing, of {c.get('cells_removed', 0):,} cells"),
        ]
    if op == "standardize_missing":
        n = c.get("cells_changed", 0)
        return [stat_card_html("Cells to mark missing", f"{n:,}",
                               f"In {plural(c.get('columns_changed', 0), 'column')}"),
                *_shape_cards(impact)]
    if op == "drop_missing_rows":
        n = c.get("rows_removed", 0)
        return [stat_card_html("Rows removed", f"{n:,}", f"{_pct(n, rows)} of {rows:,} rows"),
                *_shape_cards(impact)]
    if op == "drop_missing_columns":
        return [stat_card_html("Columns removed", f"{c.get('columns_removed', 0):,}",
                               f"At least {r['threshold']:g}% missing"),
                stat_card_html("Values removed", f"{c.get('values_removed', 0):,}",
                               f"Non-missing, of {c.get('cells_removed', 0):,} cells"),
                *_shape_cards(impact)]
    if op == "fill_missing":
        left = c.get("cells_left_missing", 0)
        return [stat_card_html("Cells filled", f"{c.get('cells_filled', 0):,}",
                               f"In {plural(c.get('columns_changed', 0), 'column')}"),
                stat_card_html("Still missing", f"{left:,}", "Cannot be filled" if left else "None left"),
                *_shape_cards(impact)]
    if op == "add_missing_indicator":
        n = c.get("rows_flagged", 0)
        return [stat_card_html("New column", r["name"], f"After {quote(r['column'])}"),
                stat_card_html(f"Rows marked '{r['true_text']}'", f"{n:,}", f"{_pct(n, rows)} of {rows:,} rows"),
                *_shape_cards(impact)]
    if op == "convert_type":
        stats = r["stats"].values()
        present = sum(s["rows"] - s["existing_missing"] for s in stats)
        failed = c.get("cells_failed", 0)
        return [stat_card_html("Values converted", f"{c.get('cells_converted', 0):,}",
                               f"Of {present:,} non-missing values"),
                stat_card_html("Cannot convert", f"{failed:,}",
                               "Become missing" if failed else "None"),
                stat_card_html("Already missing", f"{sum(s['existing_missing'] for s in stats):,}",
                               "Stay missing"),
                *_shape_cards(impact)]
    if op in TEXT_OPERATIONS:
        cards = [stat_card_html("Cells changed", f"{c.get('cells_changed', 0):,}",
                                f"In {plural(c.get('columns_changed', 0), 'column')}")]
        if op == "find_replace":
            cards.append(stat_card_html("Replacements", f"{c.get('replacements', 0):,}", "Matches replaced"))
        cards.append(stat_card_html("Become empty", f"{r.get('became_empty', 0):,}", "Counted as missing"))
        return [*cards, *_shape_cards(impact)]
    if op == "remove_duplicates":
        n = c.get("rows_removed", 0)
        return [stat_card_html("Duplicate groups", f"{c.get('groups', 0):,}", "Sets of matching rows"),
                stat_card_html("Rows removed", f"{n:,}", f"{_pct(n, rows)} of {rows:,} rows"),
                *_shape_cards(impact)]
    return _shape_cards(impact)


def _details(preview: Preview) -> None:
    """Operation-specific tables from the frozen plan."""
    op, r = preview.plan.operation, preview.plan.resolved
    if op == "standardize_missing":
        rows = [{"Column": c, "Text found": repr(v) if v.strip() == "" else v, "Cells": n}
                for c, values in r["values"].items() for v, n in values.items()]
        st.markdown("**Cells that would become missing**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    elif op == "drop_missing_columns":
        st.markdown("**Columns that qualify**")
        st.dataframe(pd.DataFrame([{"Column": c, "Missing cells": r["missing"][c],
                                    "Missing %": round(r["missing_pct"][c], 1)} for c in r["columns"]]),
                     hide_index=True, width="stretch")
    elif op == "convert_type":
        st.markdown("**Conversion by column (frozen in the step)**")
        st.dataframe(pd.DataFrame([{
            "Column": col, "Stored as": r["source_dtypes"][col], "Rows": s["rows"], "Missing now": s["existing_missing"],
            "Converted": s["converted"], "Cannot convert": s["failed"], "Ambiguous": s["ambiguous"],
            "Rounded": s["rounded"], "Precision loss": s["precision_loss"], "Time dropped": s["time_dropped"],
        } for col, s in r["stats"].items()]), hide_index=True, width="stretch")
        failures = [{"Column": col, "Value": v, "Cells": n, "Why": why}
                    for col, rows in r["failures"].items() for v, n, why in rows]
        if failures:
            st.markdown("**Values that cannot be converted** · they become missing")
            st.dataframe(pd.DataFrame(failures), hide_index=True, width="stretch")
    elif op == "map_categories":
        st.markdown("**Mappings (frozen in the step)**")
        st.dataframe(pd.DataFrame([{"From": a, "To": b} for a, b in r["mapping"]]), hide_index=True,
                     width="stretch")
    elif op == "fill_missing" and r["method"] in ("constant", "mean", "median", "mode"):
        st.markdown("**Fill values (frozen in the step)**")
        st.dataframe(pd.DataFrame([{"Column": c, "Fill value": v, "Cells filled": r["cells"][c]}
                                   for c, v in r["fills"].items()]), hide_index=True, width="stretch")


SAMPLE_CAPTIONS = {
    "drop_columns": "first {n} rows of the columns to drop",
    "standardize_missing": "first {n} rows with a matching cell, before and after",
    "drop_missing_rows": "first {n} rows that would be removed",
    "drop_missing_columns": "first {n} rows of the columns to drop",
    "fill_missing": "first {n} rows with a cell to fill, before and after",
    "add_missing_indicator": "a few rows with and without a missing value, and the new column",
    "remove_duplicates": "the first duplicate groups; in each, one row is kept and the others removed",
    "convert_type": "rows that cannot be converted first, then converted rows, before and after (written as text)",
    **{op: "first {n} rows with a cell that changes, before and after" for op in TEXT_OPERATIONS},
}


def confirm_dialog_body(preview: Preview) -> None:
    plan, impact = preview.plan, preview.impact
    st.markdown(f"**{plan.label}**")
    st.markdown(plan.summary)
    st.markdown(
        f"Rows: {impact.rows_before:,} → {impact.rows_after:,} · "
        f"Columns: {impact.columns_before:,} → {impact.columns_after:,}"
    )
    for warning in plan.warnings:
        st.warning(warning, icon=":material/warning:")
    st.caption("You can undo this step, or reset to the original data, on the History page.")
    left, right = st.columns(2)
    with left:
        if st.button("Confirm and apply", type="primary", icon=":material/check:", key="refine_confirm_apply",
                     on_click=confirm_apply, args=(preview.token,), width="stretch"):
            st.rerun()
    with right:
        if st.button("Cancel", key="refine_confirm_cancel", width="stretch"):
            st.rerun()


confirm_dialog = st.dialog("Confirm cleaning step")(confirm_dialog_body)


def same_settings(operation: str, current: dict) -> bool:
    request = preview_request()
    if request is None:
        return False
    return request[0] == operation and canonical_json(jsonable(request[1])) == canonical_json(jsonable(current))


def render_preview(preview: Preview, current: dict) -> None:
    """The preview panel: summary, counts, warnings, details, sample, and Apply / Discard."""
    plan, impact = preview.plan, preview.impact
    with section("Preview", key="clean-preview",
                 caption="What this step would change. Nothing has been changed yet."):
        st.markdown(f"**{plan.label}** · {plan.summary}")
        stat_row(impact_cards(preview))
        if plan.warnings:
            title = "Possible data loss" if plan.operation in LOSS_OPERATIONS else "Check before applying"
            for warning in plan.warnings:
                callout(title, warning, "caution")
        elif plan.operation == "drop_columns":
            callout("No values lost", "The selected columns hold no values; only empty cells are removed.",
                    "neutral")
        _details(preview)
        n = len(preview.sample)
        what = SAMPLE_CAPTIONS.get(plan.operation, "first {n} affected rows").format(n=f"{n:,}")
        st.caption(f"Affected data: {what}. 'Original row' is the row's position in the uploaded table. "
                   "A cell with no value is shown as a grey None.")
        st.dataframe(preview.sample, width="stretch", height=min(400, 38 + 35 * max(n, 1)))

        if not same_settings(plan.operation, current):
            callout("Selection changed",
                    "The settings above differ from this preview. Preview again to apply the new settings.",
                    "caution")
            st.button("Discard preview", key="refine_discard_preview", on_click=discard_preview)
            return
        a, b = st.columns([1, 1])
        with a:
            if st.button("Apply step…", type="primary", icon=":material/play_arrow:", key="refine_apply",
                         width="stretch"):
                confirm_dialog(preview)
        with b:
            st.button("Discard preview", key="refine_discard_preview", on_click=discard_preview,
                      width="stretch")
