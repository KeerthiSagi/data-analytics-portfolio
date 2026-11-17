# REFINE — Deployment Guide and Checklist

How to install, run and deploy REFINE on Streamlit Community Cloud, and what to check before a public deployment is approved. For how the app works, see the [technical documentation](TECHNICAL_DOCUMENTATION.md).

Engine version 0.9.0. Nothing in this guide has been deployed or tested on Community Cloud yet.

## Requirements

| | |
|---|---|
| Python | 3.13 (the only version tested) |
| Entry point | `project3-data-cleaning-engine/app_data_cleaner.py` |
| Dependencies | `project3-data-cleaning-engine/requirements.txt` |
| Config | `project3-data-cleaning-engine/.streamlit/config.toml` |
| Secrets | None. REFINE uses no API keys, database or external service |
| Disk | None. Uploads and downloads stay in memory |

Runtime dependencies (`requirements.txt`) are all used by the app: Streamlit, pandas, PyArrow, openpyxl, Plotly and NumPy. `requirements-dev.txt` adds pytest.

Community Cloud installs the newest versions each range allows. Every range has an upper bound just above the newest tested release line (for example `streamlit>=1.65.0,<1.68` and `pandas>=2.2.0,<3.2`), so a deployment never picks up an untested major or minor release, while patch releases still install. A test checks that every dependency has an upper bound and that each tested version is inside its range. The suite has passed on:

| pandas | Streamlit | PyArrow | NumPy |
|---|---|---|---|
| 2.2.3 | 1.65.0 | 25.0.1 | 2.5.4 |
| 3.0.6 | 1.65.0 | 25.0.1 | 2.5.4 |
| 3.1.1 | 1.67.0 | 26.0.1 | 2.5.5 |

The last row is what a fresh `pip install -r requirements-dev.txt` resolved to on 17 November 2026, with the current bounds (`pip check` reported no conflicts). To raise a bound, install the newer version in a fresh environment, run the suite, then update `requirements.txt` and this table. For a fully reproducible build you can instead pin exact versions from a tested environment (`pip freeze`), at the cost of no automatic patch releases.

## Install and run locally

```bash
cd project3-data-cleaning-engine
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
streamlit run app_data_cleaner.py
```

The app also runs from the repository root, as Community Cloud starts it:

```bash
streamlit run project3-data-cleaning-engine/app_data_cleaner.py
```

