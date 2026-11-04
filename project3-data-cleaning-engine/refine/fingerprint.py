"""Deterministic fingerprints of a parsed table and of a cleaning state.

`frame_fingerprint(frame)` is a SHA-256 over the table's content: column
names in order, each column's dtype, the row count, and every cell value
in row order, with missing cells marked separately from their values (so
pd.NA and the empty string "" give different fingerprints).

It does not depend on how pandas or Arrow happen to store the data: the
same values give the same fingerprint on pandas 2.2 and 3.x, whatever the
Arrow chunking, and whether or not the frame is a copy. Typed columns
(nullable integers, floats and booleans, Arrow dates, datetimes) are
hashed by their value bytes with missing cells zeroed. It is not the
uploaded file's checksum (SourceInfo.sha256): two files that parse to the
same table share a frame fingerprint, and every cleaning step changes it.

`state_fingerprint(frame, row_ids)` also covers row lineage, so two states
holding the same values for different original rows are told apart.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

FINGERPRINT_VERSION = "refine-fp-1"


class _Hasher:
    """SHA-256 over length-prefixed fields, so field boundaries are unambiguous."""

    def __init__(self, tag: str) -> None:
        self._h = hashlib.sha256()
        self.add(tag.encode())

    def add(self, data: bytes) -> None:
        self._h.update(len(data).to_bytes(8, "little"))
        self._h.update(data)

    def text(self, value: str) -> None:
        self.add(value.encode("utf-8", "surrogatepass"))

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def dtype_label(dtype) -> str:
    """A stable dtype name. pandas 3 prints its default string dtype as
    "str" and pandas 2 as "string", so string dtypes are named by storage
    and missing-value marker instead."""
    if isinstance(dtype, pd.StringDtype):
        na = getattr(dtype, "na_value", pd.NA)
        na_label = "nan" if isinstance(na, float) and np.isnan(na) else "NA"
        storage = "pyarrow" if str(dtype.storage).startswith("pyarrow") else str(dtype.storage)
        return f"string[{storage}]/{na_label}"
    return str(dtype)


def _string_column(h: _Hasher, series: pd.Series, mask: np.ndarray) -> bool:
    """Fast path for string columns via Arrow. False if it cannot be used."""
    try:
        arr = pa.array(series.array, from_pandas=True)
        if isinstance(arr, pa.ChunkedArray):
            arr = pa.concat_arrays(arr.chunks) if arr.num_chunks else pa.array([], pa.large_string())
        arr = pc.fill_null(arr.cast(pa.large_binary()), b"")
        arr = pa.concat_arrays([arr])          # Fresh buffers, offset 0
    except (pa.ArrowException, TypeError, ValueError):
        return False
    if len(arr) != len(series):
        return False
    offsets = np.frombuffer(arr.buffers()[1], dtype=np.int64)[arr.offset:arr.offset + len(arr) + 1]
    data_buf = arr.buffers()[2]
    values = b"" if data_buf is None else data_buf.to_pybytes()[offsets[0]:offsets[-1]]
    h.add(np.diff(offsets).astype("<i8").tobytes())
    h.add(values)
    return True


def _typed_column(h: _Hasher, series: pd.Series, mask: np.ndarray) -> bool:
    """Fast paths for the typed columns Convert data type produces. False if not one of them.

    Values are hashed by their bytes, with missing cells zeroed (the mask
    is hashed separately), so the cost is independent of Python objects:
    nullable Int64 / Float64 / boolean, Arrow date32 (days since 1970) and
    time-zone-aware datetimes (UTC microseconds; the zone is in the dtype).
    """
    dtype = series.dtype
    if isinstance(dtype, pd.ArrowDtype):
        if not pa.types.is_date32(dtype.pyarrow_dtype):
            return False
        arr = pa.array(series.array, from_pandas=True)
        if isinstance(arr, pa.ChunkedArray):
            arr = pa.concat_arrays(arr.chunks) if arr.num_chunks else pa.array([], pa.date32())
        days = pc.fill_null(arr.cast(pa.int32()), 0).to_numpy(zero_copy_only=False)
        h.add(np.ascontiguousarray(days, dtype="<i4").tobytes())
        return True
    if isinstance(dtype, pd.DatetimeTZDtype):
        values = series.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy().view("i8")
        h.add(np.ascontiguousarray(np.where(mask, 0, values), dtype="<i8").tobytes())
        return True
    if (pd.api.types.is_extension_array_dtype(dtype) and getattr(dtype, "kind", "") in "biuf"
            and isinstance(getattr(dtype, "numpy_dtype", None), np.dtype)):
        np_dtype = dtype.numpy_dtype
        values = series.to_numpy(dtype=np_dtype, na_value=np.zeros(1, dtype=np_dtype)[0])
        h.add(np.ascontiguousarray(values).astype(np_dtype.newbyteorder("<")).tobytes())
        return True
    return False


def _generic_column(h: _Hasher, series: pd.Series, mask: np.ndarray) -> None:
    """Any dtype: numeric and datetime values by their bytes, everything else by str()."""
    dtype = series.dtype
    if _typed_column(h, series, mask):
        return
    if isinstance(dtype, np.dtype) and dtype.kind in "biufcmM":
        values = series.to_numpy()
        if dtype.kind in "mM":
            values = values.view("i8")
        values = np.where(mask, np.zeros(1, dtype=values.dtype), values)
        h.add(np.ascontiguousarray(values).astype(values.dtype.newbyteorder("<")).tobytes())
        return
    present = series.to_numpy(dtype=object)[~mask]
    encoded = [str(v).encode("utf-8", "surrogatepass") for v in present]
    h.add(np.fromiter((len(b) for b in encoded), dtype="<i8", count=len(encoded)).tobytes())
    h.add(b"".join(encoded))


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """Hex SHA-256 of the table's names, order, dtypes and values."""
    h = _Hasher(FINGERPRINT_VERSION)
    h.text(f"{frame.shape[0]}x{frame.shape[1]}")
    for position in range(frame.shape[1]):
        name = frame.columns[position]
        series = frame.iloc[:, position]
        h.text(f"{type(name).__name__}:{name}")
        h.text(dtype_label(series.dtype))
        mask = series.isna().to_numpy(dtype=bool)
        h.add(np.packbits(mask).tobytes())
        if not (isinstance(series.dtype, pd.StringDtype) and _string_column(h, series, mask)):
            _generic_column(h, series, mask)
    return h.hexdigest()


def row_ids_fingerprint(row_ids: np.ndarray) -> str:
    h = _Hasher(FINGERPRINT_VERSION + "/rows")
    h.add(np.ascontiguousarray(row_ids, dtype="<i8").tobytes())
    return h.hexdigest()


def state_fingerprint(frame: pd.DataFrame, row_ids: np.ndarray) -> str:
    """Fingerprint of a cleaning state: the table plus which original rows it holds."""
    h = _Hasher(FINGERPRINT_VERSION + "/state")
    h.text(frame_fingerprint(frame))
    h.text(row_ids_fingerprint(row_ids))
    return h.hexdigest()
