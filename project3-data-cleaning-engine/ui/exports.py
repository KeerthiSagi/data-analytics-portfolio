"""The Export page's downloads: names, types and the functions that build them.

Each download is built only when its button is clicked. Streamlit runs
the builder in a separate thread, after the page was drawn, so builders
never touch st.session_state: everything they need is captured here when
the page is drawn. Nothing is cached, so a file always reflects the data
at the moment of the click, and no file's bytes stay in the session.

A builder is bound to the session state the page showed (its version,
which changes on every apply, undo and reset). If the state changed in
between, the builder refuses rather than return a file that does not
match the page; the page has already been redrawn by then.

Builders only read: CleaningSession.data, recipe() and the audit log are
copies, and the reports are made from the cached comparison or a fresh
read-only one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from refine.compare import DatasetComparison, history_summary
from refine.export import (
    CSV_MIME,
    JSON_MIME,
    XLSX_MIME,
    csv_bytes,
    export_file_name,
    history_document,
    json_bytes,
    recipe_bytes,
    state_summary,
)
from refine.quality import ValidityRule
from refine.reports import comparison_report_document, quality_report_document
from refine.session import CleaningSession
from refine.source import LoadedDataset
from refine.xlsx_writer import write_xlsx


class StaleExport(RuntimeError):
    """The data changed after the Export page was drawn."""


@dataclass(frozen=True)
class Download:
    key: str
    label: str
    file_name: str
    mime: str
    build: Callable[[], bytes]


def _bound(session: CleaningSession, version: int, build: Callable[[], bytes]) -> Callable[[], bytes]:
    def run() -> bytes:
        if session.version != version:
            raise StaleExport("The data changed after the Export page was drawn. Download again.")
        data = build()
        if session.version != version:          # Changed while building
            raise StaleExport("The data changed while the file was being made. Download again.")
        return data
    return run


class ComparisonSlot:
    """One comparison for the reports: the cached one, or computed once on first use.

    The Export page keeps a slot across reruns while the data and rules are
    unchanged (ui/state.export_comparison_slot), so a comparison built by
    one report download is reused by the other and by later draws. It is
    filled from the download thread, so it holds its own lock and never
    touches st.session_state.
    """

    def __init__(self, session: CleaningSession, rules: tuple[ValidityRule, ...],
                 cached: DatasetComparison | None, reports: dict) -> None:
        self._session, self._rules, self._value, self._reports = session, rules, cached, reports
        self._lock = threading.Lock()

    @property
    def value(self) -> DatasetComparison | None:
        """The comparison if it has been computed; never computes one."""
        with self._lock:
            return self._value

    def get(self) -> DatasetComparison:
        with self._lock:
            if self._value is None:
                self._value = self._session.compare(rules=self._rules, **self._reports)
                self._reports = {}            # Kept inside the comparison now
            return self._value


def _source(ds: LoadedDataset) -> dict:
    src = ds.source
    return {"file_name": src.file_name, "file_format": src.file_format, "sha256": src.sha256,
            "rows": src.n_rows, "columns": src.n_columns}


def dataset_downloads(ds: LoadedDataset, session: CleaningSession, *, escape_formulas: bool = False,
                      excel: bool = True) -> list[Download]:
    """CSV and (when the table fits in a worksheet) Excel downloads of the current data."""
    version = session.version
    part = "cleaned" if session.steps else "original"
    name = ds.source.file_name
    downloads = [Download(
        "refine_export_csv", "Download CSV", export_file_name(name, part, "csv"), CSV_MIME,
        _bound(session, version, lambda: csv_bytes(session.data, escape_formulas=escape_formulas)[0]),
    )]
    if excel:
        downloads.append(Download(
            "refine_export_xlsx", "Download Excel", export_file_name(name, part, "xlsx"), XLSX_MIME,
            _bound(session, version, lambda: write_xlsx(session.data)),
        ))
    return downloads


def record_downloads(ds: LoadedDataset, session: CleaningSession) -> list[Download]:
    """The cleaning history and the recipe."""
    version = session.version
    name = ds.source.file_name
    return [
        Download("refine_export_history", "Download history", export_file_name(name, "history", "json"), JSON_MIME,
                 _bound(session, version, lambda: json_bytes(history_document(session, source=ds.source)))),
        Download("refine_export_recipe", "Download recipe", export_file_name(name, "recipe", "json"), JSON_MIME,
                 _bound(session, version, lambda: recipe_bytes(session))),
    ]


def report_downloads(ds: LoadedDataset, session: CleaningSession, rules: tuple[ValidityRule, ...], *,
                     cached: DatasetComparison | None = None, reports: dict | None = None,
                     slot: ComparisonSlot | None = None) -> list[Download]:
    """The data-quality and comparison reports, from the same comparison as the Compare page.

    `cached` is the Compare page's comparison if it matches the current
    data and rules, `reports` the cached profiles and quality reports to
    build a new one from; both are read from the session when the page
    is drawn. `slot`, when given, is used instead of both: a ComparisonSlot
    the page keeps for this exact state.
    """
    version = session.version
    name = ds.source.file_name
    comparison = slot if slot is not None else ComparisonSlot(session, tuple(rules), cached, dict(reports or {}))

    def quality() -> bytes:
        return json_bytes(quality_report_document(comparison.get(), state=state_summary(session), source=_source(ds)))

    def compare() -> bytes:
        c = comparison.get()
        return json_bytes(comparison_report_document(
            c, state=state_summary(session), source=_source(ds),
            history=history_summary(session.steps, session.audit_log)))

    return [
        Download("refine_export_quality", "Download quality report",
                 export_file_name(name, "quality_report", "json"), JSON_MIME, _bound(session, version, quality)),
        Download("refine_export_comparison", "Download comparison report",
                 export_file_name(name, "comparison_report", "json"), JSON_MIME, _bound(session, version, compare)),
    ]


__all__ = ["ComparisonSlot", "Download", "StaleExport", "dataset_downloads", "record_downloads", "report_downloads"]
