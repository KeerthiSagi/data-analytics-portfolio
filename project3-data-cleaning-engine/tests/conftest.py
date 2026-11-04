"""Shared helpers: build CSV and XLSX inputs in memory.

Test inputs are built here rather than stored as binary fixture files, so
every edge case is visible in the test that uses it.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from refine.loader import load_file
from refine.source import LoadOptions

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_csv(text: str | bytes, name: str = "data.csv", **options):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return load_file(name, data, LoadOptions(**options) if options else None)


def make_xlsx(sheets: dict[str, list[list]], hidden: tuple[str, ...] = ()) -> bytes:
    """Workbook with one sheet per entry; each value is a list of rows.

    Values are written as openpyxl types (str, int, float, bool, datetime,
    formulas as '=...'). None leaves a cell empty.
    """
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row, start=1):
                if value is not None:
                    ws.cell(row=r, column=c, value=value)
        if name in hidden:
            ws.sheet_state = "hidden"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def patch_xlsx_part(data: bytes, part: str, old: bytes, new: bytes) -> bytes:
    """Return a copy of the workbook with one XML part edited (to add
    things openpyxl cannot write, such as saved formula results)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            content = src.read(info.filename)
            if info.filename == part:
                assert old in content, f"{old!r} not found in {part}"
                content = content.replace(old, new)
            dst.writestr(info, content)
    return out.getvalue()


def load_xlsx(data: bytes, name: str = "book.xlsx", **options):
    return load_file(name, data, LoadOptions(**options) if options else None)


def codes(dataset) -> list[str]:
    return [n.code for n in dataset.notes]


def column(dataset, name: str) -> list:
    """Column values as Python objects, with pd.NA as None."""
    return [None if pd.isna(v) else v for v in dataset.data[name].tolist()]


@pytest.fixture
def sample_bytes() -> bytes:
    return (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes()
