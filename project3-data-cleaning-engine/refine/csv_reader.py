"""Read CSV bytes into raw text records.

Rules (all deterministic):

* Encoding: a UTF-16 byte order mark selects UTF-16; otherwise UTF-8
  (with or without a byte order mark), then cp1252, then latin-1. Any
  fallback is reported as a warning. An explicit encoding is used as is
  and fails loudly if the bytes do not decode.
* Delimiter: the candidate (comma, semicolon, tab, pipe) that splits the
  first records into the most consistent number of fields (>1) wins; ties
  go to the larger field count, then to the order above.
* Cells are kept exactly as text: no trimming, no number or date parsing,
  and "NA", "null" or empty strings are not turned into missing values.
* Completely blank lines are skipped and reported.
* Quoting is parsed strictly first. A stray quote (e.g. "abc"def) falls
  back to Python's lenient parser and is reported as a warning. A quote
  that is never closed is an error, because it would swallow the rest of
  the file into one cell.
"""

from __future__ import annotations

import codecs
import csv
import io
from collections import Counter

from refine.config import (
    CSV_FIELD_SIZE_LIMIT,
    DELIMITER_CANDIDATES,
    DELIMITER_NAMES,
    ENCODING_CANDIDATES,
    MAX_ROWS,
    SNIFF_CHARS,
    SNIFF_RECORDS,
    SUPPORTED_ENCODINGS,
)
from refine.errors import LoadError
from refine.notes import example_list, plural
from refine.raw_table import RawTable
from refine.source import LoadNote

ENCODING_LABELS = {
    "utf-8": "UTF-8",
    "utf-16": "UTF-16",
    "cp1252": "Windows-1252 (cp1252)",
    "latin-1": "Latin-1 (ISO-8859-1)",
}


def delimiter_label(delimiter: str) -> str:
    name = DELIMITER_NAMES.get(delimiter)
    shown = "\\t" if delimiter == "\t" else delimiter
    return f"{name} ({shown})" if name else repr(delimiter)


# ------------------------------------------------------------------
# Encoding
# ------------------------------------------------------------------

def normalize_encoding(encoding: str) -> str:
    """Map user-supplied names onto the supported set, or raise LoadError."""
    try:
        canonical = codecs.lookup(encoding).name
    except LookupError:
        canonical = ""
    aliases = {
        "utf-8": "utf-8", "utf-8-sig": "utf-8",
        "utf-16": "utf-16", "utf-16-le": "utf-16", "utf-16-be": "utf-16",
        "cp1252": "cp1252",
        "iso8859-1": "latin-1", "latin-1": "latin-1",
    }
    if canonical not in aliases:
        raise LoadError(
            "unsupported_encoding",
            f"Encoding {encoding!r} is not supported. Choose one of: {', '.join(SUPPORTED_ENCODINGS)}.",
        )
    return aliases[canonical]


def decode(data: bytes, encoding: str | None) -> tuple[str, str, bool, list[LoadNote]]:
    """Return (text, encoding used, detected?, notes)."""
    notes: list[LoadNote] = []
    has_utf8_bom = data.startswith(codecs.BOM_UTF8)
    has_utf16_bom = data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))

    if encoding is not None:
        enc = normalize_encoding(encoding)
        codec = {"utf-8": "utf-8-sig", "utf-16": "utf-16"}.get(enc, enc)
        try:
            text = data.decode(codec)
        except UnicodeDecodeError as exc:
            raise LoadError(
                "decode_failed",
                f"The file is not valid {ENCODING_LABELS[enc]}: byte {exc.start:,} cannot be decoded. "
                "Choose a different encoding, or leave it on automatic detection.",
            ) from None
        detected = False
    elif has_utf16_bom:
        try:
            text, enc = data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            raise LoadError(
                "decode_failed",
                "The file starts with a UTF-16 byte order mark but is not valid UTF-16 text.",
            ) from None
        detected = True
    else:
        text, enc = "", ""
        for candidate in ENCODING_CANDIDATES:
            codec = "utf-8-sig" if candidate == "utf-8" else candidate
            try:
                text, enc = data.decode(codec), candidate
                break
            except UnicodeDecodeError:
                continue
        detected = True
        if enc != "utf-8":
            notes.append(LoadNote(
                "encoding_fallback", "warning",
                f"The file is not valid UTF-8, so it was read as {ENCODING_LABELS[enc]}. "
                "Check that accented letters and symbols look right; if not, choose the correct "
                "encoding under parsing options.",
            ))

    if enc == "utf-8" and has_utf8_bom:
        notes.append(LoadNote(
            "bom_removed", "info",
            "The file starts with a UTF-8 byte order mark (an invisible marker some programs add). "
            "It was not treated as part of the first column name.",
        ))
    if "\x00" in text:
        raise LoadError(
            "binary_content",
            "The file contains NUL characters, so it does not look like a text CSV. It may be a "
            "binary file, or UTF-16 text without a byte order mark (choose UTF-16 under parsing options).",
        )
    return text, enc, detected, notes


# ------------------------------------------------------------------
# Delimiter
# ------------------------------------------------------------------

