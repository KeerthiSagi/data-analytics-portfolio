"""Compare page: the original data as loaded next to the current data. Read-only.

The page only reads the cleaning session. It has no buttons that change
data; its widgets are filters for what is shown. Definitions come from
the profile and quality checks (see refine/compare.py), so the numbers
match the Profile & Quality page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.compare import (
    ColumnDetail,
    DatasetComparison,
    HistoryItem,
    MetricChange,
    history_summary,
)
from refine.conversion import KIND_LABELS
from refine.notes import plural
from refine.quality import CAVEAT
from ui.components import (
    callout,
    empty_state,
    no_dataset_state,
    page_header,
    section,
    stat_card_html,
    stat_row,
)
from ui.state import get_comparison, get_dataset, get_session, pending_preview

CLEAN_PAGE = "views/clean.py"
HISTORY_PAGE = "views/history.py"
NA = "n/a"


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def _fmt(x: float | None, unit: str = "count") -> str:
    if x is None:
        return NA
    if unit == "percent":
        return f"{x:.1f}%"
    if unit == "count":
        return f"{int(x):,}"
    x = float(x)
    if x.is_integer() and abs(x) < 1e15:
        return f"{x:,.0f}"
    return f"{x:,.4g}" if abs(x) < 1e6 else f"{x:,.0f}"


def _change(x: float | None, unit: str = "count") -> str:
    """Signed difference with its direction in words; never 'better' or 'worse'."""
    if x is None:
        return NA
    if x == 0:
        return "No change"
    word = "increase" if x > 0 else "decrease"
    sign = "+" if x > 0 else "−"
    if unit == "percent":
        return f"{sign}{abs(x):.1f} points ({word})"
    return f"{sign}{_fmt(abs(x), unit)} ({word})"


def _metric_card(m: MetricChange, foot: str | None = None) -> str:
    return stat_card_html(m.label, f"{_fmt(m.original, m.unit)} → {_fmt(m.current, m.unit)}",
                          foot or _change(m.difference, m.unit))


def _share(count: int | None, pct: float | None) -> str:
    if count is None:
        return "Not in table"
    return f"{count:,} ({pct:.1f}%)"


def _names(columns: tuple[str, ...], limit: int = 5) -> str:
    if not columns:
        return NA
    more = f", and {len(columns) - limit} more" if len(columns) > limit else ""
    return ", ".join(columns[:limit]) + more


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


# ------------------------------------------------------------------
# Sections
# ------------------------------------------------------------------

def _overview(c: DatasetComparison) -> None:
    with section("Dataset overview", key="compare-overview",
                 caption="Original: the table as loaded. Current: after the applied cleaning steps. "
                         "Changes are stated as increases or decreases; whether a change is wanted depends "
                         "on your data."):
        stat_row([_metric_card(c.metric(k)) for k in ("rows", "columns", "missing_cells", "duplicate_rows")])
        frame = pd.DataFrame([{
            "Metric": m.label, "Original": _fmt(m.original, m.unit), "Current": _fmt(m.current, m.unit),
            "Change": _change(m.difference, m.unit),
        } for m in c.overview])
        st.dataframe(frame, hide_index=True, width="stretch")
        st.caption("Column types are storage types. Every loaded column is stored as text until it is converted "
                   "on the Clean page.")


def _missing(c: DatasetComparison) -> None:
    with section("Missing values", key="compare-missing",
                 caption="Missing means no value or empty text, as on the Profile & Quality page. Each "
                         "percentage is out of that table's own row count."):
        only_changed = st.toggle("Only columns whose missing count changed", value=True,
                                 key="refine_cmp_missing_changed")
        rows = [m for m in c.missing if m.changed or not only_changed]
        if not rows:
            empty_state("No change in missing values", "Every column has the same number of missing cells "
                        "in the original and current data.")
            return
        st.dataframe(pd.DataFrame([{
            "Column": m.name, "Status": m.status.capitalize(),
            "Original missing": _share(m.original_missing, m.original_pct),
            "Current missing": _share(m.current_missing, m.current_pct),
            "Change": _change(m.difference) if m.status == "retained" else NA,
        } for m in rows]), hide_index=True, width="stretch")


def _duplicates(c: DatasetComparison) -> None:
    d = c.duplicates
    with section("Duplicates", key="compare-duplicates",
                 caption="Exact duplicates, as on the Profile & Quality page: rows that repeat an earlier row "
                         "in every column. The first occurrence is not counted."):
        orig = _share(d.original_duplicates, d.original_pct) if d.original_duplicates is not None else "Not assessed"
        cur = _share(d.current_duplicates, d.current_pct) if d.current_duplicates is not None else "Not assessed"
        stat_row([
            stat_card_html("Original", orig, f"Out of {plural(d.original_rows, 'row')}"),
            stat_card_html("Current", cur, f"Out of {plural(d.current_rows, 'row')}"),
            stat_card_html("Change", _change(d.difference), "Duplicate rows"),
        ])
        for label, error in (("original", d.original_error), ("current", d.current_error)):
            if error:
                st.caption(f"Not assessed for the {label} data: {error}")
        st.caption("Matching on key columns or ignoring case and spaces, as the Clean page can, may find more.")


def _columns(c: DatasetComparison) -> None:
    cols = c.columns
    with section("Columns and data types", key="compare-columns",
                 caption="Columns are matched by name. Types are the stored dtypes; a type change is reported, "
                         "not judged."):
        stat_row([
            stat_card_html("Kept", f"{len(cols.retained):,}", "In both tables"),
            stat_card_html("Added", f"{len(cols.added):,}", "Only in the current data"),
            stat_card_html("Removed", f"{len(cols.removed):,}", "Only in the original data"),
            stat_card_html("Type changed", f"{len(cols.type_changed):,}", "Kept columns with a new dtype"),
        ])
        rows = [{"Column": t.name, "Change": "Type changed", "Original type": f"{t.original_label} ({t.original_dtype})",
                 "Current type": f"{t.current_label} ({t.current_dtype})"} for t in cols.type_changed]
        rows += [{"Column": a.name, "Change": "Added", "Original type": NA,
                  "Current type": f"{_kind(a.kind)} ({a.dtype})"} for a in cols.added]
        rows += [{"Column": r.name, "Change": "Removed", "Original type": f"{_kind(r.kind)} ({r.dtype})",
                  "Current type": NA} for r in cols.removed]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            empty_state("No column changes", "Both tables have the same columns with the same types.")
        if cols.order_changed:
            st.caption("The kept columns are in a different order in the current data.")


def _kind(kind: str) -> str:
    return KIND_LABELS.get(kind, "Other")


def _finding(issue) -> str:
    if issue is None:
        return "Not detected"
    pct = issue.percentage
    share = f" ({pct:.1f}%)" if pct is not None else ""
    return f"{issue.affected_count:,} of {issue.denominator:,} {issue.denominator_label}{share}"


def _quality(c: DatasetComparison) -> None:
    q = c.quality
    with section("Data quality", key="compare-quality",
                 caption="The Profile & Quality checks, run on both tables with the same validity rules. "
                         "Counts of findings by severity; there is no overall score."):
        stat_row([
            stat_card_html(f"{label} severity", f"{q.original_severity[s]:,} → {q.current_severity[s]:,}",
                           _change(q.current_severity[s] - q.original_severity[s]))
            for s, label in (("high", "High"), ("medium", "Medium"), ("low", "Low"))
        ] + [stat_card_html("Not assessed", f"{q.original_not_assessed:,} → {q.current_not_assessed:,}",
                            "Checks that could not run reliably")])
        include_all = st.toggle("Include findings that did not change", value=False, key="refine_cmp_quality_all")
        rows = [i for i in q.issues if include_all or i.status != "unchanged"]
        if rows:
            st.dataframe(pd.DataFrame([{
                "Category": i.category, "Column": i.scope, "Original": _finding(i.original),
                "Current": _finding(i.current), "Status": i.status.capitalize(),
            } for i in rows]), hide_index=True, width="stretch")
        else:
            empty_state("No change in quality findings", "Every check reports the same result for both tables.")
        st.caption("\"No longer detected\" means the check no longer finds the problem in the current data. "
                   + CAVEAT)


def _cells(c: DatasetComparison) -> None:
    cells = c.cells
    with section("Value changes", key="compare-cells",
                 caption="Cell by cell, for columns in both tables. Rows are matched by their identity in the "
                         "original table, never by position, so removed rows are not counted as changed cells."):
        if not cells.available:
            callout("Cell-level comparison unavailable", cells.reason or "Rows cannot be matched.", "caution")
            st.caption("The comparisons above use whole-table and per-column counts, which do not need matched rows.")
            return
        stat_row([
            stat_card_html("Rows kept", f"{cells.rows_kept:,}", f"Of {plural(cells.original_rows, 'original row')}"),
            stat_card_html("Rows removed", f"{cells.rows_removed:,}", "Not counted as cell changes"),
            stat_card_html("Cells compared", f"{cells.cells_compared:,}", "Kept rows × kept columns"),
            stat_card_html("Cells changed", f"{cells.cells_changed:,}", "Modified, filled or made missing"),
        ])
        only_changed = st.toggle("Only columns with changed cells", value=True, key="refine_cmp_cells_changed")
        rows = [x for x in cells.columns if x.changed or not only_changed]
        if rows:
            st.dataframe(pd.DataFrame([{
                "Column": x.name, "Modified": x.modified, "Filled": x.filled, "Made missing": x.made_missing,
                "Unchanged": x.unchanged, "Compared as": "Text (type changed)" if x.compared_as == "text" else "Values",
            } for x in rows]), hide_index=True, width="stretch")
        else:
            empty_state("No cell changed", "Every kept cell holds the same value as in the original data.")
        st.caption("Modified: a value replaced by a different value. Filled: missing before, has a value now. "
                   "Made missing: had a value, missing now. When a column's type changed, cells are compared as "
                   "REFINE writes them as text, so \"42\" and the number 42 count as unchanged.")


def _detail_label(d: ColumnDetail) -> str:
    return d.name if d.status == "retained" else f"{d.name} ({d.status})"


def _details(c: DatasetComparison) -> None:
    with section("Column details", key="compare-details",
                 caption="Statistics from the profile of each table, for one column."):
        if not c.details:
            empty_state("No columns", "Neither table has columns.")
            return
        names = [d.name for d in c.details]
        labels = {d.name: _detail_label(d) for d in c.details}
        name = st.selectbox("Column", names, key="refine_cmp_column", format_func=lambda n: labels.get(n, n))
        d = c.detail(name)
        if d.status != "retained":
            st.caption(f"This column was {d.status}, so only one table has statistics for it.")

        if d.has_numeric:
            st.markdown("**Numeric statistics**")
            st.dataframe(pd.DataFrame([{
                "Statistic": m.label, "Original": _fmt(m.original, m.unit), "Current": _fmt(m.current, m.unit),
                "Change": _change(m.difference, m.unit),
            } for m in d.numeric_changes()]), hide_index=True, width="stretch")
            st.caption("Over values stored as numbers, or text that reads as a plain number, as on the Profile & "
                       "Quality page. Standard deviation is n/a with fewer than two values.")
        else:
            st.caption("No numeric statistics: the column holds no values that read as plain numbers.")

        st.markdown("**Values**")
        st.dataframe(pd.DataFrame([{
            "Measure": m.label, "Original": _fmt(m.original), "Current": _fmt(m.current), "Change": _change(m.difference),
        } for m in d.categorical_changes()]), hide_index=True, width="stretch")

        before, after = d.top_values()
        if before or after:
            st.markdown("**Most frequent values**")
            n = max(len(before), len(after))
            st.dataframe(pd.DataFrame([{
                "Rank": i + 1,
                "Original": f"{before[i].value} × {before[i].count:,}" if i < len(before) else "",
                "Current": f"{after[i].value} × {after[i].count:,}" if i < len(after) else "",
            } for i in range(n)]), hide_index=True, width="stretch")

        changes = c.cells.column(name) if c.cells.available else None
        if changes is not None and changes.examples:
            st.markdown("**Examples of changed cells**")
            st.dataframe(pd.DataFrame([{"Original row": e.original_row, "Before": e.before, "After": e.after}
                                       for e in changes.examples]), hide_index=True, width="stretch")
            st.caption(f"The first {len(changes.examples)} of {changes.changed:,} changed cells, in table order.")


def _history(items: tuple[HistoryItem, ...]) -> None:
    with section("Cleaning history", key="compare-history",
                 caption="The applied steps that turned the original data into the current data, in order, "
                         "from the session's records."):
        if not items:
            empty_state("No cleaning steps applied", "The current data is the original data as loaded.")
            st.page_link(CLEAN_PAGE, label="Go to Clean", icon=":material/cleaning_services:")
            return
        st.dataframe(pd.DataFrame([{
            "Step": i.number, "Operation": i.label, "Columns": _names(i.columns),
            "Change": i.summary, "Settings": "; ".join(f"{k}: {v}" for k, v in i.settings) or "Defaults",
            "Recorded counts": ", ".join(f"{_label(k)}: {v:,}" for k, v in i.counts),
            "Rows": f"{i.rows_before:,} → {i.rows_after:,}", "Columns before → after": f"{i.columns_before:,} → {i.columns_after:,}",
        } for i in items]), hide_index=True, width="stretch",
            column_config={"Change": st.column_config.TextColumn(width="large")})
        st.caption("Undone steps are not listed. The History page has the full audit log, undo and the recipe.")
        st.page_link(HISTORY_PAGE, label="Go to History", icon=":material/history:")


def render() -> None:
    page_header("Compare", "The original data as loaded, next to the current data after cleaning. Nothing here "
                           "changes the data.")
    ds = get_dataset()
    if ds is None:
        no_dataset_state()
        return
    session = get_session(ds)
    with st.spinner("Comparing the original and current data…"):
        comparison = get_comparison(ds)        # Cached after the first draw for this data
    preview = pending_preview()
    if preview is not None:
        callout("A previewed step is not included",
                f"\"{preview.plan.label}\" is waiting for confirmation on the Clean page. This comparison shows "
                "the confirmed data without it.", "neutral")
    if not session.steps:
        callout("No cleaning steps applied",
                "The current data is the original data, so every comparison below shows no change. "
                "Apply steps on the Clean page to see their effect here.", "neutral")
    _overview(comparison)
    _missing(comparison)
    _duplicates(comparison)
    _columns(comparison)
    _quality(comparison)
    _cells(comparison)
    _details(comparison)
    _history(history_summary(session.steps, session.audit_log))


render()
