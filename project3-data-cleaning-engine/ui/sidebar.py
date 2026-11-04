"""Sidebar brand and dataset status, shown on every page."""

from __future__ import annotations

from html import escape

import streamlit as st

from refine.csv_reader import delimiter_label
from refine.notes import plural
from ui.state import get_dataset, get_session


def _html(markup: str) -> None:
    st.sidebar.markdown(markup, unsafe_allow_html=True)


def render_brand() -> None:
    _html(
        '<div class="refine-brand"><span class="refine-brand-name">REFINE</span>'
        '<span class="refine-brand-tag">Data cleaning engine</span></div>'
    )


def render_status() -> None:
    ds = get_dataset()
    _html('<div class="refine-side-label">Dataset</div>')
    if ds is None:
        _html('<div class="refine-side-meta">No dataset loaded.</div>')
        return
    src = ds.source
    if src.file_format == "csv":
        detail = f"CSV · {src.encoding} · {delimiter_label(src.delimiter)}"
    else:
        detail = f"XLSX · sheet '{src.sheet_name}'"
    checks = len(ds.warnings)
    session = get_session(ds)
    rows, cols = session.shape
    steps = len(session.steps)
    cleaning = (f"{plural(steps, 'cleaning step')} applied · original {src.n_rows:,} × {src.n_columns:,}"
                if steps else "Original data, no cleaning steps")
    _html(
        f'<div class="refine-side-value">{escape(src.file_name)}</div>'
        f'<div class="refine-side-meta">{plural(rows, "row")} × {plural(cols, "column")}<br>'
        f"{escape(cleaning)}<br>"
        f"{escape(detail)}<br>"
        f"{plural(checks, 'load note') + ' to check' if checks else 'No load warnings'}</div>"
    )
