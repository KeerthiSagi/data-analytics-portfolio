# Data Analyst Portfolio

Keerthi Varma Sagi · MS Computer Science

This portfolio contains three interactive Python applications that follow the work of a data analyst: preparing raw data for analysis, reporting on business performance, and investigating why a business metric changed. Each one is a standalone Streamlit app that you can try in the browser or run locally.

## Projects

### ATLAS — Executive Sales Dashboard

ATLAS is a sales dashboard for a global retailer, built on the Global Superstore dataset (about 51,000 order lines from 2011 to 2014). It tracks revenue, profit, profit margin, orders and average order value, and compares each figure with the previous period.

It helps answer everyday business questions:

- How are sales and profit trending, and which categories and regions drive them?
- Which customer segments are most profitable, and where are discounts leading to losses?
- How do shipping costs and order processing times differ by region and ship mode?

A data explorer lets you filter and search the underlying orders and download them as CSV.

[Project folder](project1-atlas-analytics/) · [Live app](https://data-analyst-portfolio-onoez34kn8frqtuwvybao8.streamlit.app/)

### CAUSE — Root Cause Analyzer

CAUSE is a diagnostic analysis tool. When revenue, profit, orders or profit margin go up or down between two periods, it shows where the change came from by breaking it down across markets, regions, product categories and customer segments.

You choose the periods to compare (month over month, quarter over quarter, year over year or a custom range), and CAUSE ranks each group by its contribution to the change. It also splits revenue changes into an order-volume effect and an average-order-value effect, and margin changes into a rate effect and a mix effect. The results can be downloaded as CSV files and a written investigation report.

CAUSE shows where a change is concentrated. It does not prove what caused it; its findings are a starting point for further investigation.

[Project folder](project2-root-cause-analyzer/) · [Live app](https://data-analyst-portfolio-yictmcakk4rypw8ulkd85s.streamlit.app/)

### REFINE — Data Cleaning Engine

REFINE helps you prepare a messy dataset for analysis. You can:

- Upload a CSV or Excel file and see its data-quality issues
- Handle missing values and remove duplicate records
- Standardize text and convert columns to the right data types
- Detect and treat outliers
- Compare the original and cleaned data side by side
- Download the cleaned dataset, a record of every change, and data-quality reports

Every cleaning step is previewed before it is applied and can be undone. Cleaning is rule-based: REFINE does not use AI, and it never changes data without your confirmation.

[Project folder](project3-data-cleaning-engine/) · [Live app (older version)](https://data-analyst-portfolio-fbu8nnzddmgchaadmjmc8q.streamlit.app/)

The live REFINE app runs an earlier version with uploading, profiling and the first cleaning tools. The latest features (outlier handling, the comparison page and the downloads) are in this repository but not yet deployed. Run it locally to see the current version.

## How the projects fit together

The three apps follow a common analytics workflow:

**REFINE → ATLAS → CAUSE**

1. **REFINE** prepares the data: finding and fixing quality problems before analysis.
2. **ATLAS** reports business performance: what is happening across sales, profit and customers.
3. **CAUSE** explains changes: which parts of the business drove a rise or fall in a metric.

They are separate applications, not one connected pipeline. ATLAS and CAUSE both use the Global Superstore dataset, while REFINE works with any CSV or Excel file you upload.

## Skills demonstrated

- **Python and pandas** for data loading, transformation and analysis
- **Data cleaning and validation**: missing values, duplicates, data types, text standardization and outliers
- **Exploratory data analysis** and data profiling
- **Business metrics and KPI analysis**: revenue, profit, margin, order volume and average order value
- **Variance and contribution analysis** for explaining changes between periods
- **Data visualization** with Plotly
- **Interactive web apps** with Streamlit
- **Automated testing**: each project has its own test suite (140, 228 and 1,029 tests)

## Running locally

Each project runs on its own. For example, to run ATLAS:

```bash
cd project1-atlas-analytics
pip install -r requirements.txt
streamlit run app_kpi_dashboard.py
```

For CAUSE, use `project2-root-cause-analyzer` and `app_root_cause.py`. For REFINE, use `project3-data-cleaning-engine` and `app_data_cleaner.py` (REFINE has been tested with Python 3.13). Each project's README has full setup and testing instructions.

## Documentation

- [ATLAS README](project1-atlas-analytics/README.md)
- [CAUSE README](project2-root-cause-analyzer/README.md)
- [REFINE README](project3-data-cleaning-engine/README.md)

Each project also has technical documentation in its `docs` folder, covering metric definitions, calculation rules and limitations.
