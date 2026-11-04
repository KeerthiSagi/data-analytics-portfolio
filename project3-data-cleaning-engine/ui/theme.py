"""Midnight-blue design tokens and page CSS, shared with ATLAS and CAUSE.

The native theme lives in .streamlit/config.toml. The CSS here only adds
what config cannot express (cards, typography, spacing) and repeats the
core colors so the look holds even if another config takes precedence.
The Plotly template matches CAUSE's, so charts look the same across projects.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

# ------------------------------------------------------------------
# Tokens (identical values to ATLAS utils/palette.py and CAUSE ui/theme.py)
# ------------------------------------------------------------------

BACKGROUND = "#080F20"
SIDEBAR = "#0D1930"
CARD = "#142440"
CARD_RAISED = "#1A2D4F"
BORDER = "#253B5B"
ACCENT = "#4F8CFF"
ACCENT_HOVER = "#7FAEFF"
POSITIVE = "#34D399"
NEGATIVE = "#FB7185"
WARNING = "#FBBF24"
TEXT = "#F1F5F9"
TEXT_SECONDARY = "#94A3B8"
TEXT_MUTED = "#7C8DA6"
GRID = "rgba(148, 163, 184, 0.12)"
NEUTRAL = "#64748B"        # "Missing" bars and other non-data marks

CATEGORICAL = [ACCENT, "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

FONT_STACK = (
    '"Inter", "Source Sans 3", -apple-system, BlinkMacSystemFont, "Segoe UI", '
    "Roboto, Helvetica, Arial, sans-serif"
)

# ------------------------------------------------------------------
# Plotly (same template as CAUSE ui/theme.py)
# ------------------------------------------------------------------

TEMPLATE_NAME = "refine_dark"


def _build_template() -> go.layout.Template:
    axis = dict(
        gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER, tickcolor=BORDER,
        tickfont=dict(color=TEXT_SECONDARY, size=12),
        title=dict(font=dict(color=TEXT_SECONDARY, size=12)),
        automargin=True,
    )
    return go.layout.Template(layout=dict(
        paper_bgcolor=CARD,
        plot_bgcolor=CARD,
        font=dict(family=FONT_STACK, color=TEXT, size=13),
        title=dict(font=dict(size=15, color=TEXT), x=0, xanchor="left"),
        colorway=CATEGORICAL,
        xaxis={**axis, "showgrid": True},
        yaxis={**axis, "showgrid": False},
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT_SECONDARY, size=12),
                    orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hoverlabel=dict(bgcolor=SIDEBAR, bordercolor=BORDER,
                        font=dict(color=TEXT, family=FONT_STACK, size=12)),
        hovermode="closest",
        margin=dict(l=8, r=8, t=40, b=8),
        bargap=0.35,
    ))


pio.templates[TEMPLATE_NAME] = _build_template()


def style_figure(fig: go.Figure, height: int = 360) -> go.Figure:
    """Apply the template. Backgrounds are also set on the figure itself,
    because Streamlit fills any background the figure leaves unset."""
    fig.update_layout(template=TEMPLATE_NAME, height=height, paper_bgcolor=CARD, plot_bgcolor=CARD)
    return fig


PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"],
}

# ------------------------------------------------------------------
# Page CSS
# ------------------------------------------------------------------

CSS = f"""
<style>
:root {{
  --refine-bg: {BACKGROUND};
  --refine-sidebar: {SIDEBAR};
  --refine-card: {CARD};
  --refine-card-raised: {CARD_RAISED};
  --refine-border: {BORDER};
  --refine-accent: {ACCENT};
  --refine-positive: {POSITIVE};
  --refine-negative: {NEGATIVE};
  --refine-warning: {WARNING};
  --refine-text: {TEXT};
  --refine-text-2: {TEXT_SECONDARY};
  --refine-text-3: {TEXT_MUTED};
}}

/* ---------- Surfaces ---------- */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {{
  background: var(--refine-bg);
  color: var(--refine-text);
}}
[data-testid="stSidebar"] {{
  background: var(--refine-sidebar);
  border-right: 1px solid var(--refine-border);
}}
.block-container {{
  padding-top: 3.5rem;
  padding-bottom: 3rem;
  max-width: 1400px;
}}

/* ---------- Typography ---------- */
h1, h2, h3, h4 {{ color: var(--refine-text); letter-spacing: -0.01em; }}
[data-testid="stCaptionContainer"], .stCaption {{ color: var(--refine-text-2); }}

/* ---------- Section cards (containers created by components.section) ---------- */
[class*="st-key-refine-section"] {{
  background: var(--refine-card);
  border-color: var(--refine-border) !important;
  border-radius: 12px;
  padding: 1.1rem 1.2rem 0.6rem 1.2rem;
}}

/* ---------- Inputs (backup for the native theme) ---------- */
[data-baseweb="select"] > div, [data-baseweb="input"] > div, [data-baseweb="base-input"] {{
  background-color: var(--refine-card);
  border-color: var(--refine-border);
}}
[data-baseweb="popover"] ul, [data-baseweb="menu"] {{
  background-color: var(--refine-card-raised);
}}
[data-testid="stFileUploaderDropzone"] {{
  background: var(--refine-card);
  border: 1px dashed var(--refine-border);
}}

