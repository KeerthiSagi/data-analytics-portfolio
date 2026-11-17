"""Engine-wide limits and constants.

The limits are deliberately conservative. They keep one session's data
(original bytes, parsed table, and the copies later phases will hold for
undo) well inside the ~1 GB of memory a Streamlit Community Cloud app
gets. They are checked before or during parsing, so an oversized file is
rejected with a clear message rather than truncated.

MAX_UPLOAD_MB must match `server.maxUploadSize` in .streamlit/config.toml
(a test checks this).
"""

from __future__ import annotations

ENGINE_NAME = "REFINE"
ENGINE_VERSION = "0.9.0"

# ------------------------------------------------------------------
# Supported formats
# ------------------------------------------------------------------

SUPPORTED_EXTENSIONS = (".csv", ".xlsx")

# ------------------------------------------------------------------
# Size limits
# ------------------------------------------------------------------

# Benchmark (Phase 1, Apple silicon laptop, Python 3.13, pandas 3.0):
#   CSV  200,000 × 10, 17.5 MiB: 0.4 s, table 31 MiB, peak +320 MiB while parsing
#   CSV  150,000 × 15, 19.7 MiB: 0.5 s, table 35 MiB, peak +345 MiB while parsing
#   XLSX 200,000 × 15, 14.6 MiB: 12.6 s, table 42 MiB, peak +230 MiB
#   XLSX  50,000 × 10,  2.5 MiB: 2.1 s, table 7 MiB, peak +50 MiB
# Larger inputs have not been benchmarked, so they are not accepted.
MAX_UPLOAD_MB = 20
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_ROWS = 200_000          # Data rows, not counting the header
MAX_COLUMNS = 300
MAX_CELLS = 2_000_000       # rows × columns

# An .xlsx file is a zip archive. Its uncompressed size is checked before
# parsing so a small, highly compressed file cannot exhaust memory.
MAX_XLSX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024

# The original upload is kept in memory (for re-parsing with different
# options and for its checksum) when it is no larger than this. With the
# current upload limit that is always the case.
MAX_RETAINED_BYTES = MAX_UPLOAD_BYTES

# ------------------------------------------------------------------
# CSV parsing
# ------------------------------------------------------------------

# Encodings tried, in order, when none is given. UTF-16 is used only when
# the file starts with a UTF-16 byte order mark. latin-1 can decode any
# byte sequence, so it is the final fallback and always produces a warning.
ENCODING_CANDIDATES = ("utf-8", "cp1252", "latin-1")
SUPPORTED_ENCODINGS = ("utf-8", "utf-16", "cp1252", "latin-1")

# Delimiters considered by detection, in tie-break order.
DELIMITER_CANDIDATES = (",", ";", "\t", "|")
DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}

# How much of the file delimiter detection looks at.
SNIFF_CHARS = 64 * 1024
SNIFF_RECORDS = 50

# Python's csv module rejects fields longer than this many characters.
CSV_FIELD_SIZE_LIMIT = 1024 * 1024

# Line numbers listed in a load note before it says "and N more".
NOTE_EXAMPLE_LIMIT = 5

# Rows shown in the raw preview on the Upload page.
PREVIEW_ROWS = 100

# ------------------------------------------------------------------
# Cleaning session
# ------------------------------------------------------------------

# Earlier states kept in memory so undo is instant. Older states are
# rebuilt by replaying the recorded steps from the original table, so
# undo is correct however many steps there are; this only bounds memory.
SNAPSHOT_LIMIT = 5

# Memory the cached states may use together (measured with pandas'
# memory_usage(deep=True), which over-counts Arrow buffers that states
# share, so the real cost is at most this). The most recent states are
# kept while they fit; a single state larger than the budget is never
# cached, so for the largest tables undo always replays. Small tables keep
# all SNAPSHOT_LIMIT states.
#
# Benchmark (Phase 4, Apple silicon laptop, pandas 3.0.6 and 2.2.3), six
# steps that keep every row:
#   200,000 × 10: 5 states, 165 MiB before; 2 states, 73 MiB now; 6 undos 0.2 s
#     6,666 × 300: 5 states, 120 MiB before; 3 states, 72 MiB now; 6 undos 0.1 s
SNAPSHOT_MEMORY_BUDGET_BYTES = 96 * 1024 * 1024

# Rows of affected data shown in a step preview.
PREVIEW_SAMPLE_ROWS = 10
