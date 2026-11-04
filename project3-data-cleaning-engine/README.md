# REFINE — Automated Data Cleaning Engine

A Streamlit app for inspecting and cleaning CSV and Excel files.

**Live app:** https://data-analyst-portfolio-fbu8nnzddmgchaadmjmc8q.streamlit.app/ (may lag behind the latest code in this repository)

## Overview

REFINE loads a CSV or Excel file, profiles every column, and reports data quality problems such as missing values, duplicates, inconsistent formatting and numbers or dates stored as text. Cleaning steps are rule-based. Each step shows a preview of its effect, is applied only after you confirm it, and is recorded so it can be undone or replayed on the original file.

**Status:** in development. Phases 1–5 are complete (engine version 0.5.0). Outlier handling, before-and-after comparison and exporting the cleaned data are planned and not available yet.

## Features

**Loading**
- Upload `.csv` or `.xlsx` files, or open the built-in sample dataset
- Detects CSV encoding and delimiter, with options to override them, pick an Excel sheet or skip title rows
- Load notes list every adjustment made while reading the file, such as padded short rows or renamed duplicate columns

**Profiling and quality checks**
- Dataset summary and a profile of each column: suggested type, missing values, distinct values, top values and formatting issues
- Quality checks for missing values, null-like text (`NA`, `null`, `-`), duplicate rows, whitespace, inconsistent capitalization, mixed or ambiguous date formats, and numbers stored with symbols
- Optional validity rules: a number range, a list of allowed values or a text pattern

**Cleaning**
- Drop columns
- Missing values: convert markers like `NA` to real missing values, drop rows or mostly empty columns, fill with a fixed value, mean, median, most frequent value or previous/next value, add indicator columns
- Duplicates: find and remove, matching on all columns or chosen key columns, optionally ignoring case and extra spaces
- Data types: convert text to whole numbers, decimals, true/false, dates or dates with times, with explicit rules for currency symbols, separators and date order
- Text: trim whitespace, change case, collapse spaces, normalize Unicode, remove non-printing characters, find and replace, and map category variants (`NY`, `new york`) to one value

**History**
- Audit log of applied, undone and reset steps
- Undo the last step or reset to the original file
- A recipe of the applied steps that can be replayed on the original data to check it gives the same result

## Dataset

REFINE works with files you upload. Limits are 20 MB, 200,000 rows and 300 columns per file. Uploaded files stay in memory for the browser session and are not saved.

The repository includes a synthetic sample, [`sample_data/messy_customers.csv`](sample_data/messy_customers.csv) (500 rows, 9 columns), with planted problems: duplicate rows, missing-value markers, mixed date formats, numbers stored as text, inconsistent spellings and booleans, and a few outliers. Its [README](sample_data/README.md) lists them.

## Tech Stack

- Python
- Streamlit
- pandas and PyArrow
- openpyxl
- Plotly
- pytest

## Run Locally

```bash
cd project3-data-cleaning-engine
pip install -r requirements.txt
streamlit run app_data_cleaner.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Run from the `project3-data-cleaning-engine` folder. The suite has 643 tests and has been run on pandas 2.2.3 and 3.0.6. It covers file parsing and encodings, profiling and quality checks, each cleaning operation, undo and replay, and the Streamlit pages.

## Documentation

Parsing rules, quality check definitions, details of each cleaning operation, how undo and replay work, memory limits, benchmarks and known limitations are in [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md).
