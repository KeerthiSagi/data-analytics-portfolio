"""Data-quality and comparison reports as JSON documents. Read-only; never imports Streamlit.

    quality_report_document(comparison, ...)     -> dict
    comparison_report_document(comparison, ...)  -> dict

Both are built from one DatasetComparison (refine/compare.py), which
holds the profiles and quality reports of the original and current
tables. Nothing is calculated anew: every number is the one the Profile &
Quality and Compare pages show. There is no overall quality score, and a
change is described by its direction (increase, decrease, no change),
never as better or worse.

Neither report contains the tables. They hold counts, statistics, the
quality checks' own examples, up to 5 most frequent values per column,
and up to 5 changed cells per column as examples. Every list that is a
sample says so and gives the full count. The comparison report's
history uses the Compare page's shortened settings; the full settings
are in the history download.

Document formats are versioned (`report_version`); a change to the
structure gets a new version.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from refine.compare import (
    CHANGE_EXAMPLES,
    TOP_VALUES_COMPARED,
    ColumnDetail,
    DatasetComparison,
    HistoryItem,
    MetricChange,
)
from refine.export import JSON_MIME, ExportFile, document_header, export_file_name, json_bytes
from refine.profile import ColumnProfile, DatasetProfile
from refine.quality import CAVEAT, QualityIssue, QualityReport, ValidityRule

QUALITY_REPORT_FORMAT = "refine-quality-report"
COMPARISON_REPORT_FORMAT = "refine-comparison-report"
REPORT_VERSION = 1


# ------------------------------------------------------------------
# Pieces shared by both reports
# ------------------------------------------------------------------

def _metric(m: MetricChange) -> dict[str, Any]:
    return {"key": m.key, "label": m.label, "unit": m.unit, "original": m.original, "current": m.current,
            "difference": m.difference, "direction": m.direction}


def _rule(rule: ValidityRule) -> dict[str, Any]:
    return {"column": rule.column, "kind": rule.kind, "minimum": rule.minimum, "maximum": rule.maximum,
            "allowed": list(rule.allowed), "case_sensitive": rule.case_sensitive, "pattern": rule.pattern,
            "summary": rule.summary}


def _finding(issue: QualityIssue) -> dict[str, Any]:
    return {"check": issue.check, "category": issue.category, "column": issue.column, "severity": issue.severity,
            "affected_count": issue.affected_count, "denominator": issue.denominator,
            "denominator_label": issue.denominator_label, "percentage": issue.percentage,
            "explanation": issue.explanation, "suggested_action": issue.suggested_action,
            "examples": list(issue.examples)}


def _numeric(c: ColumnProfile) -> dict[str, Any] | None:
    s = c.numeric_stats
    if s is None:
        return None
    return {"count": s.count, "minimum": s.minimum, "maximum": s.maximum, "mean": s.mean, "median": s.median,
            "std": s.std, "zeros": s.zeros, "negatives": s.negatives, "all_integral": s.all_integral}


def _column_profile(c: ColumnProfile, types: dict[str, tuple[str, str]], top: int) -> dict[str, Any]:
    dtype, kind = types.get(c.name, (c.storage_dtype, None))
    return {
        "name": c.name, "position": c.position, "dtype": dtype, "kind": kind,
        "type": {"semantic_type": c.inference.semantic_type, "status": c.inference.status,
                 "confidence": c.inference.confidence, "evidence": c.inference.evidence},
        "rows": c.n_rows, "missing": c.missing_count, "missing_pct": c.missing_pct,
        "no_value": c.null_count, "empty_text": c.empty_text_count,
        "null_like_text": c.null_token_count, "whitespace_only": c.whitespace_only_count,
        "distinct": c.distinct_count, "non_missing": c.non_missing_count,
        "numeric": _numeric(c),
        "top_values": [{"value": v.value, "count": v.count, "share": v.share} for v in c.top_values[:top]],
    }


def _dataset(profile: DatasetProfile, quality: QualityReport, types: dict[str, tuple[str, str]],
             top: int) -> dict[str, Any]:
    return {
        "rows": profile.n_rows, "columns": profile.n_columns, "cells": profile.n_cells,
        "missing_cells": profile.missing_cells, "missing_pct": profile.missing_pct,
        "null_like_text_cells": profile.null_token_cells, "whitespace_only_cells": profile.whitespace_only_cells,
        "duplicate_rows": profile.duplicate_rows, "duplicate_groups": profile.duplicate_groups,
        "duplicate_pct": profile.duplicate_pct, "duplicates_not_assessed": profile.duplicate_error,
        "findings_by_severity": quality.severity_counts(),
        "findings": [_finding(i) for i in quality.issues],
        "not_assessed": [{"check": o.check, "category": o.category, "column": o.column, "detail": o.detail}
                         for o in quality.not_assessed],
        "column_profiles": [_column_profile(c, types, top) for c in profile.columns],
    }


def _finding_side(issue: QualityIssue | None) -> dict[str, Any] | None:
    if issue is None:
        return None
    return {"severity": issue.severity, "affected_count": issue.affected_count, "denominator": issue.denominator,
            "denominator_label": issue.denominator_label, "percentage": issue.percentage}


def _column_types(comparison: DatasetComparison) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """(original, current): column name -> (dtype name, kind), as the comparison recorded them."""
    cols = comparison.columns
    original = {c.name: (c.original_dtype, c.original_kind) for c in cols.retained}
    original.update({c.name: (c.dtype, c.kind) for c in cols.removed})
    current = {c.name: (c.current_dtype, c.current_kind) for c in cols.retained}
    current.update({c.name: (c.dtype, c.kind) for c in cols.added})
    return original, current


def _status_counts(comparison: DatasetComparison) -> dict[str, int]:
    counts = {"new": 0, "no longer detected": 0, "changed": 0, "unchanged": 0}
    for i in comparison.quality.issues:
        counts[i.status] += 1
    return counts


# ------------------------------------------------------------------
# Data-quality report
# ------------------------------------------------------------------

def quality_report_document(comparison: DatasetComparison, *, state: dict[str, Any],
                            source: dict[str, Any] | None = None, exported_at: str | None = None) -> dict[str, Any]:
    """Profile and quality findings of the original and current tables, and how the measures moved.

    `state` is refine.export.state_summary(session).
    """
    q = comparison.quality
    original_types, current_types = _column_types(comparison)
    return {
        **document_header(QUALITY_REPORT_FORMAT, "report_version", REPORT_VERSION, exported_at=exported_at),
        "source": source,
        "state": state,
        "validity_rules": [_rule(r) for r in q.rules],
        "original": _dataset(comparison.original_profile, comparison.original_quality, original_types,
                             TOP_VALUES_COMPARED),
        "current": _dataset(comparison.current_profile, comparison.current_quality, current_types,
                            TOP_VALUES_COMPARED),
        "changes": {
            "overview": [_metric(m) for m in comparison.overview],
            "findings_by_status": _status_counts(comparison),
        },
        "definitions": {
            "missing": "No value, or empty text. Null-like text (NA, null, -) and whitespace-only text are "
                       "counted separately and are not missing.",
            "duplicate_rows": "Rows that repeat an earlier row exactly in every column; the first occurrence is "
                              "not counted.",
            "findings": "Rule-based checks. Each finding gives its count and what it is counted out of. There is "
                        "no overall quality score.",
            "direction": "increase, decrease or no change. A change is not judged better or worse.",
            "top_values": f"Up to {TOP_VALUES_COMPARED} most frequent values per column.",
        },
        "caveat": CAVEAT,
    }


def quality_report_export(comparison: DatasetComparison, *, state: dict[str, Any], source_name: str | None = None,
                          **kwargs: Any) -> ExportFile:
    doc = quality_report_document(comparison, state=state, **kwargs)
    return ExportFile(json_bytes(doc), export_file_name(source_name, "quality_report", "json"), JSON_MIME)


# ------------------------------------------------------------------
# Comparison report
# ------------------------------------------------------------------

def _column_statistics(d: ColumnDetail) -> dict[str, Any]:
    before, after = d.top_values()
    return {
        "name": d.name, "status": d.status,
        "numeric": [_metric(m) for m in d.numeric_changes()] if d.has_numeric else None,
        "values": [_metric(m) for m in d.categorical_changes()],
        "top_values": {"original": [{"value": v.value, "count": v.count} for v in before],
                       "current": [{"value": v.value, "count": v.count} for v in after]},
    }


def _cells(comparison: DatasetComparison) -> dict[str, Any]:
    cells = comparison.cells
    doc: dict[str, Any] = {
        "available": cells.available, "unavailable_reason": cells.reason,
        "matched_by": "row lineage (each row's position in the original table)" if cells.available else None,
        "original_rows": cells.original_rows, "current_rows": cells.current_rows,
    }
    if not cells.available:
        return doc
    doc.update({
        "rows_kept": cells.rows_kept, "rows_removed": cells.rows_removed, "rows_added": cells.rows_added,
        "row_order_preserved": cells.order_preserved,
        "cells_compared": cells.cells_compared, "cells_changed": cells.cells_changed,
        "examples_per_column": CHANGE_EXAMPLES,
        "columns": [{
            "name": c.name, "compared_as": c.compared_as, "cells": c.cells, "unchanged": c.unchanged,
            "modified": c.modified, "filled": c.filled, "made_missing": c.made_missing, "changed": c.changed,
            "examples": [{"original_row": e.original_row, "before": e.before, "after": e.after} for e in c.examples],
            "examples_complete": len(c.examples) == c.changed,
        } for c in cells.columns],
    })
    return doc


def _limitations(comparison: DatasetComparison) -> list[str]:
    notes = [
        "Columns are matched by name.",
        "Duplicate counts are exact duplicates on all columns. Key-column or normalized matching, as on the Clean "
        "page, can find more.",
        CAVEAT,
    ]
    cells = comparison.cells
    if not cells.available:
        notes.insert(0, f"No cell-level comparison: {cells.reason} Only aggregate measures are reported.")
    else:
        notes.insert(0, f"Changed-cell examples are the first {CHANGE_EXAMPLES} per column in table order; the "
                        "counts cover every cell. Examples are shown as on the Compare page: text in quotes, "
                        "(no value) for a missing cell, long text shortened.")
        if any(c.compared_as == "text" for c in cells.columns):
            notes.append("Cells of columns whose type changed are compared as REFINE writes them as text, so "
                         "\"42\" and the number 42 count as unchanged.")
    return notes


def comparison_report_document(comparison: DatasetComparison, *, state: dict[str, Any],
                               history: Sequence[HistoryItem] = (), source: dict[str, Any] | None = None,
                               exported_at: str | None = None) -> dict[str, Any]:
    """Everything the Compare page shows, as one JSON document."""
    cols = comparison.columns
    d = comparison.duplicates
    q = comparison.quality
    return {
        **document_header(COMPARISON_REPORT_FORMAT, "report_version", REPORT_VERSION, exported_at=exported_at),
        "source": source,
        "state": state,
        "identical": comparison.identical,
        "overview": [_metric(m) for m in comparison.overview],
        "missing_values": [{
            "column": m.name, "status": m.status, "original_missing": m.original_missing,
            "original_pct": m.original_pct, "current_missing": m.current_missing, "current_pct": m.current_pct,
            "difference": m.difference, "direction": m.direction,
        } for m in comparison.missing],
        "duplicates": {
            "original_rows": d.original_rows, "current_rows": d.current_rows,
            "original_duplicates": d.original_duplicates, "current_duplicates": d.current_duplicates,
            "original_pct": d.original_pct, "current_pct": d.current_pct,
            "original_groups": d.original_groups, "current_groups": d.current_groups,
            "difference": d.difference, "direction": d.direction,
            "original_not_assessed": d.original_error, "current_not_assessed": d.current_error,
        },
        "columns": {
            "retained": [{"name": c.name, "original_dtype": c.original_dtype, "current_dtype": c.current_dtype,
                          "original_kind": c.original_kind, "current_kind": c.current_kind,
                          "original_position": c.original_position, "current_position": c.current_position,
                          "type_changed": c.changed} for c in cols.retained],
            "added": [{"name": c.name, "dtype": c.dtype, "kind": c.kind, "position": c.position} for c in cols.added],
            "removed": [{"name": c.name, "dtype": c.dtype, "kind": c.kind, "position": c.position}
                        for c in cols.removed],
            "type_changed": [c.name for c in cols.type_changed],
            "order_changed": cols.order_changed,
        },
        "quality": {
            "validity_rules": [_rule(r) for r in q.rules],
            "original_by_severity": q.original_severity, "current_by_severity": q.current_severity,
            "original_not_assessed": q.original_not_assessed, "current_not_assessed": q.current_not_assessed,
            "findings_by_status": _status_counts(comparison),
            "findings": [{"check": i.check, "category": i.category, "column": i.column, "status": i.status,
                          "original": _finding_side(i.original), "current": _finding_side(i.current)}
                         for i in q.issues],
        },
        "cells": _cells(comparison),
        "column_statistics": [_column_statistics(x) for x in comparison.details],
        "history": [{
            "step": h.number, "operation": h.operation, "label": h.label, "summary": h.summary,
            "columns": list(h.columns), "settings": dict(h.settings), "counts": dict(h.counts),
            "rows_before": h.rows_before, "rows_after": h.rows_after,
            "columns_before": h.columns_before, "columns_after": h.columns_after,
        } for h in history],
        "limitations": _limitations(comparison),
    }


def comparison_report_export(comparison: DatasetComparison, *, state: dict[str, Any],
                             source_name: str | None = None, **kwargs: Any) -> ExportFile:
    doc = comparison_report_document(comparison, state=state, **kwargs)
    return ExportFile(json_bytes(doc), export_file_name(source_name, "comparison_report", "json"), JSON_MIME)


__all__ = [
    "COMPARISON_REPORT_FORMAT", "QUALITY_REPORT_FORMAT", "REPORT_VERSION", "comparison_report_document",
    "comparison_report_export", "quality_report_document", "quality_report_export",
]
