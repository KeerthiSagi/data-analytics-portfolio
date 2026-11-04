# ATLAS Analytics

A Streamlit sales dashboard for the Global Superstore dataset.

**Live app:** https://data-analyst-portfolio-onoez34kn8frqtuwvybao8.streamlit.app/

## Overview

ATLAS tracks revenue, profit, profit margin, orders and average order value for a global retailer. It has four pages: an executive overview, customer segment performance, regional performance with shipping analysis, and a data explorer for browsing and exporting the underlying rows. Each page has its own sidebar filters for date range, region, category and, where relevant, customer segment.

## Features

**Executive Overview**
- KPI cards for revenue, profit, margin and orders, with sparklines and a comparison to the previous period of the same length
- Revenue and profit trends, with daily, weekly or monthly grouping depending on the date range
- Category and region charts that switch between revenue, profit and margin
- Short insights calculated from the filtered data

**Segment Performance**
- Revenue, profit, margin and average order value by segment (Consumer, Corporate, Home Office)
- Loss analysis, counting both loss-making line items and unprofitable orders
- Profit margin by discount band
- Segment trends over time

**Regional Performance & Logistics**
- Regional comparison, revenue share, profitability and trends
- Shipping cost and cost-to-revenue ratio by ship mode and region
- Order-to-shipment processing time (ship date minus order date; this is not delivery time)

**Data Explorer**
- Filter by date, region, country, category, sub-category, segment and ship mode
- Search by order ID, product, customer name or customer ID
- Download the filtered rows as CSV (all columns or only the visible ones)
- Data quality summary: missing values, duplicate rows and repeated order IDs

## Dataset

[Global Superstore](data/Global_Superstore.xlsx) retail sales data:

- 51,290 rows, where each row is an order line item
- 25,035 distinct order IDs
- Orders from January 2011 to December 2014
- 7 markets, 13 regions, 147 countries
- 3 product categories, 17 sub-categories, 3 customer segments

Because one order can contain several products, order counts use distinct order IDs, not row counts.

## Tech Stack

- Python
- Streamlit (multi-page app)
- pandas and NumPy
- Plotly
- openpyxl (to read the Excel file)

## Run Locally

```bash
cd project1-atlas-analytics
pip install -r requirements.txt
streamlit run app_kpi_dashboard.py
```

The app opens at http://localhost:8501.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The suite has 140 tests. They check KPI totals against the dataset, margin and average order value calculations, period comparisons, trend bucketing, shipping metrics, Data Explorer filtering, search and CSV export.

## Documentation

Metric definitions, filter behaviour, discount bands, shipping calculations and the project structure are in [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md).