def _sample(text: str) -> str:
    if len(text) <= SNIFF_CHARS:
        return text
    cut = text.rfind("\n", 0, SNIFF_CHARS)
    return text[: cut if cut > 0 else SNIFF_CHARS]


def detect_delimiter(text: str) -> str | None:
    """Best delimiter candidate, or None when every candidate gives one field."""
    sample = _sample(text)
    best: tuple[float, int, int] | None = None
    best_delim = None
    for order, delim in enumerate(DELIMITER_CANDIDATES):
        counts = []
        try:
            for row in csv.reader(io.StringIO(sample, newline=""), delimiter=delim):
                if row:
                    counts.append(len(row))
                if len(counts) >= SNIFF_RECORDS:
                    break
        except csv.Error:
            continue
        if not counts:
            continue
        tally = Counter(counts)
        modal = max(tally.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if modal <= 1:
            continue
        score = (tally[modal] / len(counts), modal, -order)
        if best is None or score > best:
            best, best_delim = score, delim
    return best_delim


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------

class _QuoteProblem(Exception):
    def __init__(self, line: int, detail: str) -> None:
        self.line, self.detail = line, detail


def _parse(text: str, delimiter: str, strict: bool, row_limit: int):
    """Yield (start_line, record) for every record, including blank ones ([])."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"',
                        doublequote=True, skipinitialspace=False, strict=strict)
    records: list[list[str]] = []
    rows: list[int] = []
    blank_lines: list[int] = []
    previous_end = 0
    while True:
        start = previous_end + 1
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            detail = str(exc)
            if "field larger than field limit" in detail:
                raise LoadError(
                    "field_too_large",
                    f"A value starting on line {start:,} is longer than "
                    f"{CSV_FIELD_SIZE_LIMIT:,} characters. This usually means a quote mark is "
                    "never closed, or the delimiter is wrong.",
                ) from None
            if "unexpected end of data" in detail:
                raise LoadError(
                    "unclosed_quote",
                    f"A quoted value starting on line {start:,} is never closed, so the rest of the "
                    "file would be read into one cell. Fix the quote marks in the file and upload it again.",
                ) from None
            raise _QuoteProblem(start, detail) from None
        previous_end = reader.line_num
        if not record:
            blank_lines.append(start)
            continue
        records.append(record)
        rows.append(start)
        if len(records) > row_limit:
            raise LoadError(
                "too_many_rows",
                f"The file has more than {MAX_ROWS:,} data rows, the current limit. "
                "Split the file or remove rows you do not need, then upload it again.",
            )
    return records, rows, blank_lines


def read_csv(data: bytes, encoding: str | None, delimiter: str | None, skip_rows: int) -> RawTable:
    text, enc, enc_detected, notes = decode(data, encoding)
    if not text.strip():
        raise LoadError("empty_file", "The file has no content (only whitespace or blank lines).")

    if delimiter is not None:
        if len(delimiter) != 1 or delimiter in '"\r\n':
            raise LoadError("bad_delimiter", "The delimiter must be a single character other than a quote or line break.")
        delim, delim_detected = delimiter, False
    else:
        found = detect_delimiter(text)
        delim_detected = True
        if found is None:
            delim = ","
            notes.append(LoadNote(
                "single_column", "info",
                "No delimiter (comma, semicolon, tab or pipe) splits this file into columns, so it "
                "was read as a single column. Choose a delimiter under parsing options if that is wrong.",
            ))
        else:
            delim = found

    csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    # +1 for the header, plus any rows the user asked to skip.
    row_limit = MAX_ROWS + 1 + skip_rows
    try:
        records, rows, blank_lines = _parse(text, delim, strict=True, row_limit=row_limit)
    except _QuoteProblem as problem:
        records, rows, blank_lines = _parse(text, delim, strict=False, row_limit=row_limit)
        notes.append(LoadNote(
            "lenient_quotes", "warning",
            f"Line {problem.line:,} has a quote mark in an unexpected place (for example "
            '"abc"def). The file was read with lenient quote handling, which removes such quote '
            "marks and joins the text around them. Values on this line, and possibly others, "
            "may differ from the file.",
            rows=(problem.line,),
        ))

    if not delim_detected and records and max(len(r) for r in records) == 1:
        notes.append(LoadNote(
            "single_column", "warning",
            f"The chosen delimiter, {delimiter_label(delim)}, does not split any line, so the file "
            "was read as a single column. Choose another delimiter or switch back to automatic detection.",
        ))

    if blank_lines:
        notes.append(LoadNote(
            "blank_lines_skipped", "info",
            f"Skipped {plural(len(blank_lines), 'completely blank line')} "
            f"({example_list(blank_lines, 'line')}).",
            rows=tuple(blank_lines[:20]),
        ))

    return RawTable(
        records=records,
        record_rows=rows,
        row_unit="line",
        ragged_is_error=True,
        notes=notes,
        meta={"encoding": enc, "encoding_detected": enc_detected,
              "delimiter": delim, "delimiter_detected": delim_detected},
    )
