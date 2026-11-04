"""History page: the applied steps, the full audit log, undo, reset and the recipe."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from refine.notes import plural
from ui.components import (
    callout,
    empty_state,
    no_dataset_state,
    page_header,
    section,
    show_flash,
    stat_card_html,
    stat_row,
)
from ui.state import get_dataset, get_session, reset_to_original, undo_last, verify_replay

CLEAN_PAGE = "views/clean.py"
EVENT_LABELS = {"apply": "Applied", "undo": "Undone", "reset": "Reset"}


@st.dialog("Reset to original data")
def _reset_dialog(n_steps: int) -> None:
    st.markdown(
        f"This discards {plural(n_steps, 'applied step')} and returns the table to the data as it was "
        "loaded. The audit log keeps a record of the steps and of the reset."
    )
    st.caption("Reset cannot be undone, but you can apply the steps again.")
    left, right = st.columns(2)
    with left:
        if st.button("Reset to original", type="primary", icon=":material/restart_alt:", key="refine_confirm_reset",
                     on_click=reset_to_original, width="stretch"):
            st.rerun()
    with right:
        if st.button("Cancel", key="refine_cancel_reset", width="stretch"):
            st.rerun()


def _steps_section(session) -> None:
    steps = session.steps
    with section("Applied steps", key="history-steps",
                 caption="The steps that make up the current data, in the order they were applied."):
        if not steps:
            callout("No steps applied", "The current data is the original parsed table.", "neutral")
            st.page_link(CLEAN_PAGE, label="Go to Clean", icon=":material/cleaning_services:")
            return
        audit = {e.sequence: e for e in session.audit_log}
        rows = []
        for number, step in enumerate(steps, start=1):
            entry = audit[step.audit_sequence]
            rows.append({
                "Step": number, "Operation": step.plan.label, "Change": step.plan.summary,
                "Rows": f"{entry.rows_before:,} → {entry.rows_after:,}",
                "Columns": f"{entry.columns_before:,} → {entry.columns_after:,}",
                "Applied": entry.timestamp,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Change": st.column_config.TextColumn(width="large")})
        last = steps[-1]
        a, b = st.columns(2)
        with a:
            st.button(f"Undo step {len(steps)}: {last.plan.label}", icon=":material/undo:", key="refine_undo",
                      on_click=undo_last, width="stretch")
        with b:
            if st.button("Reset to original…", icon=":material/restart_alt:", key="refine_reset", width="stretch"):
                _reset_dialog(len(steps))


def _audit_section(session) -> None:
    log = session.audit_log
    with section("Audit log", key="history-audit",
                 caption="Every event in this session, including undo and reset. Times are UTC."):
        if not log:
            empty_state("No events yet", "Applied steps, undo and reset are recorded here.")
            return
        frame = pd.DataFrame([{
            "#": e.sequence, "Event": EVENT_LABELS[e.event], "Operation": e.label, "Details": e.summary,
            "Rows": f"{e.rows_before:,} → {e.rows_after:,}",
            "Columns": f"{e.columns_before:,} → {e.columns_after:,}",
            "Time (UTC)": e.timestamp, "Fingerprint after": e.fingerprint_after[:12],
        } for e in reversed(log)])
        st.dataframe(frame, hide_index=True, width="stretch",
                     column_config={"Details": st.column_config.TextColumn(width="large")})
        for e in reversed(log):
            with st.expander(f"#{e.sequence} · {EVENT_LABELS[e.event]} · {e.label}"):
                if e.affected_columns:
                    st.markdown("**Affected columns:** " + ", ".join(f"`{c}`" for c in e.affected_columns))
                for warning in e.warnings:
                    st.caption(f"Warning shown before applying: {warning}")
                st.code(json.dumps(e.to_dict(), indent=2, ensure_ascii=False), language="json")


def _recipe_section(session) -> None:
    with section("Recipe", key="history-recipe",
                 caption="The applied steps with every resolved value, as JSON. The same recipe on the same "
                         "data always gives the same result. It has no timestamps."):
        st.button("Verify by replaying", icon=":material/replay:", key="refine_verify_replay",
                  on_click=verify_replay, disabled=not session.steps,
                  help="Rebuild the current data from the original using only the recipe, and compare fingerprints.")
        with st.expander("Show recipe JSON", icon=":material/data_object:"):
            st.code(session.recipe_json(indent=2), language="json")


def render() -> None:
    page_header("History", "Every confirmed step, in order, with undo and reset.")
    ds = get_dataset()
    if ds is None:
        no_dataset_state()
        return
    session = get_session(ds)
    show_flash()
    rows, cols = session.shape
    stat_row([
        stat_card_html("Steps applied", f"{len(session.steps):,}", "Make up the current data"),
        stat_card_html("Audit events", f"{len(session.audit_log):,}", "Applied, undone and reset"),
        stat_card_html("Current data", f"{rows:,} × {cols:,}", "Rows × columns"),
        stat_card_html("Original data", f"{ds.source.n_rows:,} × {ds.source.n_columns:,}", "As loaded"),
    ])
    _steps_section(session)
    _audit_section(session)
    _recipe_section(session)


render()
