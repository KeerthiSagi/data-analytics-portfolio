# REFINE — Technical Documentation

Detailed reference for REFINE: loading rules, profiling definitions, the cleaning pipeline, every cleaning operation, parsing rules, the before-and-after comparison, memory limits, benchmarks and known limitations. For a short overview, see the [project README](../README.md).

**Status:** Phases 1–9 are complete (file loading; profiling and quality assessment; cleaning pipeline, history and undo; missing values and duplicates; data type conversion and text standardization; outlier detection and treatment; dataset comparison; export of data, records and reports; integration testing, polish and deployment preparation). Engine version 0.9.0. The app is ready for manual browser acceptance testing; see [DEPLOYMENT.md](DEPLOYMENT.md).

## Contents

- [Pages and features](#pages-and-features)
- [Profile & Quality](#profile--quality)
- [How loading works](#how-loading-works)
- [Cleaning pipeline](#cleaning-pipeline)
- [Missing values](#missing-values)
- [Duplicates](#duplicates)
- [Data types](#data-types)
- [Text](#text)
- [Outliers](#outliers)
- [Compare](#compare)
- [Export](#export)
- [Undo memory](#undo-memory)
- [Moving between pages](#moving-between-pages)
- [Errors](#errors)
- [Limitations](#limitations)
- [Privacy and security](#privacy-and-security)
- [Project layout](#project-layout)
- [Run locally](#run-locally)
- [Tests](#tests)
- [Deployment](DEPLOYMENT.md)

## Pages and features

- Upload a **.csv** or **.xlsx** file, or open the built-in synthetic sample.
- See how the file was read: encoding, delimiter or sheet, row and column counts, and the file's SHA-256 checksum.
- **Load notes** list every interpretation or adjustment the loader made, such as a removed byte order mark, renamed duplicate columns, padded short rows, skipped blank lines, or Excel formulas without saved results. Notes that need your attention are marked **Check**.
- Re-read the file with a different encoding, delimiter, sheet, or number of title rows to skip.
- Preview the first 100 rows exactly as parsed.
- **Profile & Quality** page: dataset overview, a profile of every column, rule-based quality checks with counts and denominators, column inspection, optional validity rules, and the load notes.
- **Clean** page, in tabs: **Columns** (drop columns), **Missing values** (standardize markers, drop rows, drop mostly-missing columns, fill, add an indicator column), **Duplicates** (find and remove), **Data types** (convert to text, whole numbers, decimals, true/false, dates, dates with times) **Text** (trim, capitalization, repeated spaces, Unicode, non-printing characters, find and replace, category mapping) and **Outliers** (IQR, z-score or percentile detection in a numeric column; cap, replace with missing, replace with a value, or remove rows). Every step shows its exact effect (counts, percentages, warnings, a before-and-after sample) and is applied only after you confirm it in a dialog.
- **Compare** page: the original data next to the current data. Dataset metrics, missing values per column, duplicates, added, removed and retyped columns, quality findings, cell-level changes matched by row identity, per-column statistics, and a summary of the applied steps. Read-only.
- **History** page: the applied steps, a full audit log (applied, undone, reset), undo of the last step, reset to the original with confirmation, and the reproducible recipe with a replay check.
- **Export** page: download the current data as CSV or Excel, the cleaning history and recipe as JSON, and a data-quality report and a comparison report as JSON. Read-only.

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

## Outliers

**Treat outliers** (`treat_outliers`) works on one column at a time. Detection is in `refine/outliers.py` (read-only); the operation is in `refine/operations/outlier_ops.py`.

### Eligible columns

Only whole-number and decimal columns: `Int64`, `Float64`, and plain numpy integer and float columns. Boolean columns are excluded, even though pandas can treat them as numbers. Text columns are never parsed: every column REFINE loads is text, so convert numbers on the Data types tab first. If the table has no eligible column, the tab says so.

### Detection methods

A value is an outlier when it is **strictly** outside the boundaries: `value < lower` or `value > upper`. A value equal to a boundary is not an outlier.

| Method | Lower boundary | Upper boundary | Parameter (default) |
|---|---|---|---|
| IQR | Q1 − k × IQR | Q3 + k × IQR | k, the multiplier: more than 0, at most 100 (1.5) |
| Z-score | mean − t × sd | mean + t × sd | t, the threshold: more than 0, at most 100 (3.0) |
| Percentile | value at the lower percentile | value at the upper percentile | 0 ≤ lower < upper ≤ 100 (1 and 99) |

- **Quartiles and percentiles** use linear interpolation: for n sorted values, the q-th quantile is at position (n − 1) × q, between the two nearest values. This is numpy's `linear` method, the default in numpy and pandas. REFINE calls numpy directly so the result does not depend on the pandas version.
- **Standard deviation** is the population standard deviation (ddof = 0). The z-score rule is applied as boundaries (mean ± t × sd), which is |z| > t written so that preview, apply and replay use the same comparison. At a value exactly on a boundary, the two forms can differ only by floating-point rounding. The values are scaled by their largest magnitude before squaring, so very large values do not overflow.
- The mean and standard deviation include the outliers themselves, so one extreme value inflates the standard deviation and can hide others. IQR and percentiles are less affected.

### Edge cases

| Case | Result |
|---|---|
| Missing cells | Never outliers and not part of the statistics. Counted separately ("Missing values"). |
| Empty column, all missing | Refused: there is nothing to check. |
| Fewer than 3 values | Refused: the statistics would not mean anything. |
| Constant column | Zero outliers. For the z-score, a constant column has sd exactly 0 (no rounding), so nothing is divided by zero. |
| IQR = 0 with differing values (at least half the values equal) | The rule is applied as stated: both boundaries equal that value, so every other value is an outlier. The summary and preview warn. |
| Identical percentile boundaries (many ties) | Same as above, with a warning. With ties, the share detected does not have to match the percentile tails. |
| Small samples (z-score) | With n values, no \|z\| can exceed √(n − 1). If the threshold is at least that, the summary explains that no outlier can be found. |
| Non-finite values (inf, or NaN that is not a missing value) | Refused with a message. They are never dropped silently. REFINE's own typed columns cannot hold them, because Convert data type refuses `inf` and `NaN`. |
| Boundaries too large to represent (values near ±1.8e308) | Refused. |
| No outliers | Reported as "nothing would change"; no step or history entry is created. |
| Duplicate index labels | Rows are selected by position, never by label. |

### Treatments

| Treatment | What changes | Dtype |
|---|---|---|
| Cap at the boundaries | Values below the lower boundary become the lower boundary; values above the upper become the upper. | Kept, except that a whole-number column capped at a fractional boundary becomes `Float64` (the boundary is never rounded) and the preview warns. This is refused if the column holds whole numbers above 2^53, which Float64 cannot store exactly. |
| Replace with missing values | Outliers become missing. Existing missing values stay missing. | Kept (`Int64` and `Float64` use pd.NA; numpy float uses NaN). A numpy `int64` column, which cannot hold a missing value, becomes `Int64`. Missing values in the result work with every missing-value operation. |
| Remove rows | Rows holding an outlier in the column are removed, with all their columns. Rows where the column is missing are kept. Remaining rows keep their order and identity. | Kept. Removing every row is refused. |
| Replace with a fixed value | Outliers become the number you enter. | Kept. The value must be a plain number (`0`, `42`, `-3.5`, `1e3`). Text, `NaN`, `inf` and `-inf` are refused. A fractional value for a whole-number column is refused, not rounded. A value that is itself outside the boundaries is warned about. |

Only the selected column changes (or, for Remove rows, the row count). Other columns, the column order and the row order are not changed.

### Preview, confirmation and replay

The tab shows a read-only detection summary before any preview: lower and upper boundaries, the number of outliers and their share of the values checked, missing values, and the method's statistics (Q1, Q3 and IQR; mean and standard deviation; or the percentile values). The preview then shows the frozen detection table, the treatment, the number of cells changed or rows removed, warnings, and a before/after sample in which low and high outliers both appear.

**Replay contract.** As with every operation, `resolve()` works out the boundaries once, from the table the preview was made for, and freezes them in the plan with the method, its parameters, the treatment, the replacement value, the column's dtype before and after, and the numbers of values below and above. `apply()` never recalculates the boundaries. It finds the values outside the frozen boundaries and refuses (`replay_mismatch`) data that gives different counts or has a different dtype. So undo, replay and the recipe reproduce exactly the change shown in the preview, and the before/after fingerprints are checked as for every step. A second outlier step on the treated data calculates new boundaries from that data and freezes those.

A preview cannot be applied to data that has changed since it was made (another step, undo or reset): the session rejects it as stale and the page asks for a new preview. If the settings on the tab no longer match the preview, the page shows "Selection changed" instead of the Apply button.

## Compare

The Compare page puts the original parsed table (as loaded, before any step) next to the current table (after the active steps). It only reads the cleaning session: it has no buttons that change data, it adds nothing to the history, and it never changes the session version, fingerprints, undo cache or recipe. Its toggles and the column selector only filter what is shown.

The engine is `refine/compare.py`. `CleaningSession.compare()` passes it the stored tables and row lineage without copying them; `compare_datasets(original, current, row_ids=...)` can also be called directly with any two tables.

### Definitions

No metric is defined anew. Every number comes from the same code as the Profile & Quality page, run once on each table.

| Section | What is compared | Source |
|---|---|---|
| Dataset overview | Rows, columns, cells (rows × columns), missing cells and their share of cells, duplicate rows, and the number of numeric, text, date/time (and, when present, true/false and other) columns | `profile_dataset`; column kinds from `refine/conversion.py` |
| Missing values | Per column: missing count and percentage in each table, and the difference | `ColumnProfile.missing_count` |
| Duplicates | Duplicate rows and duplicate groups in each table | `DatasetProfile.duplicate_rows` / `duplicate_groups` |
| Columns and data types | Kept, added and removed columns; kept columns whose dtype changed; a change in column order | dtypes, named as in recipes (`dtype_name`) |
| Data quality | Findings by severity, checks not assessed, and each finding (check × column) with its count and denominator in both tables | `assess_quality`, with the same validity rules for both tables |
| Value changes | Cell by cell, for kept rows and kept columns | Row lineage (below) |
| Column details | Count, minimum, maximum, mean, median and standard deviation; distinct values, missing cells, cells with a value, and the 5 most frequent values | `ColumnProfile` |

- **Missing** means no value or empty text, as everywhere in REFINE. Null-like text such as `NA` and whitespace-only text are not missing.
- **Missing-value denominators.** Each table's percentages use its own row count (or cell count for the overview). The two tables often have different row counts, so a column can keep the same missing count while its percentage changes. A column that exists in only one table shows "Not in table" for the other, with no difference.
- **Duplicate rows** are rows that repeat an earlier row exactly in every column; the first occurrence is not counted. This is the Profile page's rule. Duplicates found on the Clean page with key columns, ignoring case or ignoring spaces can be more and are not used here. When the rows cannot be compared (for example a table with no columns), the count is "Not assessed" and has no difference.
- **Column types** are storage types. Every loaded column is stored as text, so the original table normally has only text columns; numeric and date counts rise only when columns are converted. A type change is reported, not judged.
- **Directions.** Every difference is shown as an increase or decrease (or no change). Fewer missing cells, for example, may come from filling values, which is not always what the analysis needs, so the page never calls a change an improvement.
- **Quality findings** are matched by check and column. Status is *new*, *no longer detected*, *changed* (count or denominator differs) or *unchanged*. A finding is "changed" when only its denominator moved, for example after rows were removed. "No longer detected" means the check no longer finds the problem, not that the data is correct. There is no overall quality score, as on the Profile page.
- **Numeric statistics** are the profile's: values stored as numbers, or text that reads as a plain number (`$1,200` and `12%` are not used). The standard deviation is the sample standard deviation (n − 1) and is n/a with fewer than two values; a constant column has a standard deviation of 0. Statistics exist only for columns the profile treats as numeric.
- **Categorical statistics** are the profile's distinct count (missing cells excluded), missing count, and top values, which the profile already limits to 10 per column, so high-cardinality columns add no extra work.

### Row matching

Cells are compared only through **row lineage**. Each row of the original table has an identity, its 0-based position in the parsed table, and every operation carries it through (see Cleaning pipeline). For current row *i*, `row_ids[i]` names the original row it came from.

- The original column is read at those positions only (`take`), so a removed row is never compared with the next row that moved up. Removed rows are counted separately and never appear as changed cells.
- The tables' pandas index labels are never used, so duplicate index labels cannot cause mismatches.
- Equal row counts are not treated as evidence that rows correspond. Without valid lineage there is no cell-level comparison: the page says so and shows only the aggregate comparisons above. Lineage is valid when there is exactly one whole-number id per current row, every id is a row of the original table, and no id repeats.
- REFINE operations only keep or remove rows, so there are never added rows, and the kept rows keep their order. If ids were reordered, cells would still be matched by identity.

Each changed cell is classified once:

| Class | Meaning |
|---|---|
| Modified | Had a value, still has one, and it differs |
| Filled | Was missing, has a value now |
| Made missing | Had a value, is missing now |

Two cells with no value are equal; no value and empty text are different stored values (so `""` → no value counts as modified, although both are missing). When a column's dtype changed, cells are compared as REFINE writes them as text (whole numbers as digits, decimals in shortest form, `true`/`false`, ISO dates): `"42"` → 42 is unchanged, `"76.0"` → 76 and `" 7"` → 7 are modified. In columns of mixed Python objects, values of different types (`"1"` and `1`, or `True` and `1`) are different values. Up to 5 changed cells per column are kept as examples, with their original row number.

### Cleaning history

The page lists the active steps in order from the session's own records: operation, affected columns, the step's summary, its settings (lists over 5 items and text over 60 characters are shortened), the counts recorded when it was applied, and rows and columns before and after. Fingerprints and frozen resolved values are not shown; the History page has the full audit log and recipe. Undone steps are not listed, and nothing is inferred from differences between the tables.

### Caching and session state

The comparison is cached in the browser session, keyed by the original and current state fingerprints, the profiler and comparison versions, and the validity rules. Apply, undo, reset, loading another file or editing the rules changes the key, so the page always reflects the current data; reruns with unchanged data reuse the result. Only the latest comparison is kept. The current table's profile and quality report are the ones the Profile & Quality page caches. The original table's profile and report are cached separately, so after a new step only the current side is profiled again. They are also kept when the Profile & Quality page profiles the data before any step is applied (or after every step is undone), since that table is the original. A comparison the Export page built for a report download is reused when the Compare page is opened for the same state. The cache holds profiles and counts, never copies of the tables.

### Performance

Benchmarks (Apple silicon laptop, Python 3.13.3; same within 0.2 s on pandas 2.2.3 and 3.0.6), after four steps that remove about 4% of the rows, standardize markers in every column and convert one column to Int64:

| Table | Cell comparison | Comparison with cached profiles | First comparison (profiles both tables) |
|---|---|---|---|
| 200,000 × 10 | 0.07 s | 0.06 s | 0.35 s |
| 6,666 × 300 | 0.39 s | 0.18 s | 3.3 s |

Peak Python-heap growth during the cell comparison was 6 to 8 MiB at 200,000 × 10 (tracemalloc; Arrow buffers are not counted). Columns are compared one at a time, so the extra memory is about one column's worth of matched original values.

## Export

The Export page offers six downloads of the current confirmed state. It works before any cleaning step (the files then hold the data as loaded and an empty history) and only reads the session.

| Download | File name | Type | Built from |
|---|---|---|---|
| Cleaned data, CSV | `<file>_cleaned.csv` | `text/csv` | `CleaningSession.data` |
| Cleaned data, Excel | `<file>_cleaned.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `CleaningSession.data` |
| Cleaning history | `<file>_history.json` | `application/json` | The audit log and active steps |
| Recipe | `<file>_recipe.json` | `application/json` | `CleaningSession.recipe()`, unchanged |
| Data-quality report | `<file>_quality_report.json` | `application/json` | The comparison's profiles and quality reports |
| Comparison report | `<file>_comparison_report.json` | `application/json` | `DatasetComparison` (refine/compare.py) |

With no steps applied the data files are named `<file>_original.*`. `<file>` is the uploaded file's name reduced to ASCII letters, digits, `-` and `_` (at most 60 characters, `dataset` if nothing is left). It is only a download name: no path is built from it and nothing is written to disk.

The code is in `refine/export.py` (CSV, history, recipe, JSON, file names), `refine/xlsx_writer.py` (the workbook) and `refine/reports.py` (the two reports). The page (`views/export.py`) and its download definitions (`ui/exports.py`) contain no serialization logic.

### CSV

- UTF-8 without a byte order mark, comma-separated, CRLF line endings (RFC 4180), a header row, no index column. Fields are quoted only when they contain a comma, a quote or a line break; a row with a single empty field is written as `""` so it never becomes a blank line.
- Rows and columns are in table order. Every value is written as REFINE writes it as text, the same text *Convert data type → Text* produces: text unchanged, whole numbers as digits (no rounding, including 64-bit values beyond 2^53), decimals in their shortest exact form (`76.0`, `0.1`, `1e-07`), `true` / `false`, dates `2026-01-15`, date-times `2026-01-15 14:30:00[.ffffff]`, with `+00:00` for UTC columns.
- CSV has no types: loading the file again gives text columns with the same values. Missing cells and empty text are both written as an empty field, so that distinction is lost.
- Without BOM, some versions of Excel read non-ASCII text in a CSV as another encoding. The Excel download avoids this.

**Formula-like text.** Spreadsheet apps can run a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return as a formula (CSV injection). A value counts as formula-like when it starts with `=`, `@`, a tab or a carriage return, or with `+` or `-` followed by more text that is not a plain number. So `-12.5`, `+3` and a lone `-` are not formula-like; `=SUM(A1)`, `+1 555 0100` and `-2+3` are. Typed columns (numbers, dates, booleans) never are.

The policy:

- By default the CSV holds every value exactly. When the current data has formula-like cells, the page says how many and in which columns.
- The *Escape formula-like text* switch writes each such cell (and header) with a leading apostrophe (`'=SUM(A1)`), which spreadsheet apps show as text. That changes those values in the file, and the page says how many cells start with an apostrophe. The data in REFINE is not changed.
- The Excel download needs neither: it never writes a cell as a formula.

### Excel

The workbook has one worksheet, *Cleaned Data*, with a bold, frozen header row and no index column. It is written by `refine/xlsx_writer.py` as a zip of a few XML parts, built in memory with the standard library. openpyxl and `pandas.to_excel` were not used because they write each worksheet to a temporary file on disk before zipping it, and REFINE does not write uploaded data to disk. No new dependency is needed; the tests read every workbook back with openpyxl and `pandas.read_excel`. The same table always gives the same bytes.

| Stored as | Written as |
|---|---|
| Text | Text (inline string). Never a formula: no cell has a formula element, so `=SUM(A1:A9)` opens as text |
| Int64 | Number; with 16 or more digits, text (Excel keeps 15 digits) |
| Float64 | Number, shortest exact form; `inf` / `-inf` as text |
| boolean | Excel boolean |
| date | Date, format `yyyy-mm-dd` |
| date & time | Date-time, `yyyy-mm-dd hh:mm:ss`, or `.000` when the column has fractions of a second |
| date & time (UTC) | The UTC time, without a zone (Excel has none) |
| Other storage types | Text, cell by cell |
| Missing | Empty cell |

What the workbook holds differently is counted before download, with the writer's own rules (a test checks the counts agree), and listed on the page when it applies:

- Excel keeps 15 significant digits. Decimals that need more are written exactly but rounded by Excel when it opens the file.
- Excel shows times to the millisecond; microseconds are counted.
- Dates before 1 March 1900 are written as ISO text, because Excel's 1900 date system cannot hold them correctly.
- Text longer than 32,767 characters (Excel's cell limit, counted in UTF-16 units) is cut to that length without splitting a character.
- Control characters other than tab, line feed and carriage return cannot be stored in an .xlsx file and are replaced with `�` (U+FFFD). The CSV keeps them.
- Excel reads text such as `_x0041_` as an escape for another character, so the underscore of such text is escaped (`_x005F_`) and Excel shows the text as written. openpyxl and `pandas.read_excel` do not decode these escapes and show `_x005F_x0041_`.
- A row whose cells are all missing is an empty row. It keeps its place, but readers stop at the last row with a value, so trailing empty rows are not read back.

A worksheet holds 1,048,576 rows including the header, and 16,384 columns. REFINE's limits (200,000 rows, 300 columns, plus at most one indicator column per column) are far below this; a table that does not fit gets a message and a disabled Excel button.

### Cleaning history

JSON (`format: "refine-history"`, `history_version: 1`), from the session's own records; nothing is reconstructed:

- `source`: file name, format, SHA-256, rows and columns as loaded.
- `state`: steps applied, and the rows, columns and fingerprint of the original and current data.
- `active_steps`: the steps that make up the current data, in order: operation, label, summary, affected columns, the settings as chosen, the counts recorded when the step was applied, warnings shown before applying, rows and columns before and after, the time it was applied (UTC) and its audit sequence number.
- `events`: the full audit log, including undo and reset. Each applied step has a `status`: `active`, `undone` (with `undone_by`) or `discarded by reset`.

Frozen resolved values and per-step fingerprints are in the recipe. The history holds no table contents, cached states, row lineage or uploaded bytes. With no steps it is a valid document with empty lists.

### Recipe

The download is `CleaningSession.recipe()` written with `recipe_json(indent=2)`: the same JSON the History page shows, with no field added, removed or recomputed. That includes the recipe and fingerprint versions, the source and result fingerprints, each step's settings, its frozen resolved values (for outlier steps, the boundaries, statistics and counts calculated when the step was previewed) and its before and after fingerprints. Export never recalculates anything.

`replay_recipe(original, recipe)` (in `refine.session`) rebuilds the cleaned table from the parsed original file and the parsed JSON, checking the source fingerprint and every step's fingerprints; the tests replay downloaded recipes, including outlier steps, and compare the result with the session's data. A recipe replays only on the same parsed table, and needs the same recipe version (1) and fingerprint version (`refine-fp-1`); the engine version is recorded but not checked. REFINE has no page for importing a recipe yet.

### Reports

Both reports are versioned JSON (`report_version: 1`) built from one `DatasetComparison`, the object the Compare page shows. When the Compare page has already computed it for the current data and rules, the reports reuse it. Otherwise they build one from whatever profiles and quality reports are cached, once for both reports. No metric is defined anew, there is no overall score, and changes are given as increase, decrease or no change.

**Data-quality report** (`refine-quality-report`): for the original and the current data, the rows, columns, cells, missing cells and their share, null-like and whitespace-only cells, duplicate rows and groups, findings by severity, every finding (check, column, severity, count, denominator, percentage, explanation, suggested action, the check's examples), checks not assessed, and per column the dtype, kind, inferred type, missing, null-like and distinct counts, numeric statistics and up to 5 most frequent values. Also the validity rules, the overview changes, findings by status (new, no longer detected, changed, unchanged), the definitions used and the quality-check caveat.

**Comparison report** (`refine-comparison-report`): the overview, missing values per column, duplicates, kept, added, removed and retyped columns and column order, quality findings in both tables, the cell-level summary, column statistics, the applied steps, and a list of limitations. The cell-level summary uses row lineage only (`matched_by`). When lineage is missing or invalid, `available` is false, the reason is given and no cell counts are reported. Changed-cell examples are the first 5 per column, as shown on the Compare page, with `examples_complete` saying whether they are all of them; the counts always cover every cell.

Neither report contains the tables: only counts, statistics, top values and the examples above. On a 6,666 × 300 table each is about 1 MiB.

### Session state and reruns

- **Read-only.** No download changes the original or current data, the session version, history, audit log, undo cache, recipe, row lineage or fingerprints. Tests check all of these before and after every download, and that undo and replay still work exactly afterwards.
- **Always current, never stale.** Nothing is cached as file bytes. Each file is built when its button is clicked (Streamlit's deferred `download_button` data, run in a separate thread), from the session at that moment. The builder is bound to the session version the page showed and refuses if the data changed in between, so a file never disagrees with the page. After apply, undo or reset the page is redrawn with the new state.
- **Pending previews.** A previewed step is not applied. The files hold the confirmed data, and the page says a preview is waiting. Download buttons do not rerun the page (`on_click="ignore"`), and the page does not touch the preview, so it can still be applied afterwards.
- **Reports' comparison.** Both reports use one comparison, held in a slot keyed like the Compare page's cache (session, both fingerprints, profiler and comparison versions, rules). The first report download fills it; the other report, later draws of the page (for example after switching formula escaping) and the Compare page reuse it. Any apply, undo, reset or rules change gives a new key and a new, empty slot.
- **Per rerun.** Drawing the page costs one read-only check of the current data (the counts behind the CSV and Excel notes), cached by fingerprint: 0.1 s at 200,000 × 10 and 0.3 s at 6,666 × 300. Reports and files are not computed until clicked.

### Performance

Measured on an Apple silicon laptop, Python 3.13.3, after realistic steps. Tall table: 200,000 × 10 synthetic customers with about 4% duplicates, after removing duplicates (192,000 rows left), standardizing markers, converting three columns to Int64 and Float64 and one to dates, and trimming. Wide table: 6,666 × 300, half text and half decimals as text, after standardizing markers in 150 columns and converting 50 to Float64. pandas 2.2.3 and 3.0.6 were within 0.5 s of each other; the pandas 3.0.6 times are shown.

| | Tall: time | Tall: size | Wide: time | Wide: size |
|---|---|---|---|---|
| CSV | 2.0 s | 14.9 MiB | 2.2 s | 10.9 MiB |
| CSV, formulas escaped | 2.1 s | 15.0 MiB | 2.6 s | 10.9 MiB |
| Excel | 9.2 s | 10.3 MiB | 6.8 s | 10.5 MiB |
| History / recipe | < 0.01 s | 10 KiB | 0.01 s | 30–50 KiB |
| Quality / comparison report, comparison cached | < 0.01 s | 40 KiB | 0.1 s | 1.0–1.1 MiB |
| Comparison, nothing cached (first report; tracemalloc on, see Phase 9 below) | 2.0 s | | 13 s | |

Rows are converted and written in chunks of about 50,000 cells (Excel) or 200,000 cells (CSV), so the peak Python-heap growth while building a file was about 20 MiB for either table and format (tracemalloc; Arrow buffers are not counted). The CSV is written straight into a byte buffer. The current table is copied once per click (`CleaningSession.data`). Streamlit keeps a generated file in memory only until the page is drawn again. A report built without cached profiles profiles both tables, as the Compare page does on first use; on wide tables this is the slowest download.

**Phase 9 measurements.** The Phase 8 times above were taken with tracemalloc running, which slows Python-heavy code several times (the wide first report took 16 s with it and 4.1 s without). Re-measured without it, on new synthetic tables of the same shape (tall: 200,000 × 10 with 8,000 duplicates, after six steps; wide: 6,666 × 300 with unique text values and decimals as text, after three steps), on the same laptop; pandas 2.2.3 and 3.0.6 within 0.15 s:

| | Tall | Wide |
|---|---|---|
| CSV | 0.4 s | 0.9 s |
| Excel | 1.8 s | 3.1 s |
| First report, Compare page not opened, before Phase 9 | 1.05 s | 4.1 s |
| First report after Phase 9 (original profiled on the Profile page before cleaning) | 0.53 s | 2.0 s |
| Second report, or a report after the Export page reruns | 0.001 s (before Phase 9: recomputed) | 0.002 s (before: 4.1 s) |

The reports are byte-for-byte the same either way (apart from the export time). Peak Python-heap growth while building the first report stayed at 30–36 MiB. The writers were not changed in Phase 9.

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

Phase 6 benchmarks at 200,000 × 10 (Apple silicon laptop, same on pandas 2.2.3 and 3.0.6), Float64 column with 2,000 missing cells: detection 0.003 s for each method; preview / confirmed apply 0.01 s / 0.01 s for cap, replace with missing and replace with a value, 0.01 s / 0.02 s for remove rows; replaying the step 0.03 s. Peak Python-heap growth during preview and apply was 11 MiB (tracemalloc; Arrow buffers are not counted).

Parsing works on distinct values, so cost follows the number of distinct values, not rows. A conversion is parsed once per preview and reused by the confirmed apply. Typed columns are fingerprinted by their value bytes. Converting text to numbers shrinks a column (3.4 MiB of currency text → 1.7 MiB Float64). The peak extra memory for a 200,000-distinct-value conversion is about 70 MiB. Recipes hold only settings, counts and up to 8 example failed values per column, never data (1.2 KiB for that conversion).

## Moving between pages

Streamlit clears a page's widget values when you switch to another page. Every setting on the Clean page is therefore kept for the browser session (`persist_state="session"`), so a step previewed on Clean can be checked on Compare or Export and still applied afterwards: the form shows the same settings and the preview is still the current one. Widget keys include the session's state version, so after apply, undo or reset the forms still start fresh. The Clean page opens on its first tab; when a preview is waiting, a notice at the top names the step and its tab. Compare and Export say when a previewed step is waiting and that their numbers and files leave it out. Navigating never applies or discards a preview; a preview is discarded only by its Discard button, by a change of settings, or when the data changes underneath it (another step, undo or reset), and the Clean page says so.

## Errors

- **Expected problems** (a file that cannot be read, invalid settings, a step that would change nothing, a stale preview) are `LoadError` or `CleaningError` with a plain message saying what to change. Nothing is loaded or changed when they occur; the previous dataset and session stay as they were.
- **Atomic steps.** `CleaningSession.apply` builds the new state completely before it changes anything, so any failure during a step, expected or not, leaves the data, history, version and fingerprints as they were (a test injects a failure to check this).
- **Unexpected errors** are not caught and relabelled. With `client.showErrorDetails = "type"` in `.streamlit/config.toml`, the browser shows only the exception type and a generic message; the full traceback goes to the server console (and the Community Cloud logs). Run with `--client.showErrorDetails=full` to see tracebacks in the browser while debugging.
- **Downloads.** A file that cannot be built shows Streamlit's generic "Failed to generate file for download" and is logged on the server. A download made after the data changed (for example from another click in between) is refused rather than built from different data; the page has already been redrawn by then.

## Limitations

- **Float64 is binary floating point.** Decimal fractions such as `0.1` are stored as the nearest binary value, and arithmetic can show tiny rounding differences. REFINE warns only when a value cannot be read back exactly. Exact decimal (fixed-point) storage, for example for currency, is not supported.
- Dates use one day/month order per step: a column mixing `08/12/2025` (month first) and `08.01.2022` (day first) needs the other values standardized first, or *Convert invalid values to missing* for one of the formats. Two-digit years, AM/PM times, non-English month names, named time zones (`America/Chicago`) and leap seconds are not supported. Date & time values are stored to the microsecond.
- Time zones: only fixed offsets (`Z`, `UTC`, `GMT`, `±HH:MM`) are read, only for Convert to UTC, and the original offset is not kept.
- Typed columns convert only along the pairs listed above; anything else goes through text.
- Title Case and NFKC can change names, brands and symbols; both warn, and the before/after examples show exactly what changes.
- Find and replace is literal; regular expressions are not supported.
- The comparison matches columns by name. A column removed and later re-added under the same name is compared with the original column of that name.
- Cell-level comparison needs row lineage. Tables compared without it (for example two files loaded separately) get only the aggregate comparisons.
- Duplicate counts on the Compare page are exact all-column duplicates only; key-column and normalized matching is available on the Clean page.
- CSV downloads do not keep column types, and write missing cells and empty text the same way. Escaping formula-like text changes those values in the file.
- Excel downloads keep 15 significant digits, show times to the millisecond, have no time zones, cut text over 32,767 characters, replace control characters with `�`, and write dates before 1 March 1900 as text (see Export).
- The first report download profiles whichever table has not been profiled yet. Without tracemalloc this took 0.5–1 s at 200,000 × 10 and 2–4 s at 6,666 × 300 (see Export, Performance); later downloads reuse the result.
- Recipes can be downloaded and replayed with `refine.replay_recipe`, but there is no page for importing one.
- Outlier boundaries are shown in their shortest exact form, so z-score and percentile boundaries can read like `20.697472847070387` in the step summary, history and reports. This keeps the audit exact; the values are not rounded for display.
- Section titles are level-4 headings directly under the page's level-1 title. Screen readers can navigate them, but the levels skip; changing them needs a visual check in a browser.
- REFINE has been tested with Python 3.13 only.
- Outlier detection works on one column at a time and uses only the three rule-based methods above; there is no multivariate or model-based anomaly detection. Values are compared as 64-bit floats, so whole numbers above 2^53 are compared with rounding. The z-score's mean and standard deviation include the outliers themselves.

## Privacy and security

Uploaded files stay in memory for your browser session only. They are not written to disk, cached across sessions, or shared. REFINE uses explicit rules, not machine learning or AI models.

Reviewed in Phase 9 against the code:

- **In memory only.** Uploads are read from Streamlit's in-memory upload, parsed from `io.BytesIO`, and every download is built in a `BytesIO` buffer. No module writes a file, creates a temporary file or uses a database. The Excel writer is REFINE's own for this reason: openpyxl's writer and `pandas.to_excel` use temporary files.
- **Isolation between users.** All data lives in `st.session_state`, which is per browser session. Nothing uses `st.cache_data` or `st.cache_resource`, and there is no module-level store of data. The conversion memo is thread-local and answers only for the same table object with the same column content.
- **Untrusted files.** Size, row, column and cell limits are checked before or while parsing. An .xlsx file's uncompressed size is checked before it is opened (zip bombs). Legacy .xls, encrypted workbooks and files whose content does not match their extension are refused. Formulas in workbooks are never evaluated; their saved values are read.
- **File names** are used only for display and download names. The display copies are HTML-escaped; download names are reduced to ASCII letters, digits, `-` and `_`. No path is ever built from them.
- **HTML.** Every piece of text from a file that goes into custom HTML (cards, callouts, the sidebar) is escaped.
- **Formula injection.** Excel downloads never contain formulas. CSV downloads keep values exactly by default, warn when cells could run as formulas, and can escape them (see Export).
- **Serialization.** Recipes and reports are plain JSON (no NaN or Infinity); nothing is loaded with pickle or evaluated.
- **Logging.** REFINE logs nothing itself. Streamlit logs unexpected exceptions on the server; an exception message can quote a value from the data.
- **Error details** shown in the browser are limited to the exception type (see Errors). Streamlit's XSRF and CORS protection stay on.
- **Memory.** One session holds the original bytes, the parsed table, the current table, up to 96 MiB of undo states, one profile and quality report per side, and one comparison. Very wide or tall uploads near the limits use several hundred MB at peak while parsing (see Limits); a public deployment on a small instance can run out of memory with several such sessions at once. This is a deployment limit, not something REFINE can enforce per user.
- **Not addressed:** there is no authentication, rate limiting or per-user quota; anyone with the URL of a public deployment can use it.

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
│   ├── outliers.py          # Outlier detection: eligible columns, IQR, z-score, percentile (read-only)
│   ├── compare.py           # compare_datasets(): original vs current, row matching by lineage (read-only)
│   ├── export.py            # CSV, history and recipe downloads, formula-like text, JSON, file names
│   ├── xlsx_writer.py       # In-memory .xlsx writer and the count of values Excel holds differently
│   ├── reports.py           # Data-quality and comparison reports (JSON)
│   └── operations/          # Operation framework, registry, and every cleaning operation
│                            #   (convert_ops.py: Convert data type; text_ops.py: text steps;
│                            #    outlier_ops.py: Treat outliers)
├── ui/                      # Theme, components, charts, session state, sidebar, Clean-page tabs
│                            #   (clean_forms.py, clean_types.py, clean_text.py, clean_outliers.py,
│                            #    clean_preview.py) and the Export page's downloads (exports.py)
├── views/                   # One file per page
├── sample_data/             # Synthetic messy dataset and its generator
├── docs/                    # This document and DEPLOYMENT.md (deployment checklist)
├── tests/                   # pytest suite, including Streamlit AppTest checks
└── .streamlit/config.toml   # Midnight-blue theme, upload limit, browser error details
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

The suite has 1,029 tests, including Streamlit `AppTest` checks of the pages, and passes on pandas 2.2.3 and 3.0.6 with Streamlit 1.65.0, and on pandas 3.1.1 with Streamlit 1.67.0 (Python 3.13.3). The AppTest fixture turns full error details back on, so a failing page test shows its message. Phase 9 added `test_integration_workflows.py` (complete workflows from a messy file through every cleaning kind to all six downloads, checking that the profile, comparison, CSV, Excel, history, recipe and reports agree; undo and reset; recipe replay with frozen outlier boundaries; a failure injected during a step; row lineage; edge cases from empty files to infinite values) and `test_integration_pages.py` (a preview kept across Compare and Export and applied afterwards, discarded previews, stale previews after undo, caches shared between pages and refreshed after undo and reset, every page after a full workflow, and the error-detail setting). Phase 8 added `test_export_csv.py` (structure, text forms of every type, missing values, Unicode, quoting, formula-like text and escaping, round trips through the loader, safe file names), `test_export_xlsx.py` (workbooks read back with openpyxl and pandas, every cell type, limits, sheet names, formula-like text, control characters, long text, row chunks, and that the writer's counts match the page's), `test_export_records.py` (history and recipe JSON, undo and reset statuses, replay of downloaded recipes including frozen outlier boundaries, JSON safety), `test_reports.py` (both reports against the profile, quality and comparison engines, missing and invalid lineage, bounded examples, empty tables), `test_session_phase8.py` (downloads before and after cleaning, after undo, reset, outlier treatment and row removal, with a pending preview, and that nothing in the session changes) and `test_export_page.py` (the Export page, its notes and empty states, the files its buttons produce, and that downloading triggers no cleaning). Phase 7 added `test_compare.py` (every comparison section, row matching after deletions, duplicate index labels, invalid lineage, type changes, nullable and mixed dtypes, empty tables and columns, statistics, a 200,000-row table), `test_session_phase7.py` (comparison before and after cleaning, after undo and reset, across steps of every kind, and checks that comparing changes no session state) and `test_compare_page.py` (the Compare page, its empty states, caching and that it triggers no cleaning). Phase 6 added `test_outliers.py` (detection, hand-calculated IQR, z-score and percentile examples and edge cases), `test_outlier_ops.py` (validation and the four treatments, dtypes, missing values, duplicate index labels), `test_session_phase6.py` (preview, confirmation, stale previews, history, undo, reset, replay with frozen boundaries, fingerprints, steps from earlier phases) and `test_clean_phase6_pages.py` (the Outliers tab). `pytest.ini` sets the test path and import path, so run it from this folder.
