# REFINE — Data Cleaning Engine

REFINE is a web app for inspecting, cleaning and preparing messy datasets for analysis. You upload a CSV or Excel file, see what is wrong with it, fix the problems step by step, compare the result with the original, and download the cleaned data along with a record of every change.

**Live app:** https://data-analyst-portfolio-fbu8nnzddmgchaadmjmc8q.streamlit.app/
The live version is older than this code. It has uploading, profiling and the first cleaning tools, but not outlier handling, the comparison page or the downloads described below. Run it locally to see the current version.

## Why I built it

Real datasets are rarely ready to analyze. They have missing values, duplicate records, numbers and dates stored as text, inconsistent spellings and the occasional extreme value. Fixing these by hand in a spreadsheet is slow, easy to get wrong, and hard to repeat on the next file.

I wanted a tool that makes data cleaning visible and reversible: every change is previewed before it happens, recorded once it does, and can be undone or repeated.

## What you can do

- **Upload and inspect** CSV or Excel files, or try the included sample dataset.
- **Find quality problems:** missing values, duplicates, extra spaces, inconsistent capitalization, mixed date formats and numbers stored as text, each with a count of how many rows are affected.
- **Clean the data:** fill or remove missing values, remove duplicates, convert columns to numbers or dates, standardize text, and detect and treat outliers.
- **Preview before applying:** every step shows exactly what it will change, and nothing changes until you confirm.
- **Undo and review:** undo the last step, reset to the original file, or review the full history of changes.
- **Compare** the original and cleaned data side by side: rows removed, values changed, missing values and quality checks before and after.
- **Download** the cleaned data as CSV or Excel, the cleaning history, a reusable cleaning recipe, and data-quality and comparison reports.

REFINE uses clear, rule-based cleaning steps. It does not use AI or machine learning, and it never changes data without your confirmation.

## How it works

1. **Upload** a dataset.
2. **Review** its profile and quality checks.
3. **Choose** a cleaning step and adjust its settings.
4. **Preview** the result, then confirm to apply it.
5. **Compare** the cleaned data with the original.
6. **Download** the cleaned file and reports.

## Built with

Python, Streamlit, pandas, NumPy and Plotly.

## Run locally

Requires Python 3.13.

```bash
cd project3-data-cleaning-engine
pip install -r requirements.txt
streamlit run app_data_cleaner.py
```

Then open the address Streamlit prints (usually http://localhost:8501) and click **Open sample dataset** to try it.

## Testing

REFINE has 1,029 automated tests covering file loading, quality checks, every cleaning operation, undo and history, the comparison, the downloads, the app's pages, and complete workflows from upload to export. To run them:

```bash
pip install -r requirements-dev.txt
pytest
```

## Limitations

- Files can be CSV or `.xlsx` (not the older `.xls`), up to 20 MB, 200,000 rows and 300 columns.
- Uploaded files are kept in memory for your browser session only. They are not saved to disk or shared, and they are gone when you close the page.
- CSV downloads store every value as text, so column types are not kept. Excel downloads keep types but are limited by Excel itself (for example, 15 significant digits). The Export page says when this affects your data.
- Cleaning recipes can be downloaded and replayed in Python, but the app cannot import one yet.

## Documentation

- [Technical documentation](docs/TECHNICAL_DOCUMENTATION.md): how each check and cleaning step works, file formats, limits and performance.
- [Deployment guide](docs/DEPLOYMENT.md): setup, Streamlit Community Cloud settings and the manual testing checklist.
- [Sample dataset](sample_data/README.md): the problems planted in the included sample file.
