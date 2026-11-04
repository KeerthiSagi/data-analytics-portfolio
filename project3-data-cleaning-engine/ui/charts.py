"""Plotly figures for the Profile & Quality page.

Builders return figures; `show_chart` applies the REFINE template. Labels
that come from the data are HTML-escaped, because Plotly renders a subset
of HTML tags in tick labels and hover text.
"""

from __future__ import annotations

from html import escape

import plotly.graph_objects as go
import streamlit as st

from refine.profile import SEMANTIC_LABELS, ColumnProfile, DatasetProfile
from ui import theme

MAX_MISSING_BARS = 30
MISSING_SERIES = (
    # (attribute, legend name, color): fixed order, so colors follow the series.
    ("missing_count", "Missing (no value or empty text)", theme.CATEGORICAL[0]),
    ("null_token_count", "Null-like text", theme.CATEGORICAL[1]),
    ("whitespace_only_count", "Whitespace-only", theme.CATEGORICAL[2]),
)


def show_chart(fig: go.Figure, height: int = 360, key: str | None = None) -> None:
    """Render with the REFINE template. theme=None stops Streamlit restyling it."""
    theme.style_figure(fig, height=height)
    st.plotly_chart(fig, theme=None, config=theme.PLOTLY_CONFIG, width="stretch", key=key)


def display_value(value: str) -> str:
    """Show a cell value so empty text and edge whitespace are visible."""
    if value == "":
        return "(empty text)"
    if value != value.strip() or "  " in value or "\t" in value or "\n" in value:
        return '"' + value.replace("\t", "\\t").replace("\n", "\\n") + '"'
    return value


def _label(text: str, limit: int = 40) -> str:
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return escape(text)


def missing_columns(profile: DatasetProfile) -> list[ColumnProfile]:
    """Columns with any missing, null-like or whitespace-only cells, worst first."""
    def total(c: ColumnProfile) -> int:
        return c.missing_count + c.null_token_count + c.whitespace_only_count
    cols = [c for c in profile.columns if total(c)]
    return sorted(cols, key=lambda c: (-total(c), c.position))


def missing_chart(profile: DatasetProfile) -> tuple[go.Figure, int]:
    """Stacked horizontal bars: share of rows per column, by kind of gap."""
    cols = missing_columns(profile)[:MAX_MISSING_BARS]
    cols = list(reversed(cols))            # Worst at the top
    y = list(range(len(cols)))
    fig = go.Figure()
    n = profile.n_rows or 1
    for attr, name, color in MISSING_SERIES:
        counts = [getattr(c, attr) for c in cols]
        if not any(counts):
            continue
        fig.add_bar(
            y=y, x=[100 * k / n for k in counts], orientation="h", name=name,
            marker=dict(color=color, line=dict(color=theme.CARD, width=1.5)),
            customdata=[[_label(c.name, 60), k] for c, k in zip(cols, counts)],
            hovertemplate=f"%{{customdata[0]}}<br>{escape(name)}: %{{customdata[1]:,}} rows "
                          "(%{x:.1f}%)<extra></extra>",
        )
    fig.update_layout(
        barmode="stack", showlegend=True,
        xaxis=dict(title="Share of rows", ticksuffix="%", rangemode="tozero"),
        yaxis=dict(tickvals=y, ticktext=[_label(c.name) for c in cols]),
        bargap=0.3,
    )
    return fig, 80 + 26 * max(len(cols), 3)


def type_chart(profile: DatasetProfile) -> tuple[go.Figure, int]:
    """Columns per suggested semantic type (one series, one color)."""
    counts = profile.semantic_type_counts
    order = sorted(counts, key=lambda t: (-counts[t], t))
    order.reverse()
    fig = go.Figure(go.Bar(
        y=[SEMANTIC_LABELS[t] for t in order], x=[counts[t] for t in order], orientation="h",
        marker=dict(color=theme.ACCENT, cornerradius=4),
        hovertemplate="%{y}: %{x:,} columns<extra></extra>",
        text=[f"{counts[t]:,}" for t in order], textposition="outside",
        textfont=dict(color=theme.TEXT_SECONDARY),
    ))
    fig.update_layout(showlegend=False, xaxis=dict(title="Columns", rangemode="tozero", dtick=1 if max(counts.values(), default=0) <= 10 else None))
    return fig, 80 + 30 * max(len(order), 3)


def top_values_chart(column: ColumnProfile) -> tuple[go.Figure, int]:
    """Most frequent values, plus a neutral 'missing' bar so shares add up visibly."""
    items = [(display_value(v.value), v.count, theme.ACCENT) for v in column.top_values]
    if column.missing_count:
        items.append(("(missing)", column.missing_count, theme.NEUTRAL))
    items.sort(key=lambda it: -it[1])
    items.reverse()
    y = list(range(len(items)))
    n = column.n_rows or 1
    fig = go.Figure(go.Bar(
        y=y, x=[k for _, k, _ in items], orientation="h",
        marker=dict(color=[c for _, _, c in items], cornerradius=4),
        customdata=[[_label(v, 80), 100 * k / n] for v, k, _ in items],
        hovertemplate="%{customdata[0]}<br>%{x:,} rows (%{customdata[1]:.1f}%)<extra></extra>",
        text=[f"{k:,}" for _, k, _ in items], textposition="outside",
        textfont=dict(color=theme.TEXT_SECONDARY),
    ))
    fig.update_layout(
        showlegend=False,
        xaxis=dict(title="Rows", rangemode="tozero"),
        yaxis=dict(tickvals=y, ticktext=[_label(v) for v, _, _ in items]),
    )
    return fig, 80 + 28 * max(len(items), 3)
