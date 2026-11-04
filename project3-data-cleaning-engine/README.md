# REFINE — Automated Data Cleaning Engine

A Streamlit application for finding, understanding, and fixing data quality problems in CSV and Excel files. Cleaning is rule-based and transparent. Nothing changes until you preview a step and confirm it, and every step is recorded.

**Status: in development.** Phase 1 (file loading), Phase 2 (profiling and quality assessment), Phase 3 (cleaning pipeline, history and undo), Phase 4 (missing values and duplicates) and Phase 5 (data type conversion and text standardization) are complete. Engine version 0.5.0. Outliers, comparison and exports arrive in later phases and show a "Not available yet" notice.

**Live app:** https://data-analyst-portfolio-fbu8nnzddmgchaadmjmc8q.streamlit.app/ (shows the most recently deployed version)

## What works today

- Upload a **.csv** or **.xlsx** file, or open the built-in synthetic sample.
- See how the file was read: encoding, delimiter or sheet, row and column counts, and the file's SHA-256 checksum.
- **Load notes** list every interpretation or adjustment the loader made, such as a removed byte order mark, renamed duplicate columns, padded short rows, skipped blank lines, or Excel formulas without saved results. Notes that need your attention are marked **Check**.
- Re-read the file with a different encoding, delimiter, sheet, or number of title rows to skip.
- Preview the first 100 rows exactly as parsed.
- **Profile & Quality** page: dataset overview, a profile of every column, rule-based quality checks with counts and denominators, column inspection, optional validity rules, and the load notes.
- **Clean** page, in tabs: **Columns** (drop columns), **Missing values** (standardize markers, drop rows, drop mostly-missing columns, fill, add an indicator column), **Duplicates** (find and remove), **Data types** (convert to text, whole numbers, decimals, true/false, dates, dates with times) and **Text** (trim, capitalization, repeated spaces, Unicode, non-printing characters, find and replace, category mapping). Every step shows its exact effect (counts, percentages, warnings, a before-and-after sample) and is applied only after you confirm it in a dialog.
- **History** page: the applied steps, a full audit log (applied, undone, reset), undo of the last step, reset to the original with confirmation, and the reproducible recipe with a replay check.

## Profile & Quality

Profiling only reads the table. Nothing is converted, trimmed or filled in.

**Definitions used everywhere on the page:**

| Term | Meaning |
|---|---|
| Missing | No value (an empty Excel cell, or a CSV row that ended early) or empty text (an empty CSV field). |
| Whitespace-only | Text made only of spaces or tabs. Counted separately, never as missing. |
| Null-like text | `NA`, `N/A`, `null`, `None`, `nan`, `-`, `?` and similar, compared after trimming and ignoring case. Counted separately, never as missing, because only you can confirm what they mean. `unknown` is deliberately not on the list. |
| Values checked | Non-missing values that are neither whitespace-only nor null-like. Type checks use these. |
| Confirmed / inferred type | A column stored as numbers, dates or booleans has a *confirmed* type. Every loaded column is stored as text, so its type is *inferred* from the values: a suggestion with a confidence level and the evidence behind it. |

**Dataset metrics:** rows, columns, cells, missing cells, null-like and whitespace-only cells, exact duplicate rows (rows repeating an earlier row in every column), memory, and column type distribution.

**Column metrics:** storage dtype, suggested type (empty, boolean, integer, decimal, date, date & time, email, identifier, categorical, text, or mixed), missing breakdown, distinct values, top 10 values, text lengths, leading/trailing/repeated whitespace, null-like tokens, and parsing indicators:

- Plain numbers vs numbers that need symbols or separators removed (`$1,234.50`).
- Valid dates by format, dates that are not real calendar dates (`2024-02-30`), and ambiguous month/day dates (`01/05/2024`).
- Yes/no style values and email-like values.
- Capitalization variants of the same value (`USA` / `usa`).

Numeric statistics (min, quartiles, median, mean, standard deviation, max) are shown only for mostly-numeric columns. They use plain numbers only; formatted values are counted but excluded, because reading them needs an assumption about separators.

**Quality checks:** missing values, null-like text, duplicate rows, empty columns, whitespace (whitespace-only, leading/trailing, repeated spaces), capitalization (variants and case-style mix), numeric type mismatches and formatting, date type mismatches, mixed date formats, ambiguous dates, and validity rules. Every issue states its category, column or dataset, severity (High, Medium, Low), affected count, denominator, percentage, explanation, and a suggested action. Fixes are never applied.

