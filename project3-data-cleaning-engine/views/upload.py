"""Page 1: Upload. Load a file, see how it was read, adjust parsing options."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from refine.config import (
    DELIMITER_CANDIDATES,
    MAX_COLUMNS,
    MAX_ROWS,
    MAX_UPLOAD_MB,
    PREVIEW_ROWS,
    SUPPORTED_ENCODINGS,
)
from refine.csv_reader import ENCODING_LABELS, delimiter_label
from refine.loader import PRESERVATION_SUMMARY, verify_raw_bytes
from refine.notes import plural
from refine.source import LoadedDataset, LoadOptions
from ui.components import callout, load_note, page_header, section, stat_card_html, stat_row
from ui.state import (
    SAMPLE_NAME,
    clear_dataset,
    get_dataset,
    get_error,
    get_session,
    load_sample,
    on_upload_change,
    reparse,
    uploader_key,
)

AUTO = "Automatic"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n:,} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:,.1f} KB"
    return f"{n / 1024 / 1024:,.1f} MB"


def _source_section(ds: LoadedDataset) -> None:
    src = ds.source
    if src.file_format == "csv":
        fmt_foot = (
            f"{ENCODING_LABELS.get(src.encoding, src.encoding)}"
            f"{' (detected)' if src.encoding_detected else ''} · "
            f"{delimiter_label(src.delimiter)}{' (detected)' if src.delimiter_detected else ''}"
        )
    else:
        fmt_foot = f"Sheet '{src.sheet_name}' of {len(src.sheets)}"
    with section("Source file", key="source"):
        stat_row([
            stat_card_html("File", src.file_name, _size(src.size_bytes)),
            stat_card_html("Format", src.file_format.upper(), fmt_foot),
            stat_card_html("Rows", f"{src.n_rows:,}", "Data rows, excluding the header"),
            stat_card_html("Columns", f"{src.n_columns:,}"),
        ])
        intact = verify_raw_bytes(ds)
        st.caption(
            f"SHA-256 {src.sha256[:16]}… · Original file "
            + ("kept in memory, checksum verified" if intact else "not kept in memory")
        )
        callout("What is preserved", PRESERVATION_SUMMARY, "neutral")


def _notes_section(ds: LoadedDataset) -> None:
    warnings = [n for n in ds.notes if n.severity == "warning"]
    infos = [n for n in ds.notes if n.severity == "info"]
    caption = (
        f"{plural(len(warnings), 'item')} to check · {plural(len(infos), 'note')}"
        if ds.notes else None
    )
    with section("Load notes", caption=caption, key="notes"):
        if not ds.notes:
            callout(
                "No parsing adjustments",
                "The file was read without renaming, padding, skipping or converting anything.",
                "positive",
            )
        for note in warnings + infos:
            load_note(note)


def _option_key(name: str, suffix: str) -> str:
    return f"refine_opt_{name}_{suffix}"


def _apply_options(suffix: str) -> None:
    """Form callback: re-read the retained file with the chosen options."""
    state = st.session_state
    encoding = state.get(_option_key("encoding", suffix), AUTO)
    delimiter = state.get(_option_key("delimiter", suffix), AUTO)
    reparse(LoadOptions(
        encoding=None if encoding == AUTO else encoding,
        delimiter=None if delimiter == AUTO else delimiter,
        sheet=state.get(_option_key("sheet", suffix)),
        skip_rows=int(state.get(_option_key("skip", suffix), 0)),
    ))


def _options_form(ds: LoadedDataset) -> None:
    src = ds.source
    with st.expander("Parsing options", icon=":material/tune:"):
        if ds.raw_bytes is None:
            st.caption("The original file is not kept in memory, so it cannot be re-read. Upload it again.")
            return
        # Widget keys include the checksum and current options, so the form
        # always opens showing how the current table was read, and never
        # carries values over from another file.
        o = src.options
        suffix = f"{src.sha256[:12]}_{o.encoding}_{o.delimiter!r}_{o.sheet}_{o.skip_rows}"
        with st.form(f"refine_parse_options_{suffix}", border=False):
            if src.file_format == "csv":
                enc_options = [AUTO, *SUPPORTED_ENCODINGS]
                current_enc = AUTO if src.encoding_detected else src.encoding
                st.selectbox(
                    "Encoding", enc_options, index=enc_options.index(current_enc), key=_option_key("encoding", suffix),
                    format_func=lambda e: e if e == AUTO else ENCODING_LABELS[e],
                )
                delim_options = [AUTO, *DELIMITER_CANDIDATES]
                current_delim = AUTO if src.delimiter_detected else src.delimiter
                if current_delim not in delim_options:
                    delim_options.append(current_delim)
                st.selectbox(
                    "Delimiter", delim_options, index=delim_options.index(current_delim), key=_option_key("delimiter", suffix),
                    format_func=lambda d: d if d == AUTO else delimiter_label(d),
                )
            else:
                names = [s.name for s in src.sheets]
                hidden = {s.name for s in src.sheets if s.hidden}
                st.selectbox(
                    "Sheet", names, index=names.index(src.sheet_name), key=_option_key("sheet", suffix),
                    format_func=lambda n: f"{n} (hidden)" if n in hidden else n,
                )
            st.number_input(
                "Rows above the header to ignore", min_value=0, max_value=1000,
                value=src.options.skip_rows, step=1, key=_option_key("skip", suffix),
                help="Counts non-empty rows. Use this when a title or notes sit above the column names.",
            )
            st.form_submit_button("Re-read file", icon=":material/refresh:", on_click=_apply_options, args=(suffix,))


def _preview_section(ds: LoadedDataset) -> None:
    shown = min(PREVIEW_ROWS, len(ds.data))
    with section(
        "Raw preview",
        caption=f"First {shown:,} of {len(ds.data):,} rows, exactly as parsed (all values are text). "
                "Empty text appears blank; cells with no value appear as None.",
        key="preview",
    ):
        st.dataframe(ds.data.head(PREVIEW_ROWS), width="stretch", height=360)
        renamed = [
            (i + 1, label, name)
            for i, (label, name) in enumerate(zip(ds.source.header_labels, ds.source.columns))
            if label != name
        ]
        if renamed:
            st.caption("Columns named differently from the file's header:")
            st.dataframe(
                pd.DataFrame(renamed, columns=["Position", "Header in file", "Loaded as"]),
                hide_index=True, width="content",
            )


def render() -> None:
    page_header(
        "Upload a dataset",
        "Load a CSV or Excel file to inspect it. Loading only reads the file: nothing is cleaned "
        "or changed until you choose and confirm a step.",
    )

    left, right = st.columns([3, 2], gap="medium")
    with left:
        st.file_uploader(
            "CSV or Excel workbook",
            type=["csv", "xlsx"],
            key=uploader_key(),
            on_change=on_upload_change,
            help=f"Up to {MAX_UPLOAD_MB} MB, {MAX_ROWS:,} rows and {MAX_COLUMNS:,} columns. "
                 "Legacy .xls files are not supported; save them as .xlsx first.",
        )
    with right:
        with section("Try the sample", key="sample"):
            st.caption(
                f"{SAMPLE_NAME}: 500 synthetic customer rows with duplicates, missing-value "
                "markers, mixed date formats and inconsistent text."
            )
            st.button("Open sample dataset", icon=":material/dataset:", key="refine_load_sample",
                      on_click=load_sample)

    error = get_error()
    if error is not None:
        kept = " The previously loaded dataset is still open." if get_dataset() is not None else ""
        st.error(f"**{error.file_name} could not be loaded.** {error.message}{kept}",
                 icon=":material/error:")

    ds = get_dataset()
    if ds is None:
        return

    _source_section(ds)
    _notes_section(ds)
    steps = len(get_session(ds).steps)
    if steps:
        callout("Cleaning steps in progress",
                f"{plural(steps, 'cleaning step')} {'has' if steps == 1 else 'have'} been applied to this data. "
                "Re-reading the file, loading another file or closing it starts again from the original "
                "and discards the step history. The preview below shows the data as loaded.", "caution")
    _options_form(ds)
    _preview_section(ds)

    st.button("Close dataset", icon=":material/close:", key="refine_close", on_click=clear_dataset)


render()
