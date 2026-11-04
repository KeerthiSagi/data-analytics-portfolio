"""Compare page. Placeholder until its phase is built."""

from __future__ import annotations

from ui.components import no_dataset_state, page_header, planned_page
from ui.state import get_dataset


def render() -> None:
    page_header(
        "Compare",
        "See how the working table differs from the original file.",
    )
    if get_dataset() is None:
        no_dataset_state()
        return
    planned_page(
        "A later phase adds before-and-after comparisons: rows removed, cells changed per column, type changes and summary statistics."
    )


render()
