"""Profile & Quality page: what is in the dataset, and which rule-based checks found problems.

Everything here reads the loaded table; nothing is changed. Counts are
always shown with what they are counted out of, and no overall score is
computed.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.notes import plural
from refine.profile import ColumnProfile, DatasetProfile
from refine.quality import CATEGORIES, CAVEAT, SEVERITY_LABELS, QualityReport, ValidityRule
from refine.source import LoadedDataset
from ui import charts
from ui.components import (
    callout,
    issue_card,
    load_note,
    no_dataset_state,
    not_assessed_card,
    page_header,
    section,
    severity_card_html,
    stat_card_html,
    stat_row,
)
from ui.state import get_dataset, get_profile, get_quality, get_rules, get_session, is_sample, set_rules


def _size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:,.1f} KB"
    return f"{n / 1024 / 1024:,.1f} MB"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}%"


# ------------------------------------------------------------------
# Overview
# ------------------------------------------------------------------

def _overview(p: DatasetProfile) -> None:
    dup_value = "Not assessed" if p.duplicate_rows is None else f"{p.duplicate_rows:,}"
    dup_foot = (p.duplicate_error or "") if p.duplicate_rows is None else \
        f"{_pct(p.duplicate_pct)} of rows repeat an earlier row exactly"
    with section("Dataset overview", key="overview",
                 caption=f"Profiled in {p.profile_seconds:.2f} s. All values are stored as text; "
                         "types below are suggestions inferred from the values, not conversions."):
        stat_row([
            stat_card_html("Rows", f"{p.n_rows:,}"),
            stat_card_html("Columns", f"{p.n_columns:,}",
                           f"{plural(len(p.empty_columns), 'empty column')}"),
            stat_card_html("Cells", f"{p.n_cells:,}", "Rows × columns"),
            stat_card_html("Memory", _size(p.memory_bytes), "Parsed table in this session"),
        ])
        stat_row([
            stat_card_html("Missing cells", f"{p.missing_cells:,}",
                           f"{_pct(p.missing_pct)} of cells: no value or empty text"),
            stat_card_html("Null-like text", f"{p.null_token_cells:,}",
                           "Cells such as NA, null or '-'; not counted as missing"),
            stat_card_html("Whitespace-only", f"{p.whitespace_only_cells:,}",
                           "Cells of only spaces; not counted as missing"),
            stat_card_html("Duplicate rows", dup_value, dup_foot),
        ])


# ------------------------------------------------------------------
# Quality summary
# ------------------------------------------------------------------

def _issues_frame(report: QualityReport, severities, categories) -> pd.DataFrame:
    rows = [
        {
            "Severity": SEVERITY_LABELS[i.severity],
            "Category": i.category,
            "Column": i.scope,
            "Affected": i.affected_count,
            "Out of": i.denominator,
            "Counted out of": i.denominator_label,
            "%": i.percentage,
            "Explanation": i.explanation,
            "Suggested action": i.suggested_action or "",
            "Examples": " · ".join(i.examples),
        }
        for i in report.issues
        if SEVERITY_LABELS[i.severity] in severities and i.category in categories
    ]
    return pd.DataFrame(rows, columns=["Severity", "Category", "Column", "Affected", "Out of",
                                       "Counted out of", "%", "Explanation", "Suggested action", "Examples"])


def _quality(report: QualityReport, ds: LoadedDataset) -> None:
    counts = report.severity_counts()
    not_assessed = report.not_assessed
    with section("Quality checks", key="quality",
                 caption="Rule-based checks. Each issue is a count with its denominator; there is "
                         "deliberately no overall quality score."):
        stat_row([
            severity_card_html("high", f"{counts['high']:,}", "Issues likely to change results"),
            severity_card_html("medium", f"{counts['medium']:,}", "Issues worth fixing before analysis"),
            severity_card_html("low", f"{counts['low']:,}", "Consistency and formatting issues"),
            severity_card_html("assess", f"{len(not_assessed):,}", "Checks that could not run reliably"),
        ])
        callout("What these checks can and cannot tell you", CAVEAT, "neutral")

        if not report.issues:
            callout("No issues detected by these checks",
                    "None of the checks below found a problem. This is not a guarantee that the "
                    "data is correct.", "neutral")
        else:
            present = [c for c in CATEGORIES if any(i.category == c for i in report.issues)]
            f1, f2 = st.columns([1, 2], gap="medium")
            with f1:
                severities = st.multiselect("Severity", list(SEVERITY_LABELS.values()),
                                            default=list(SEVERITY_LABELS.values()), key="refine_q_sev")
            with f2:
                categories = st.multiselect("Category", present, default=present, key="refine_q_cat")
            frame = _issues_frame(report, severities, categories)
            st.caption(f"Showing {plural(len(frame), 'issue')} of {len(report.issues):,}, most severe first.")
            st.dataframe(
                frame, hide_index=True, width="stretch", height=min(420, 38 + 35 * max(len(frame), 1)),
                column_config={
                    "Affected": st.column_config.NumberColumn(format="%d"),
                    "Out of": st.column_config.NumberColumn(format="%d"),
                    "%": st.column_config.NumberColumn(format="%.1f%%"),
                    "Explanation": st.column_config.TextColumn(width="large"),
                    "Suggested action": st.column_config.TextColumn(width="medium"),
                },
            )
            dataset_issues = [i for i in report.issues if i.column is None]
            for issue in dataset_issues:
                issue_card(issue)

        if not_assessed:
            with st.expander(f"Not assessed ({len(not_assessed)})", icon=":material/help:"):
                for outcome in not_assessed:
                    not_assessed_card(outcome)
        none = report.none_detected
        with st.expander(f"Checks that found nothing ({len(none)})", icon=":material/check:"):
            st.caption("A check finding nothing means only that this rule did not match; the data may still be wrong in other ways.")
            st.dataframe(
                pd.DataFrame([{"Category": o.category, "Column": o.column or "Dataset", "Result": o.detail}
                              for o in none], columns=["Category", "Column", "Result"]),
                hide_index=True, width="stretch",
            )


# ------------------------------------------------------------------
# Charts
# ------------------------------------------------------------------

def _charts(p: DatasetProfile) -> None:
    left, right = st.columns([3, 2], gap="medium")
    with left:
        with section("Missing and missing-like values", key="missing",
                     caption="Share of rows per column. Null-like text and whitespace-only cells are "
                             "shown separately because they are values in the data, not gaps."):
            cols = charts.missing_columns(p)
            if not cols:
                callout("No gaps found",
                        "No column has missing, null-like or whitespace-only cells.", "neutral")
            else:
                fig, height = charts.missing_chart(p)
                charts.show_chart(fig, height=height, key="refine_missing_chart")
                if len(cols) > charts.MAX_MISSING_BARS:
                    st.caption(f"Showing the {charts.MAX_MISSING_BARS} most affected of {len(cols):,} columns; "
                               "the column profile lists all of them.")
    with right:
        with section("Suggested column types", key="types",
                     caption="Inferred from the values. Nothing has been converted."):
            fig, height = charts.type_chart(p)
            charts.show_chart(fig, height=height, key="refine_type_chart")


# ------------------------------------------------------------------
# Column profile table
# ------------------------------------------------------------------

def _profile_frame(p: DatasetProfile, report: QualityReport) -> pd.DataFrame:
    issue_counts: dict[str, int] = {}
    for i in report.issues:
        if i.column is not None:
            issue_counts[i.column] = issue_counts.get(i.column, 0) + 1
    rows = []
    for c in p.columns:
        top = c.top_values[0] if c.top_values else None
        rows.append({
            "#": c.position,
            "Column": c.name,
            "Storage": c.storage_dtype,
            "Suggested type": c.inference.label,
            "Type": f"{c.inference.status.capitalize()} · {c.inference.confidence}",
            "Missing": c.missing_count,
            "Missing %": c.missing_pct,
            "Null-like": c.null_token_count,
            "Whitespace-only": c.whitespace_only_count,
            "Distinct": c.distinct_count,
            "Distinct %": c.distinct_pct,
            "Edge spaces": c.edge_whitespace_count,
            "Length": f"{c.length.minimum}–{c.length.maximum}" if c.length else "",
            "Most frequent": f"{charts.display_value(top.value)} ({top.count:,})" if top else "",
            "Issues": issue_counts.get(c.name, 0),
        })
    return pd.DataFrame(rows)


def _profile_table(p: DatasetProfile, report: QualityReport) -> None:
    with section("Column profile", key="columns",
                 caption="One row per column. Missing = no value or empty text. Distinct values are "
                         "exact text (\"USA\" and \"usa\" are different) and exclude missing cells."):
        st.dataframe(
            _profile_frame(p, report), hide_index=True, width="stretch",
            height=min(560, 38 + 35 * len(p.columns)),
            column_config={
                "#": st.column_config.NumberColumn(width="small"),
                "Missing %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
                "Distinct %": st.column_config.NumberColumn(format="%.1f%%"),
                "Type": st.column_config.TextColumn(help="Confirmed: the storage type says so. "
                                                    "Inferred: suggested from text values, with a confidence level."),
            },
        )


# ------------------------------------------------------------------
# Column inspection
# ------------------------------------------------------------------

def _details(c: ColumnProfile) -> pd.DataFrame:
    rows: list[tuple[str, str]] = [
        ("Storage dtype", c.storage_dtype),
        ("Values checked", f"{c.values_checked:,} (non-missing, not whitespace-only, not null-like)"),
    ]
    if c.text_values:
        rows += [
            ("Leading whitespace", f"{c.leading_whitespace_count:,}"),
            ("Trailing whitespace", f"{c.trailing_whitespace_count:,}"),
            ("Repeated inner spaces", f"{c.repeated_space_count:,}"),
            ("Parse as plain numbers", f"{c.numeric_plain_count:,}"),
            ("Parse as numbers after removing symbols", f"{c.numeric_formatted_count:,}"),
            ("Valid dates", f"{c.date_count:,}"),
            ("Date-like but not a calendar date", f"{c.date_invalid_count:,}"),
            ("Ambiguous month/day dates", f"{c.date_ambiguous_count:,}"),
            ("Yes/no style values", f"{c.boolean_count:,}"),
            ("Email-like values", f"{c.email_count:,}"),
        ]
        if c.case_styles:
            rows.append(("Case styles", ", ".join(f"{k} {v:,}" for k, v in c.case_styles.items())))
    if c.length:
        rows.append(("Text length", f"min {c.length.minimum}, median {c.length.median:g}, "
                                    f"mean {c.length.mean:.1f}, max {c.length.maximum}"))
    if c.python_types:
        rows.append(("Non-text Python values", ", ".join(c.python_types)))
    return pd.DataFrame(rows, columns=["Indicator", "Value"])


def _fmt_num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.4g}" if abs(x) < 1e6 else f"{x:,.0f}"


def _inspect(p: DatasetProfile, report: QualityReport) -> None:
    with section("Inspect a column", key="inspect"):
        names = [c.name for c in p.columns]
        name = st.selectbox("Column", names, key="refine_inspect_column",
                            format_func=lambda n: f"{p.column(n).position} · {n}")
        c = p.column(name)
        inf = c.inference
        stat_row([
            stat_card_html("Suggested type", inf.label, f"{inf.status.capitalize()}, {inf.confidence} confidence"),
            stat_card_html("Missing", f"{c.missing_count:,}",
                           f"{c.missing_pct:.1f}% · {c.null_count:,} no value, {c.empty_text_count:,} empty text"),
            stat_card_html("Null-like text", f"{c.null_token_count:,}",
                           ", ".join(f"{t.value} ({t.count})" for t in c.null_tokens[:4]) or "None found"),
            stat_card_html("Distinct", f"{c.distinct_count:,}", f"{c.distinct_pct:.1f}% of non-missing values"),
        ])
        st.caption(f"Type evidence: {inf.evidence}")

        left, right = st.columns([3, 2], gap="medium")
        with left:
            st.markdown("**Most frequent values**")
            if c.top_values or c.missing_count:
                fig, height = charts.top_values_chart(c)
                charts.show_chart(fig, height=height, key=f"refine_top_{c.position}")
                shown = sum(v.count for v in c.top_values)
                st.caption(f"Top {len(c.top_values)} of {c.distinct_count:,} distinct values cover "
                           f"{shown:,} of {c.n_rows:,} rows. Grey bar: missing cells.")
            else:
                st.caption("The column has no values.")
        with right:
            st.markdown("**Indicators**")
            st.dataframe(_details(c), hide_index=True, width="stretch")

        if c.numeric_stats:
            s = c.numeric_stats
            st.markdown("**Numeric statistics**")
            st.caption(f"Over the {s.count:,} values that parse as plain numbers. "
                       f"{c.numeric_formatted_count:,} formatted and {c.values_checked - c.numeric_count:,} "
                       "non-numeric values are excluded.")
            stat_row([
                stat_card_html("Min", _fmt_num(s.minimum)),
                stat_card_html("Median", _fmt_num(s.median), f"P25 {_fmt_num(s.p25)} · P75 {_fmt_num(s.p75)}"),
                stat_card_html("Mean", _fmt_num(s.mean), "n/a" if s.std is None else f"Std dev {_fmt_num(s.std)}"),
                stat_card_html("Max", _fmt_num(s.maximum), f"{s.negatives:,} negative · {s.zeros:,} zero"),
            ])
        if c.date_formats:
            st.markdown("**Date formats found**")
            st.dataframe(pd.DataFrame([(f.label, f.count) for f in c.date_formats],
                                      columns=["Format", "Values"]), hide_index=True, width="content")
        if c.case_variant_groups:
            st.markdown("**Same value, different capitalization**")
            st.caption(f"{plural(c.case_variant_group_count, 'group')}; up to 10 shown. "
                       "Spacing is ignored when grouping.")
            st.dataframe(
                pd.DataFrame([{
                    "Most common": charts.display_value(g.variants[0].value),
                    "Variants": " · ".join(f"{charts.display_value(v.value)} ({v.count:,})" for v in g.variants),
                    "Cells differing": g.minority_count,
                } for g in c.case_variant_groups]),
                hide_index=True, width="stretch",
            )

        issues = report.for_column(c.name)
        st.markdown(f"**Issues in this column ({len(issues)})**")
        if not issues:
            st.caption("No check found an issue in this column. That does not prove its values are correct.")
        for issue in issues:
            issue_card(issue)
        for o in report.not_assessed:
            if o.column == c.name:
                not_assessed_card(o)


# ------------------------------------------------------------------
# Validity rules
# ------------------------------------------------------------------

RULE_KINDS = {"range": "Number range", "allowed_values": "Allowed values", "pattern": "Text pattern"}


def _add_rule(ds: LoadedDataset, k: str) -> None:
    state = st.session_state
    column = state.get(f"{k}_col")
    kind = state.get(f"{k}_kind") or "range"
    if not column:
        return
    if kind == "range":
        lo, hi = state.get(f"{k}_min"), state.get(f"{k}_max")
        if lo is None and hi is None:
            state[f"{k}_msg"] = "Enter a minimum, a maximum, or both."
            return
        if lo is not None and hi is not None and lo > hi:
            state[f"{k}_msg"] = "The minimum is larger than the maximum."
            return
        rule = ValidityRule(column, "range", minimum=lo, maximum=hi)
    elif kind == "allowed_values":
        allowed = tuple(v.strip() for v in (state.get(f"{k}_allowed") or "").split(",") if v.strip())
        if not allowed:
            state[f"{k}_msg"] = "List at least one allowed value, separated by commas."
            return
        rule = ValidityRule(column, "allowed_values", allowed=allowed,
                            case_sensitive=not state.get(f"{k}_ignore_case", False))
    else:
        pattern = (state.get(f"{k}_pattern") or "").strip()
        if not pattern:
            state[f"{k}_msg"] = "Enter a pattern."
            return
        rule = ValidityRule(column, "pattern", pattern=pattern)
    state.pop(f"{k}_msg", None)
    rules = get_rules(ds)
    if rule not in rules:
        set_rules(ds, rules + (rule,))


def _remove_rule(ds: LoadedDataset, index: int) -> None:
    rules = list(get_rules(ds))
    if 0 <= index < len(rules):
        rules.pop(index)
        set_rules(ds, tuple(rules))


def _rules(ds: LoadedDataset, p: DatasetProfile) -> None:
    rules = get_rules(ds)
    k = f"refine_rule_{ds.source.sha256[:12]}"
    with st.expander(f"Validity rules ({len(rules)})", icon=":material/rule:"):
        st.caption("Rules you state about valid values. They are checked, never enforced: no value is "
                   "changed. Without rules, value validity is reported as not assessed.")
        if is_sample(ds):
            st.caption("The sample starts with example rules taken from its documentation. Remove or add any.")
        for idx, rule in enumerate(rules):
            a, b = st.columns([6, 1], vertical_alignment="center")
            a.markdown(f"`{RULE_KINDS[rule.kind]}` {rule.summary}")
            b.button("Remove", key=f"{k}_rm_{idx}", on_click=_remove_rule, args=(ds, idx))
        st.divider()
        c1, c2 = st.columns([2, 2])
        c1.selectbox("Column", [c.name for c in p.columns], key=f"{k}_col")
        c2.segmented_control("Rule", list(RULE_KINDS), default="range", key=f"{k}_kind",
                             format_func=lambda x: RULE_KINDS[x])
        kind = st.session_state.get(f"{k}_kind") or "range"
        if kind == "range":
            r1, r2 = st.columns(2)
            r1.number_input("Minimum (optional)", value=None, key=f"{k}_min", format="%g")
            r2.number_input("Maximum (optional)", value=None, key=f"{k}_max", format="%g")
        elif kind == "allowed_values":
            st.text_input("Allowed values, separated by commas", key=f"{k}_allowed")
            st.checkbox("Ignore capitalization and surrounding spaces", key=f"{k}_ignore_case")
        else:
            st.text_input("Pattern the whole value must match (RE2 regular expression)", key=f"{k}_pattern",
                          help="For example [A-Z]-[0-9]{4}. Backreferences and lookarounds are not supported.")
        st.button("Add rule", icon=":material/add:", key=f"{k}_add", on_click=_add_rule, args=(ds, k))
        if st.session_state.get(f"{k}_msg"):
            st.caption(st.session_state[f"{k}_msg"])


# ------------------------------------------------------------------
# Load notes
# ------------------------------------------------------------------

def _notes(ds: LoadedDataset) -> None:
    missing_headers = [name for label, name in zip(ds.source.header_labels, ds.source.columns) if label is None]
    caption = f"{plural(len(ds.notes), 'note')} from loading the file"
    if missing_headers:
        caption += f" · {plural(len(missing_headers), 'column')} without a header in the file"
    with section("Load notes and warnings", key="profile-notes", caption=caption):
        if not ds.notes:
            callout("No parsing adjustments",
                    "The file was read without renaming, padding, skipping or converting anything.", "neutral")
        for note in sorted(ds.notes, key=lambda n: n.severity != "warning"):
            load_note(note)


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------

def render() -> None:
    page_header(
        "Profile & Quality",
        "Understand the dataset before changing it: column types, missing values, duplicates and other quality checks.",
    )
    ds = get_dataset()
    if ds is None:
        no_dataset_state()
        return
    steps = len(get_session(ds).steps)
    if steps:
        callout("Profiling the cleaned data",
                f"These results describe the current data, after {plural(steps, 'applied cleaning step')}. "
                "Undo or reset on the History page to profile earlier data. Load notes still describe the file.",
                "neutral")
    with st.spinner("Profiling the dataset…"):
        profile = get_profile(ds)
        report = get_quality(ds)

    _overview(profile)
    _quality(report, ds)
    _rules(ds, profile)
    _charts(profile)
    _profile_table(profile, report)
    _inspect(profile, report)
    _notes(ds)


render()
