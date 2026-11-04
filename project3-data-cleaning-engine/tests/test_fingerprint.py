"""Dataset fingerprints: deterministic, content-based, the same on pandas 2.2 and 3.x."""

import numpy as np
import pandas as pd
import pytest

from refine.fingerprint import dtype_label, frame_fingerprint, row_ids_fingerprint, state_fingerprint
from refine.source import TEXT_DTYPE
from tests.conftest import load_csv


def text_frame(**cols) -> pd.DataFrame:
    return pd.DataFrame({k: pd.array(v, dtype=TEXT_DTYPE) for k, v in cols.items()})


BASE = dict(a=["x", None, "", "yy"], b=["1", "2", "3", "4"])

# Recorded on pandas 3.0.6 and 2.2.3: a change here means fingerprints of
# existing recipes would no longer match.
GOLDEN_SMALL = "0d5bdeae43b15c3a439daf6dc25936eac4ce546cd82ccbf8efb67afb307015c8"
GOLDEN_SAMPLE = "dadd4fbbc2902523a1a67a0bbc4f19126d100ced6c59c4964d3f7acd02f3abd8"


def test_golden_values_are_stable_across_pandas_versions(sample_bytes):
    assert frame_fingerprint(text_frame(**BASE)) == GOLDEN_SMALL
    assert frame_fingerprint(load_csv(sample_bytes).data) == GOLDEN_SAMPLE


def test_same_content_same_fingerprint():
    assert frame_fingerprint(text_frame(**BASE)) == frame_fingerprint(text_frame(**BASE))
    df = text_frame(**BASE)
    assert frame_fingerprint(df) == frame_fingerprint(df.copy(deep=True))


def test_independent_of_index_and_arrow_chunking():
    df = text_frame(**BASE)
    sliced = text_frame(a=["q"] + BASE["a"], b=["0"] + BASE["b"]).iloc[1:]
    assert frame_fingerprint(sliced) == frame_fingerprint(df)
    chunked = pd.concat([df.iloc[:2], df.iloc[2:]], ignore_index=True)
    assert frame_fingerprint(chunked) == frame_fingerprint(df)


@pytest.mark.parametrize("change", [
    lambda d: d.rename(columns={"a": "A"}),                     # Column name
    lambda d: d[["b", "a"]],                                    # Column order
    lambda d: d.iloc[::-1].reset_index(drop=True),              # Row order
    lambda d: d.assign(b=pd.array(["1", "2", "3", "5"], dtype=TEXT_DTYPE)),     # Cell value
    lambda d: d.assign(b=pd.array([1, 2, 3, 4], dtype="Int64")),                # Dtype
    lambda d: d.assign(b=pd.array(["1", "2", "3", "4"], dtype="string[python]")),  # Storage
    lambda d: d.iloc[:3],                                       # Row count
    lambda d: d.drop(columns="b"),                              # Column count
])
def test_any_change_changes_fingerprint(change):
    df = text_frame(**BASE)
    assert frame_fingerprint(change(df)) != frame_fingerprint(df)


def test_missing_is_distinct_from_empty_text_and_from_text_na():
    na = text_frame(a=[None, "x"])
    empty = text_frame(a=["", "x"])
    word = text_frame(a=["<NA>", "x"])
    assert len({frame_fingerprint(na), frame_fingerprint(empty), frame_fingerprint(word)}) == 3


def test_value_boundaries_are_unambiguous():
    assert frame_fingerprint(text_frame(a=["ab", "c"])) != frame_fingerprint(text_frame(a=["a", "bc"]))


def test_non_text_dtypes():
    df = pd.DataFrame({"f": [1.0, np.nan, 3.0], "i": [1, 2, 3], "t": pd.to_datetime(["2024-01-01", None, "2024-01-03"]),
                       "o": pd.Series(["a", None, 3], dtype=object)})
    assert frame_fingerprint(df) == frame_fingerprint(df.copy())
    assert frame_fingerprint(df) != frame_fingerprint(df.assign(f=[1.0, np.nan, 4.0]))
    assert frame_fingerprint(df) != frame_fingerprint(df.assign(f=[1.0, 0.0, 3.0]))   # NaN is not 0


def test_text_dtype_label_is_version_independent():
    assert dtype_label(TEXT_DTYPE) == "string[pyarrow]/NA"


def test_state_fingerprint_covers_row_lineage():
    df = text_frame(a=["x", "x"])
    assert state_fingerprint(df, np.array([0, 1])) != state_fingerprint(df, np.array([1, 0]))
    assert row_ids_fingerprint(np.array([0, 1])) == row_ids_fingerprint(np.array([0, 1], dtype=np.int32))


def test_fingerprint_does_not_modify_frame():
    df = text_frame(**BASE)
    before = df.copy(deep=True)
    frame_fingerprint(df)
    pd.testing.assert_frame_equal(df, before)


def test_differs_from_file_checksum(sample_bytes):
    ds = load_csv(sample_bytes)
    assert frame_fingerprint(ds.data) != ds.source.sha256
    # The same table from a different file (CRLF line endings) has the same fingerprint.
    crlf = load_csv(sample_bytes.replace(b"\n", b"\r\n"))
    assert crlf.source.sha256 != ds.source.sha256
    assert frame_fingerprint(crlf.data) == frame_fingerprint(ds.data)


def test_typed_columns_from_phase5():
    import datetime as dt

    import pyarrow as pa

    cases = [
        (pd.array([1, None, 3], dtype="Int64"), pd.array([1, 0, 3], dtype="Int64")),
        (pd.array([1.5, None, -0.0], dtype="Float64"), pd.array([1.5, 0.0, -0.0], dtype="Float64")),
        (pd.array([True, None, False], dtype="boolean"), pd.array([True, False, False], dtype="boolean")),
        (pd.array([dt.date(2026, 1, 15), None], dtype=pd.ArrowDtype(pa.date32())),
         pd.array([dt.date(2026, 1, 15), dt.date(1970, 1, 1)], dtype=pd.ArrowDtype(pa.date32()))),
        (pd.Series(pd.to_datetime(["2026-01-15 10:00", None]).as_unit("us")).dt.tz_localize("UTC").array,
         pd.Series(pd.to_datetime(["2026-01-15 10:00", "1970-01-01 00:00"]).as_unit("us")).dt.tz_localize("UTC").array),
    ]
    for with_missing, with_zero in cases:
        a, b = pd.DataFrame({"x": with_missing}), pd.DataFrame({"x": with_zero})
        assert frame_fingerprint(a) == frame_fingerprint(a.copy(deep=True))
        assert frame_fingerprint(a) != frame_fingerprint(b)                   # Missing is not zero
    d = pd.DataFrame({"x": pd.array([dt.date(2026, 1, 15)] * 4, dtype=pd.ArrowDtype(pa.date32()))})
    chunked = pd.DataFrame({"x": pd.arrays.ArrowExtensionArray(pa.chunked_array(
        [[dt.date(2026, 1, 15)] * 2, [dt.date(2026, 1, 15)] * 2], type=pa.date32()))})
    assert frame_fingerprint(d) == frame_fingerprint(chunked)
    assert frame_fingerprint(pd.DataFrame({"x": pd.array([1.0], dtype="Float64")})) != frame_fingerprint(
        pd.DataFrame({"x": pd.array([-1.0], dtype="Float64")}))
