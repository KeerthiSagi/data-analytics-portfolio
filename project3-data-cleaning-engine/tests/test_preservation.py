"""Original bytes are kept, loading is deterministic, and the engine is
independent of Streamlit."""

import hashlib
import subprocess
import sys

import pandas as pd

import refine.loader as loader
from refine.loader import load_file, verify_raw_bytes
from refine.source import LoadOptions
from tests.conftest import PROJECT_DIR, load_csv, make_xlsx

MESSY = (
    "﻿id;name;name;;\n"
    "1;Ann ;x;;\n"
    "\n"
    "2;Bo\n"
    '3;"5"" monitor";NA;;\n'
).encode("utf-8")


def test_raw_bytes_retained_exactly_with_checksum():
    ds = load_file("messy.csv", MESSY)
    assert ds.raw_bytes == MESSY
    assert ds.raw_bytes_retained
    assert ds.source.sha256 == hashlib.sha256(MESSY).hexdigest()
    assert ds.source.size_bytes == len(MESSY)
    assert verify_raw_bytes(ds)


def test_raw_bytes_not_retained_above_threshold(monkeypatch):
    monkeypatch.setattr(loader, "MAX_RETAINED_BYTES", 10)
    ds = load_file("messy.csv", MESSY)
    assert ds.raw_bytes is None and not ds.raw_bytes_retained
    assert not verify_raw_bytes(ds)
    assert ds.source.sha256 == hashlib.sha256(MESSY).hexdigest()   # Checksum still recorded


def test_input_buffer_not_modified():
    buffer = bytearray(MESSY)
    load_file("messy.csv", buffer)
    assert bytes(buffer) == MESSY


def test_same_input_gives_identical_result():
    a, b = load_file("messy.csv", MESSY), load_file("messy.csv", MESSY)
    pd.testing.assert_frame_equal(a.data, b.data)
    assert a.notes == b.notes
    assert a.source == b.source
    assert a.source_rows.tolist() == b.source_rows.tolist()


def test_xlsx_load_is_deterministic():
    data = make_xlsx({"S": [["id", "v", "v"], [1, 2.5, True], [None, "x", None]]})
    a, b = load_file("b.xlsx", data), load_file("b.xlsx", data)
    pd.testing.assert_frame_equal(a.data, b.data)
    assert a.notes == b.notes


def test_reparse_from_retained_bytes_matches_fresh_upload():
    first = load_file("messy.csv", MESSY)
    again = load_file("messy.csv", first.raw_bytes, LoadOptions(delimiter=";"))
    fresh = load_file("messy.csv", MESSY, LoadOptions(delimiter=";"))
    pd.testing.assert_frame_equal(again.data, fresh.data)


def test_messy_file_reports_every_adjustment():
    ds = load_file("messy.csv", MESSY)
    found = {n.code for n in ds.notes}
    assert found == {"bom_removed", "blank_lines_skipped", "blank_header", "empty_unnamed_columns",
                     "short_rows_padded", "duplicate_header"}
    assert list(ds.data.columns) == ["id", "name", "name_2", "column_4", "column_5"]
    # And the values themselves are untouched.
    assert ds.data["name"].tolist() == ["Ann ", "Bo", '5" monitor']


def test_file_name_reduced_to_base_name():
    ds = load_csv("a\n1\n", name="/tmp/uploads/../data.csv")
    assert ds.source.file_name == "data.csv"


def test_every_note_has_a_known_severity_and_message():
    ds = load_file("messy.csv", MESSY)
    for note in ds.notes:
        assert note.severity in {"info", "warning"}
        first = note.message[0]
        assert (first.isupper() or first.isdigit()) and note.message.endswith(".")


def test_engine_does_not_import_streamlit():
    code = (
        "import sys; import refine, refine.loader, refine.csv_reader, refine.xlsx_reader; "
        "refine.load_file('a.csv', b'a\\n1\\n'); "
        "sys.exit(1 if 'streamlit' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or "refine imported streamlit"
