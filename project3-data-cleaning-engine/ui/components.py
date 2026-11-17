"""Reusable UI building blocks. All text that comes from data is HTML-escaped."""

from __future__ import annotations

import re
from contextlib import contextmanager
from html import escape

import streamlit as st

from refine.source import LoadNote
from ui.state import pop_flash

TONES = {"positive", "negative", "neutral", "caution"}


def _html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Headers and sections
# ------------------------------------------------------------------

def page_header(title: str, subtitle: str = "", eyebrow: str = "REFINE") -> None:
    _html(
        '<div class="refine-header">'
        f'<div class="refine-eyebrow">{escape(eyebrow)}</div>'
        f'<h1 class="refine-title">{escape(title)}</h1>'
        + (f'<p class="refine-subtitle">{escape(subtitle)}</p>' if subtitle else "")
        + "</div>"
    )


@contextmanager
def section(title: str, caption: str | None = None, key: str | None = None):
    """A bordered card section. Use as `with section("Title"): ...`."""
    slug = key or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    with st.container(border=True, key=f"refine-section-{slug}"):
        st.markdown(f"#### {title}")
        if caption:
            st.caption(caption)
        yield


# ------------------------------------------------------------------
# Cards and messages
# ------------------------------------------------------------------

def stat_card_html(label: str, value: str, footnote: str | None = None) -> str:
    parts = [
        '<div class="refine-card">',
        f'<div class="refine-kpi-label">{escape(label)}</div>',
        f'<div class="refine-kpi-value">{escape(value)}</div>',
    ]
    if footnote:
        parts.append(f'<div class="refine-kpi-foot">{escape(footnote)}</div>')
    parts.append("</div>")
    return "".join(parts)


def stat_row(cards: list[str]) -> None:
    _html('<div class="refine-grid">' + "".join(cards) + "</div>")


def callout(title: str, body: str, tone: str = "neutral") -> None:
    tone = tone if tone in TONES else "neutral"
    _html(
        f'<div class="refine-callout {tone}">'
        f'<div class="refine-callout-title">{escape(title)}</div>'
        f'<div class="refine-callout-body">{escape(body)}</div></div>'
    )


def empty_state(title: str, body: str) -> None:
    _html(
        '<div class="refine-empty">'
        f'<div class="refine-empty-title">{escape(title)}</div>'
        f'<div class="refine-empty-body">{escape(body)}</div></div>'
    )


NOTE_LABELS = {"warning": "Check", "info": "Note"}


def load_note(note: LoadNote) -> None:
    """One load note as a callout with a text badge (never color alone)."""
    tone = "caution" if note.severity == "warning" else "neutral"
    _html(
        f'<div class="refine-callout {tone}">'
        f'<div class="refine-callout-body"><span class="refine-badge {note.severity}">'
        f"{NOTE_LABELS[note.severity]}</span>{escape(note.message)}</div></div>"
    )


# ------------------------------------------------------------------
# Shared page pieces
# ------------------------------------------------------------------

UPLOAD_PAGE = "views/upload.py"


def no_dataset_state() -> None:
    empty_state(
        "No dataset loaded",
        "Upload a CSV or Excel file, or open the sample dataset, on the Upload page.",
    )
    st.space("small")
    st.page_link(UPLOAD_PAGE, label="Go to Upload", icon=":material/upload_file:")


FLASH_ICONS = {"success": ":material/check_circle:", "error": ":material/error:",
               "warning": ":material/warning:", "info": ":material/info:"}


def show_flash() -> None:
    """Show, once, the result message left by the last button callback."""
    message = pop_flash()
    if message is None:
        return
    kind, text = message
    kind = kind if kind in FLASH_ICONS else "info"
    getattr(st, kind)(text, icon=FLASH_ICONS[kind])


ABOUT = [
    ("Your data stays in this session",
     "Uploaded files are held in memory for your browser session only. They are not written to "
     "disk, stored, or shared with other users, and they are gone when the session ends."),
    ("Nothing changes without your confirmation",
     "Loading a file only reads it. REFINE never trims, converts, fills or removes values on its "
     "own; every change is a step you preview and confirm, recorded in a history you can undo."),
    ("Original file kept",
     "The uploaded file is kept unchanged in memory with its SHA-256 checksum, separate from the "
     "table you work on."),
    ("Rule-based, not AI",
     "REFINE uses explicit, documented rules. It does not use machine learning or AI models."),
]


def about_section() -> None:
    with st.expander("How REFINE handles your data", icon=":material/info:"):
        for title, body in ABOUT:
            st.markdown(f"**{title}.** {body}")


# ------------------------------------------------------------------
# Quality issues
# ------------------------------------------------------------------

SEVERITY_TEXT = {"high": "High", "medium": "Medium", "low": "Low", "assess": "Not assessed"}
SEVERITY_TONE = {"high": "negative", "medium": "caution", "low": "neutral"}


def severity_card_html(severity: str, value: str, footnote: str) -> str:
    """A KPI card whose label carries a severity swatch and its name (never color alone)."""
    return (
        f'<div class="refine-card refine-sev-card {severity}">'
        f'<div class="refine-kpi-label">{escape(SEVERITY_TEXT[severity])}</div>'
        f'<div class="refine-kpi-value">{escape(value)}</div>'
        f'<div class="refine-kpi-foot">{escape(footnote)}</div></div>'
    )


def issue_card(issue) -> None:
    """One QualityIssue as a callout: badge, count with denominator, explanation."""
    pct = issue.percentage
    share = f" ({pct:.1f}%)" if pct is not None else ""
    examples = (
        f'<div class="refine-examples">{escape(" · ".join(issue.examples))}</div>' if issue.examples else ""
    )
    action = (
        f'<div class="refine-action"><b>Suggested:</b> {escape(issue.suggested_action)}</div>'
        if issue.suggested_action else ""
    )
    _html(
        f'<div class="refine-callout {SEVERITY_TONE[issue.severity]}">'
        f'<div class="refine-callout-title"><span class="refine-badge {issue.severity}">'
        f"{SEVERITY_TEXT[issue.severity]}</span>{escape(issue.category)} · {escape(issue.scope)} · "
        f"{issue.affected_count:,} of {issue.denominator:,} {escape(issue.denominator_label)}{share}</div>"
        f'<div class="refine-callout-body">{escape(issue.explanation)}</div>{action}{examples}</div>'
    )


def not_assessed_card(outcome) -> None:
    scope = outcome.column if outcome.column is not None else "Dataset"
    _html(
        '<div class="refine-callout neutral">'
        f'<div class="refine-callout-body"><span class="refine-badge assess">Not assessed</span>'
        f"{escape(outcome.category)} · {escape(scope)}: {escape(outcome.detail)}</div></div>"
    )
