"""Export page. Placeholder until its phase is built."""

from __future__ import annotations

from ui.components import no_dataset_state, page_header, planned_page
from ui.state import get_dataset


def render() -> None:
    page_header(
        "Export",
        "Download the cleaned data, the step history and a data quality report.",
    )
    if get_dataset() is None:
        no_dataset_state()
        return
    planned_page(
        "A later phase adds downloads of the cleaned data (CSV, XLSX), the audit log, a reusable recipe and Markdown/HTML quality reports."
    )


render()