- There is **no overall quality score**. One number would hide which problems matter for your purpose.
- A check that finds nothing is reported as "nothing detected", never as "clean".
- A check that cannot run reliably is marked **Not assessed** with the reason, for example type checks on a column with fewer than 5 values, or validity when no rules are defined.

**Validity rules** are explicit statements about valid values: a number range, a list of allowed values, or a text pattern. Patterns use RE2, which runs in linear time, so a pattern cannot hang the app. The sample dataset starts with four example rules from its documentation; you can remove them or add your own.

Profiles are cached for the browser session, keyed by the file checksum and parsing options. They are never shared across sessions.

## How loading works

REFINE keeps two things, and they are not the same:

1. **The original file.** The uploaded bytes are kept unchanged in memory, with their SHA-256 checksum, for this browser session.
2. **The parsed cell values.** Every cell is stored as text. Nothing is trimmed, no types are guessed, and markers such as `NA` or `null` stay as text. Parsing still has to interpret the file (encoding, delimiter, quoting, Excel's stored values), so the table is not a byte-for-byte copy of the file. Every adjustment appears in the load notes.

### CSV

- Encoding: a UTF-16 byte order mark selects UTF-16. Otherwise REFINE tries UTF-8 (with or without a byte order mark), then Windows-1252, then Latin-1. Any fallback is reported as something to check.
- Delimiter: comma, semicolon, tab, or pipe, chosen by which splits the first rows most consistently. You can override it.
- An empty field (`a,,b`) is empty text. A row that ends early is padded with "no value", and REFINE warns you.
- A row with *more* values than the header is an error, because it usually means an unquoted delimiter that would shift values into the wrong columns. Extra *empty* fields at the end of a row are ignored and reported.
- A quote that is never closed is an error. A stray quote (`"abc"def`) is read leniently and reported.

### XLSX

- Excel stores typed values, so they are written as text with fixed rules:
  - Integers as digits, other numbers in their shortest exact form.
  - `TRUE`/`FALSE`.
  - Dates as `YYYY-MM-DD`, with `HH:MM:SS` when there is a time.
- Display formatting (currency symbols, thousands separators, rounding) is not stored with the value, so it is not reproduced.
- Formulas are not recalculated. REFINE uses the result Excel last saved. Formula cells with no saved result are loaded as empty and reported.
- The first visible sheet loads by default. Hidden sheets can be chosen and are flagged.
- Legacy **.xls** and password-protected workbooks are not supported.

### Limits

| Limit | Value |
|---|---|
| File size | 20 MB |
| Data rows | 200,000 |
| Columns | 300 |
| Cells (rows × columns) | 2,000,000 |
| Unpacked size of an .xlsx | 250 MB |

Files over a limit are rejected with a message, never truncated. Benchmarks on a laptop: a 17.5 MB CSV (200,000 × 10) loads in about 0.4 s, and a 200,000 × 15 XLSX takes about 12 s. Larger inputs have not been benchmarked.

Columns are never dropped. A column with no header gets the name `column_<position>`, even when it has no values (for example from a trailing delimiter), and the load notes list it.

Profiling benchmarks (Apple silicon laptop, Python 3.13, CSV input, same results on pandas 2.2.3 and 3.0.6):

| Dataset | Profile + checks | Peak memory vs. loading |
|---|---|---|
| 200,000 × 10, messy values | 0.4 s | no higher than loading's peak |
| 200,000 × 10, every value unique | 1.2 s | no higher on pandas 3; about +120 MB on pandas 2.2 |
| 6,666 × 300 | 1.4 s | no higher than loading's peak |
| 20,000 × 100, mixed types | 0.4 s | no higher than loading's peak |

## Cleaning pipeline

Every change follows **preview → confirm → apply**, and is recorded so it can be undone, reset or replayed.

- **Operations** (`refine/operations/`) are pure Python. Each validates its settings, resolves every value it depends on into a frozen plan, and applies that plan to return a *new* table. Applying reads only the frozen values, so a replay never recomputes them.
- **CleaningSession** (`refine/session.py`) keeps the original parsed table unchanged, the current table, the active steps and an append-only audit log. Pages never edit a table; they only ask the session for a preview and apply it from the confirmation button.
- **Stale previews are rejected.** A preview is bound to the session, a state version that increases on every apply, undo and reset, and the state's fingerprint. It can be applied once. A rerun, a double click, a preview made before an undo or reset, or one made for a previously loaded file is refused with a message, and nothing changes.
- **Fingerprints** (`refine/fingerprint.py`) are SHA-256 hashes of the table's column names and order, dtypes, row order, values and missing cells (pd.NA and "" are distinct). They do not depend on the upload's checksum, the pandas version (2.2 or 3.x) or how Arrow stores the data.
- **Undo** restores the exact previous state, from a small cache of recent states (up to 5, within a memory budget; see Undo memory) or by replaying the recorded steps from the original; the result is checked against the recorded fingerprint. **Reset** returns to the original parsed table.
- **Recipe**: the active steps with their settings, frozen values and before/after fingerprints, as deterministic JSON without timestamps. Replaying it on the same data reproduces the same table, verified step by step.
- **Row lineage**: each row keeps its original position as an internal identity, stored beside the table rather than as a column, so it cannot clash with your column names and never appears in the data you export.
- **Profiling follows the data.** The profile and quality report are cached by the current state's fingerprint (plus the validity rules), so they refresh after apply, undo, reset or a new upload, and are reused when nothing changed.
- Re-reading or loading another file starts a new cleaning session; the Upload page warns when that would discard applied steps.

### Dates are never assumed day-first or month-first

Dotted dates such as `05.01.2024` are reported as "DD.MM.YYYY or MM.DD.YYYY" and flagged as ambiguous when both readings are valid and differ (5 January or 1 May), like slashed dates. Profiling never converts anything; converting dates requires choosing the order explicitly (see Data types).

## Missing values

REFINE uses one definition everywhere: a cell is **missing** when it has no value or holds empty text. Whitespace-only text and markers such as `NA` or `null` are not missing until you say so.

| Operation | What it does | Frozen in the recipe |
|---|---|---|
| Standardize missing values | Turns the markers you pick (`NA`, `N/A`, `null`, `None`, your own, empty text, whitespace-only text) into no value, in the columns you pick. Shows the exact text found and its counts first; markers are compared after trimming, with case matching on or off. Nothing is preselected. | Markers, options, the exact text matched per column, cell counts |
| Drop rows with missing values | Removes rows where **any** or **all** selected columns are missing, or fewer than a minimum number have a value. Shows the number and share removed and sample rows. At least one row must remain. | Rule, columns, rows removed |
| Drop mostly-missing columns | Removes columns with at least the chosen share of missing values, listing each one with its percentage. At least one column must remain. | Threshold, columns, missing counts and percentages |
| Fill missing values | Fills missing cells with a fixed value, the mean, median, most frequent value, or the previous / next value. | Each column's fill value and cell count |
| Add missing-value indicator | Adds `<column>_missing` (`true` / `false`) right after the column; `_2`, `_3`, ... if the name is taken. | Name, position, count |

Fill rules:

- **Every column keeps its type.** In text columns the mean and median are calculated from values that read as plain numbers (`42`, `-3.5`, `1e3`). `$1,200`, `1,200`, `12%` or `unknown` are not plain numbers. If a column holds any, the step is refused with examples unless you choose to calculate from the plain numbers only. The column is never converted; only missing cells receive the value, written as text, rounded as you choose.
- **Typed columns** (from Data types) are filled with a value of their own type: a fixed value must be valid for the column (`0` for Int64, `true` / `false` for booleans, `2026-01-15` for dates, `2026-01-15 08:00` for date & time), and is refused otherwise. A whole-number column is filled with a mean or median only if it is a whole number, or after you choose to round to 0 decimals. Mean and median are refused for boolean and date columns. Mode, forward fill and backward fill work for every type. The fill value is frozen in the recipe as text and read back with the column's type.
- Means use exactly rounded summation, so they do not depend on row order.
- **Mode ties** go to the value that appears first in the table, with a warning listing the tied values.
- A column with no values cannot be filled with a mean, median or mode; the step is refused.
- **Forward and backward fill** depend on row order, and the preview says so. Cells with nothing above (or below) stay missing and are counted.

## Duplicates

Rows can be matched on **all columns** or on **key columns**, optionally **ignoring case** and **extra spaces** (surrounding spaces, and runs of spaces counted as one). Normalization is for matching only: stored values never change. A cell with no value matches another cell with no value, but not empty text.

- **Find duplicates** only counts: duplicate groups (sets of matching rows), duplicate rows (the extra copies a removal deletes), rows in groups (including the kept ones), percentages, and example groups.
- **Remove duplicate rows** keeps the **first** or **last** row of each group in the current row order and removes the rest; surviving rows keep their order and identity. It is never applied automatically. When rows match on key columns or after normalization, the preview counts removed rows that are not exact copies of the kept row.

## Data types

Every column REFINE loads is text. **Convert data type** turns the columns you select into a real type, with parsing rules you choose and a preview of exactly what converts and what does not. Nothing is converted automatically, and the whole dataset is never converted at once.

| Target | pandas dtype | Notes |
|---|---|---|
| Text | `string[pyarrow]` | For typed columns: values are written as `1250`, `1250.5`, `true`, `2026-01-15`, `2026-01-15 14:30:00` |
| Whole number | `Int64` (nullable) | 64-bit range; overflow is refused |
| Decimal number | `Float64` (nullable) | Binary floating point (see Limitations) |
| True / false | `boolean` (nullable) | Mappings you choose |
| Date | `date32[day][pyarrow]` | A calendar date with no time of day |
| Date & time | `datetime64[us]`, or `datetime64[us, UTC]` | Microsecond precision; no time zone unless you convert offsets to UTC |

Missing cells (no value, or empty text) become the target's missing value. They never become the text `nan`, `None` or `NaT`. Whitespace-only text and markers such as `NA` are not missing, so they fail conversion with a hint to standardize them first.

**Failure policies.** *Refuse the step* (the default) rejects the step when any non-missing value cannot be converted and lists the failed values with the reason for each. *Convert invalid values to missing* replaces them with no value, but only after showing the count, the percentage, examples and a warning; it is refused if no value converts at all. *Keep the original text* is offered only to explain why it is impossible: a typed column cannot also hold text such as `unknown`, so the step is refused rather than silently changing the target type.

Before you preview, the tab shows a read-only table per column (stored type, rows, already missing, would convert, cannot convert, ambiguous) and the failed values with their reasons. The preview then shows the same counts frozen in the plan, the failed values, a before/after sample (failed rows first) and warnings for any information loss.

### Numeric parsing

Plain numbers (`1250`, `-42`, `3.14`, `+7`, `.5`, `1e3`) always parse; surrounding whitespace is ignored and counted. Everything else is opt-in:

| Input | Option | Result |
|---|---|---|
| `1,250` | Thousands separator: comma | 1250 |
| `1.250,50` | Thousands: period, decimal: comma | 1250.50 |
| `1 250`, `1'250` | Thousands: space (also no-break spaces) or apostrophe | 1250 |
| `$1,250.50`, `-$5`, `1250 €` | Read currency symbols (`$ € £ ¥ ₹`) | 1250.50, -5, 1250 |
| `(250.75)` | Read parentheses as negative | -250.75 |
| `12.5%` | Percentages: as a fraction / as the number written | 0.125 / 12.5 |

Thousands groups must have exactly three digits (`1,25,000` is refused), a value may not have two signs, two currency symbols, or a currency symbol and a percent sign. `inf`, `NaN` and other non-finite values are refused. Values that read differently under the other separator convention (`1,250` vs `1.250`) are counted as ambiguous and warned about. Numbers are parsed exactly (as decimals) before conversion:

- **Whole numbers never truncate.** `12.7` is refused unless you choose a rounding rule: *half to even* (2.5 → 2, 3.5 → 4, -2.5 → -2) or *half away from zero* (2.5 → 3, -2.5 → -3). Rounded values are counted and warned about. Values outside the 64-bit range are refused.
- **Decimal numbers** that cannot be stored exactly as Float64 (more than about 15–17 significant digits, such as `3.14159265358979323846`, or integers above 2^53) are counted as precision loss and warned about. Values too large for Float64 are refused.

### Dates and times

REFINE never guesses the order of day and month. You choose one or more formats: **year first** (`2026-01-15`, `2026/01/15`, ISO 8601 with `T`), **month first** (`01/15/2026`) or **day first** (`15/01/2026`, `15.01.2026`) — never both, so `05/06/2026` has exactly one meaning — **month names** (`Jan 15, 2026`, `15 January 2026`, English only), or a **custom** format using `%Y %m %d %H %M %S %f %z`. The chosen formats are frozen in the recipe.

- Dates that would read differently in the other order are counted as **ambiguous** and the warning says which order was used.
- Invalid calendar dates (`2026-02-30`), invalid times, 2-digit years, AM/PM times and sub-microsecond precision are refused with a reason.
- **Date** targets refuse values with a time of day unless you explicitly choose to keep only the date (the number of dropped times is shown). Midnight values convert without loss. Values with a time zone are refused.
- **Date & time** targets keep the time as written and have no time zone. Values with a time zone offset (`Z`, `+02:00`) are refused unless you choose **Convert to UTC**, which then requires an offset on every value and stores the same instant in UTC. The original offset is not kept, and a warning counts the values that were shifted. Nothing is ever shifted silently.

### Booleans

You choose what means true and false: presets (`true/false`, `yes/no`, `y/n`, `t/f`, `1/0`) and your own values. Matching ignores surrounding spaces and, unless you turn on exact case, capitalization. A value listed as both true and false is refused as a conflict; values in neither list are failures. The Phase 4 indicator columns (`true` / `false`) convert with the `true/false` preset.

### Typed columns

Typed columns convert to text, between whole and decimal numbers (the rounding rule applies), and between dates and date & time (time-of-day rule applies). Other combinations are refused with a pointer to convert to text first, so every parsing choice stays explicit. Converting a column to the type it already has is reported as "nothing would change".

## Text

Text rules apply only to text columns you select. Typed columns are not offered: converting them to text is an explicit step on the Data types tab. Missing cells stay missing. If a rule leaves a cell empty (for example trimming a cell of spaces), it becomes empty text, which REFINE counts as missing; the preview counts these.

| Operation | What it does |
|---|---|
| Trim whitespace | Removes leading and trailing whitespace (spaces, tabs, line breaks, no-break spaces). |
| Change capitalization | lowercase, UPPERCASE, Title Case or casefold. Title Case capitalizes the first letter after a start, space, hyphen or slash and lower-cases the rest, so `McDonald`, `O'Neil`, `iPhone` and `USA` change; the preview warns about this. |
| Collapse repeated spaces | `New    York` → `New York`. Only runs of ordinary spaces by default; optionally any whitespace run (tabs, line breaks, no-break spaces) becomes one space. |
| Normalize Unicode | NFC (joins accents) or NFKC (also compatibility forms: `ＡＢＣ` → `ABC`, `①` → `1`, `ﬁ` → `fi`, `²` → `2`); NFKC warns that meaning can change. |
| Remove non-printing characters | Removes control characters (Unicode category Cc), keeping tabs and line breaks unless you choose otherwise, and optionally five invisible formatting characters (zero-width space, word joiner, byte order mark, soft hyphen, Mongolian vowel separator). Letters of every script, accents, emoji and zero-width joiners are kept. |
| Find and replace | Literal text (no regular expressions, so no pattern can run away), matching the whole cell or every occurrence, with or without case. Shows matching cells and the number of replacements. |
| Standardize categories | Maps exact observed values to canonical values, for example `NY`, `new york` and `New  York` to `New York`. The tab lists every distinct value with its frequency and shows suggestions for values that differ only in case or spacing; suggestions are applied only if you add them. Conflicts (one value mapped to two targets, or a canonical value that is itself remapped) are refused. Unmapped values are kept. |

Each text step freezes its settings and the number of cells it changes per column; replay refuses data that gives a different count.

## Undo memory

Undo uses cached earlier states when they fit both a count limit (5) and a memory budget (96 MiB); larger tables keep fewer states, and a state over the budget is never cached. Anything not cached is rebuilt by replaying the recipe from the original and checked against the recorded fingerprint, so undo is always exact.

| Table, six steps that keep every row | Cached before | Cached now | 6 undos |
|---|---|---|---|
| 200,000 × 10 | 5 states, 165 MiB | 2 states, 73 MiB | 0.2 s |
| 6,666 × 300 | 5 states, 120 MiB | 3 states, 72 MiB | 0.1 s |

Operation benchmarks at 200,000 × 10 (same on pandas 2.2.3 and 3.0.6): preview of standardize (10 columns) 0.4 s, remove duplicates 0.25 s, fills 0.02 to 0.1 s, drop rows 0.06 s; replaying ten steps 0.5 s.

Phase 5 benchmarks at 200,000 × 10 (Apple silicon laptop, same on pandas 2.2.3 and 3.0.6; preview / confirmed apply):

| Step | Preview | Apply |
|---|---|---|
| Currency to Float64, 200,000 distinct values (`$12,345.67`) | 0.6 s | 0.01 s |
| Text to date, ISO / month-first (3,000 distinct) | 0.02 s | 0.01 s |
| Text to boolean | 0.01 s | 0.01 s |
| Change case, 2 columns × 200,000 distinct values | 0.8 s | 0.3 s |
| Trim / category mapping (few distinct values) | 0.03 s | 0.02 s |
| Profile after conversion | 0.35 s | |
| Replay 7 mixed steps / undo all 7 | 1.0 s / 1.2 s | |

Parsing works on distinct values, so cost follows the number of distinct values, not rows. A conversion is parsed once per preview and reused by the confirmed apply. Typed columns are fingerprinted by their value bytes. Converting text to numbers shrinks a column (3.4 MiB of currency text → 1.7 MiB Float64). The peak extra memory for a 200,000-distinct-value conversion is about 70 MiB. Recipes hold only settings, counts and up to 8 example failed values per column, never data (1.2 KiB for that conversion).

## Limitations

- **Float64 is binary floating point.** Decimal fractions such as `0.1` are stored as the nearest binary value, and arithmetic can show tiny rounding differences. REFINE warns only when a value cannot be read back exactly. Exact decimal (fixed-point) storage, for example for currency, is outside Phase 5.
- Dates use one day/month order per step: a column mixing `08/12/2025` (month first) and `08.01.2022` (day first) needs the other values standardized first, or *Convert invalid values to missing* for one of the formats. Two-digit years, AM/PM times, non-English month names, named time zones (`America/Chicago`) and leap seconds are not supported. Date & time values are stored to the microsecond.
- Time zones: only fixed offsets (`Z`, `UTC`, `GMT`, `±HH:MM`) are read, only for Convert to UTC, and the original offset is not kept.
- Typed columns convert only along the pairs listed above; anything else goes through text.
- Title Case and NFKC can change names, brands and symbols; both warn, and the before/after examples show exactly what changes.
- Find and replace is literal; regular expressions are not supported.

## Privacy

Uploaded files stay in memory for your browser session only. They are not written to disk, cached across sessions, or shared. REFINE uses explicit rules, not machine learning or AI models.

## Project layout

```text
project3-data-cleaning-engine/
├── app_data_cleaner.py      # Streamlit entry point and navigation
├── refine/                  # Engine: loading, profiling, quality checks (no Streamlit imports)
│   ├── config.py            # Limits and constants
│   ├── errors.py            # LoadError and CleaningError with user-facing messages
│   ├── source.py            # LoadOptions, LoadNote, SourceInfo, LoadedDataset
│   ├── loader.py            # load_file(): format checks, header rules, table build
│   ├── csv_reader.py        # Encoding, delimiter detection, CSV parsing
│   ├── xlsx_reader.py       # Sheet reading, typed value → text rules
│   ├── raw_table.py         # Intermediate records shared by both readers
│   ├── notes.py             # Message formatting helpers
│   ├── profile.py           # profile_dataset(): dataset and column profiling
│   ├── quality.py           # assess_quality(): rule-based checks, validity rules
│   ├── fingerprint.py       # Deterministic table and state fingerprints
│   ├── session.py           # CleaningSession: preview, apply, audit, undo, reset, recipe, replay
│   ├── missing.py           # The missing-value definition, marker scans, plain-number parsing
│   ├── duplicates.py        # Duplicate matching and counts (read-only)
│   ├── conversion.py        # Column kinds, numeric/date/boolean parsing rules, typed columns
│   ├── text.py              # Text rules, distinct-value mapping, category suggestions
│   └── operations/          # Operation framework, registry, and every cleaning operation
│                            #   (convert_ops.py: Convert data type; text_ops.py: text steps)
├── ui/                      # Theme, components, charts, session state, sidebar, Clean-page tabs
│                            #   (clean_forms.py, clean_types.py, clean_text.py, clean_preview.py)
├── views/                   # One file per page
├── sample_data/             # Synthetic messy dataset and its generator
├── tests/                   # pytest suite, including Streamlit AppTest checks
└── .streamlit/config.toml   # Midnight-blue theme, upload limit
```

## Run locally

From this directory:

```bash
pip install -r requirements.txt
streamlit run app_data_cleaner.py
```

Or from the repository root:

```bash
streamlit run project3-data-cleaning-engine/app_data_cleaner.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