/* ---------- Expander ---------- */
[data-testid="stExpander"] details {{
  background: var(--refine-card);
  border-color: var(--refine-border);
}}

/* ---------- Custom components ---------- */
.refine-eyebrow {{
  color: var(--refine-accent);
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 0.25rem;
}}
.refine-title {{
  font-size: 1.85rem;
  font-weight: 700;
  line-height: 1.2;
  margin: 0 0 0.35rem 0;
  color: var(--refine-text);
}}
.refine-subtitle {{
  color: var(--refine-text-2);
  font-size: 0.98rem;
  max-width: 62rem;
  margin: 0;
}}
.refine-header {{ margin-bottom: 1.25rem; }}

.refine-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 0.9rem;
  margin: 0.25rem 0 1rem 0;
}}
.refine-card {{
  background: var(--refine-card);
  border: 1px solid var(--refine-border);
  border-radius: 12px;
  padding: 1rem 1.1rem;
  min-width: 0;
}}
.refine-kpi-label {{
  color: var(--refine-text-2);
  font-size: 0.8rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}}
.refine-kpi-value {{
  color: var(--refine-text);
  font-size: 1.6rem;
  font-weight: 700;
  line-height: 1.25;
  margin-top: 0.3rem;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}}
.refine-kpi-foot {{
  color: var(--refine-text-3);
  font-size: 0.78rem;
  margin-top: 0.3rem;
  overflow-wrap: anywhere;
}}

.refine-callout {{
  background: var(--refine-card);
  border: 1px solid var(--refine-border);
  border-left: 3px solid var(--refine-accent);
  border-radius: 10px;
  padding: 0.85rem 1rem;
  margin: 0.5rem 0;
}}
.refine-callout.positive {{ border-left-color: var(--refine-positive); }}
.refine-callout.negative {{ border-left-color: var(--refine-negative); }}
.refine-callout.caution  {{ border-left-color: var(--refine-warning); }}
.refine-callout-title {{ font-weight: 600; color: var(--refine-text); margin-bottom: 0.2rem; }}
.refine-callout-body  {{ color: var(--refine-text-2); font-size: 0.93rem; line-height: 1.5; }}

.refine-badge {{
  display: inline-block;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  padding: 0.08rem 0.45rem;
  border-radius: 999px;
  margin-right: 0.45rem;
  vertical-align: 0.08rem;
}}
.refine-badge.info    {{ background: rgba(79, 140, 255, 0.16); color: var(--refine-accent); }}
.refine-badge.warning {{ background: rgba(251, 191, 36, 0.14); color: var(--refine-warning); }}
.refine-badge.high    {{ background: rgba(251, 113, 133, 0.16); color: var(--refine-negative); }}
.refine-badge.medium  {{ background: rgba(251, 191, 36, 0.14); color: var(--refine-warning); }}
.refine-badge.low     {{ background: rgba(148, 163, 184, 0.16); color: var(--refine-text-2); }}
.refine-badge.assess  {{ background: rgba(124, 141, 166, 0.14); color: var(--refine-text-3); border: 1px dashed var(--refine-border); }}

.refine-sev-card .refine-kpi-label::before {{
  content: ""; display: inline-block; width: 0.55rem; height: 0.55rem; border-radius: 2px;
  margin-right: 0.4rem; vertical-align: 0.05rem; background: var(--refine-text-3);
}}
.refine-sev-card.high   .refine-kpi-label::before {{ background: var(--refine-negative); }}
.refine-sev-card.medium .refine-kpi-label::before {{ background: var(--refine-warning); }}
.refine-sev-card.low    .refine-kpi-label::before {{ background: var(--refine-text-2); }}
.refine-sev-card.assess .refine-kpi-label::before {{ background: transparent; border: 1px dashed var(--refine-text-3); }}

.refine-examples {{
  color: var(--refine-text-3); font-size: 0.82rem; margin-top: 0.35rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere;
}}
.refine-action {{ color: var(--refine-text-2); font-size: 0.86rem; margin-top: 0.3rem; }}

.refine-empty {{
  background: var(--refine-card);
  border: 1px dashed var(--refine-border);
  border-radius: 12px;
  padding: 2rem 1.5rem;
  text-align: center;
}}
.refine-empty-title {{ font-weight: 600; font-size: 1.05rem; color: var(--refine-text); }}
.refine-empty-body  {{ color: var(--refine-text-2); margin-top: 0.35rem; font-size: 0.93rem; }}

.refine-brand {{
  display: flex; align-items: baseline; gap: 0.5rem;
  margin: 0.1rem 0 0.2rem 0;
}}
.refine-brand-name {{ font-weight: 800; font-size: 1.15rem; letter-spacing: 0.12em; color: var(--refine-text); }}
.refine-brand-tag {{ color: var(--refine-text-2); font-size: 0.78rem; }}
.refine-side-label {{
  color: var(--refine-text-2);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin: 0.9rem 0 0.25rem 0;
}}
.refine-side-value {{ color: var(--refine-text); font-size: 0.88rem; font-weight: 600; overflow-wrap: anywhere; }}
.refine-side-meta {{ color: var(--refine-text-3); font-size: 0.76rem; line-height: 1.45; }}

@media (max-width: 640px) {{
  .refine-title {{ font-size: 1.45rem; }}
  .refine-kpi-value {{ font-size: 1.35rem; }}
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
}}
</style>
"""