Either way the entry point adds its own folder to the import path and Streamlit reads the `.streamlit/config.toml` next to it, so the theme, the 20 MB upload limit and the error settings are the same. (The repository root has its own `.streamlit/config.toml` and `requirements.txt` for the other projects; REFINE's files take precedence for REFINE.)

To see full tracebacks in the browser while debugging locally:

```bash
streamlit run app_data_cleaner.py --client.showErrorDetails=full
```

## Streamlit Community Cloud settings

1. Repository: this repository; branch: the one you approved.
2. Main file path: `project3-data-cleaning-engine/app_data_cleaner.py`. The existing deployment already points at this file name.
3. Advanced settings → Python version: 3.13.
4. Secrets: leave empty.
5. Community Cloud uses `requirements.txt` from the entry point's folder before the repository root's, so REFINE's own dependencies are installed.

`config.toml` sets:

- `server.maxUploadSize = 20` (MB). It must match `MAX_UPLOAD_MB` in `refine/config.py`; a test checks this.
- `client.showErrorDetails = "type"`: an unexpected error shows its type and a generic message, not a traceback. Tracebacks are in the app's logs (Manage app).
- `browser.gatherUsageStats = false`.
- The theme. Streamlit's XSRF and CORS protections keep their defaults (on).

## Memory

Community Cloud apps get about 1 GB of memory, shared by all sessions of the app. One session near the limits (a 20 MB CSV of 200,000 rows) peaks at roughly 300–350 MB while parsing and then holds the table, the cleaned table and up to 96 MiB of undo states. Two or three such sessions at the same time can exhaust the memory. This is acceptable for a portfolio demo; a heavily used deployment would need lower limits or more memory.

## Acceptance checklist

Mark each item only after doing it. Automated results are listed separately at the end.

### 1. Dependency installation

- [ ] A fresh Python 3.13 environment installs `requirements-dev.txt` without errors.
- [ ] `pytest` passes in that environment (note the pandas and Streamlit versions installed).

### 2. Application startup

- [ ] `streamlit run app_data_cleaner.py` starts without errors in the console.
- [ ] `streamlit run project3-data-cleaning-engine/app_data_cleaner.py` from the repository root starts too, with the dark theme.

### 3. Navigation

- [ ] All six pages open from the sidebar: Upload, Profile & Quality, Clean, Compare, History, Export.
- [ ] With no dataset, every page except Upload shows "No dataset loaded" and a link to Upload.
- [ ] The browser's back and forward buttons move between pages without errors.

### 4. Upload

- [ ] Open the sample dataset: the sidebar shows 500 rows × 9 columns.
- [ ] Upload a CSV and an .xlsx file of your own; "Reading <file>…" shows while a large file loads.
- [ ] Upload an invalid file (a renamed .txt, a .xls, an empty file): a plain message explains what to do, and a dataset that was already open stays open.
- [ ] Upload the same file twice, and close and reopen a dataset.
- [ ] Re-read a CSV with another delimiter, and an .xlsx with another sheet.

### 5. Profiling

- [ ] Profile & Quality shows the overview, findings with counts and denominators, and the column profiles.
- [ ] Add and remove a validity rule.
- [ ] A wide file (100+ columns) is readable.

### 6. Cleaning

- [ ] In each tab (Columns, Missing values, Duplicates, Data types, Text, Outliers), configure a step, preview it, and check that nothing changes until "Confirm and apply".
- [ ] Cancel in the confirmation dialog applies nothing.
- [ ] Change a setting after previewing: "Selection changed" appears and Apply is hidden.
- [ ] Preview a step in the Text or Outliers tab, open Compare and Export, then return to Clean: the notice names the tab, the settings are unchanged, and the step can be applied.
- [ ] Undo on History restores the previous data; Reset asks for confirmation and restores the original.
- [ ] "Verify by replaying" on History reports an exact match.

### 7. Comparison

- [ ] Compare matches the row and missing counts on Profile & Quality after cleaning.
- [ ] Removed rows are not counted as changed cells.
- [ ] With a pending preview, Compare says the preview is not included.

### 8. Export

- [ ] Download the CSV and open it in a spreadsheet app and a text editor (UTF-8, header row, one row per data row).
- [ ] Download the Excel file and open it in Microsoft Excel or LibreOffice: one sheet, "Cleaned Data", numbers and dates as numbers and dates, formula-like text as text.
- [ ] Download history, recipe, quality report and comparison report; each opens as valid JSON.
- [ ] With formula-like text in the data, the warning and the escape switch appear.
- [ ] With a pending preview, the downloads hold the confirmed data only.

### 9. Error handling

- [ ] Invalid settings (for example a fill value that is not a number for a number column) show a message and change nothing.
- [ ] No traceback appears in the browser for any of the above.

### 10. Session isolation

- [ ] Open the app in two browsers (or a normal and a private window), load different files, and check that neither sees the other's data, steps or downloads.
- [ ] Reloading the page starts a new session with no dataset.

### 11. Performance

- [ ] A file near the limits (for example 200,000 × 10) loads, profiles and exports within a few seconds each; the Compare page and the first report take a few seconds the first time and are instant afterwards.

### 12. Browser verification

- [ ] Current Chrome, Firefox and Safari (or Edge) on a desktop.
- [ ] A narrow window (about 400 px) or a phone: the sidebar collapses, cards wrap, tables scroll sideways.
- [ ] Keyboard only: Tab reaches every control on Clean, the confirmation dialog can be confirmed and cancelled, and focus is visible.

### 13. Public deployment approval

- [ ] Everything above passes, or the exceptions are written down and accepted.
- [ ] The dependency versions to deploy are accepted (bounded ranges, as above, or exact pins).
- [ ] The changes are committed and pushed to the branch Community Cloud deploys.
- [ ] After deployment: repeat sections 2–4, 6 and 8 on the public URL, and check the app's logs for errors.

## Verified so far

Done in Phase 9 on an Apple silicon Mac, Python 3.13.3:

- Full test suite (1,029 tests): passes on pandas 2.2.3 and 3.0.6 with Streamlit 1.65.0, and on the newest versions the requirements resolved to (pandas 3.1.1, Streamlit 1.67.0, PyArrow 26.0.1).
- `streamlit run` from the project folder and from the repository root: the server started, the health check answered `ok`, and the pages returned HTTP 200. Streamlit's configuration loader reads REFINE's `config.toml` in both cases.
- The pages were exercised with Streamlit's AppTest (no browser).

Repeated during final preparation (17 November 2026, same Mac, Python 3.13.3):

- Full test suite (1,029 tests, no warnings or skips): passes with pandas 2.2.3 / Streamlit 1.65.0, pandas 3.0.6 / Streamlit 1.65.0, and a fresh install from the bounded `requirements-dev.txt` (pandas 3.1.1 / Streamlit 1.67.0 / PyArrow 26.0.1 / NumPy 2.5.5).
- `streamlit run` from the project folder and from the repository root, including in the fresh environment: health check `ok`, pages HTTP 200, no errors in the server log. From the repository root, Streamlit resolves the dark theme, the 20 MB upload limit and `showErrorDetails = "type"` from REFINE's own `config.toml`.
- Every page through AppTest with no dataset, with the sample, after four cleaning steps with a preview pending, after undo and after reset: no exceptions or errors; the pending preview survived visits to other pages; after reset the stale preview was discarded with a message.

Not done: manual testing in a browser, testing on Community Cloud, testing in Microsoft Excel or LibreOffice, and screen-reader testing.

The live URL in the README serves whatever was last deployed from the pushed branch. At the time of writing that branch has Phases 1–5 only; the current version is not deployed.

## Known limitations

- No authentication, rate limiting or per-user memory quota; anyone with a public URL can use the app (see Memory above).
- Tested on Python 3.13 only.
- Excel files keep 15 significant digits, have no time zones, cut text over 32,767 characters, replace control characters, and write dates before 1 March 1900, infinite values and whole numbers with 16 or more digits as text. The Export page says when any of these applies.
- Accessibility has not been tested with assistive technology. Status messages use text as well as color; section headings skip from level 1 to level 4.
- Full list: [technical documentation, Limitations](TECHNICAL_DOCUMENTATION.md#limitations).
