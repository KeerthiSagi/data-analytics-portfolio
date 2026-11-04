# ATLAS Analytics — Technical Documentation

Reference for how ATLAS loads the data, defines its metrics and builds each page. For a short overview, see the [project README](../README.md).

## Contents

- [Dataset](#dataset)
- [Data loading](#data-loading)
- [Pages and filters](#pages-and-filters)
- [Metric definitions](#metric-definitions)
- [Period comparison](#period-comparison)
- [Trend charts](#trend-charts)
- [Discount bands](#discount-bands)
- [Shipping and logistics](#shipping-and-logistics)
- [Data Explorer](#data-explorer)
- [Tests](#tests)
- [Project structure](#project-structure)

## Dataset

`data/Global_Superstore.xlsx` (Global Superstore retail data).

| | |
|---|---|
| Rows (order line items) | 51,290 |
| Distinct Order IDs | 25,035 |
| Order dates | 1 Jan 2011 – 31 Dec 2014 |
| Markets | 7 (APAC, Africa, Canada, EMEA, EU, LATAM, US) |
| Regions | 13 |
| Countries | 147 |
| Categories / sub-categories | 3 / 17 |
| Customer segments | Consumer, Corporate, Home Office |

Each row is a line item, not an order. One order can contain several products, so row counts and order counts are reported separately throughout the app.

## Data loading

`utils/data_loader.py` loads the workbook once per session (`st.cache_data`):

1. Renames the dotted source columns (`Order.Date` → `Order_Date`, `Sub.Category` → `Sub_Category`, and so on).
2. Checks that `Order_Date`, `Ship_Date`, `Order_ID`, `Sales`, `Profit`, `Region` and `Category` exist, and raises an error listing any that are missing.
3. Converts the date columns with `pd.to_datetime` and `Sales`, `Profit`, `Quantity`, `Discount` and `Shipping_Cost` with `pd.to_numeric`. Unparseable values become missing.
4. Drops rows missing `Order_Date`, `Order_ID`, `Sales` or `Profit`. With the current file, no rows are dropped (51,290 in, 51,290 out).
5. Adds `Year`, `Year_Month` and `Shipping_Days` (`Ship_Date − Order_Date` in days), then sorts by order date.

`test_loader.py` is a standalone script that loads the file and prints row counts, date range and headline totals. It is a manual check, not part of the test suite.

## Pages and filters

| Page | File | Sidebar filters |
|---|---|---|
| Executive Overview | `app_kpi_dashboard.py` | Reporting period, Region, Product Category |
| Segment Performance | `pages/1_Segment_Performance.py` | Reporting period, Region, Category, Segment |
| Regional Performance & Logistics | `pages/2_Regional_Logistics.py` | Reporting period, Region, Category, Segment; plus a ship-mode filter for the logistics section only |
| Data Explorer | `pages/3_Data_Explorer.py` | Date range, Region, Country, Category, Sub-Category, Segment, Ship Mode, text search, reset button |

Filters default to everything selected. When the filters match no rows, the page shows a warning and stops.

**Executive Overview:** KPI cards with sparklines and prior-period comparison; revenue and profit trends (revenue, profit, margin, or revenue and profit together); category and region bar charts, each switchable between revenue, profit and margin; calculated insights.

**Segment Performance:** segment KPIs; segment comparison by a chosen metric; revenue contribution; profitability health; loss analysis; discount and profitability analysis; segment trends; insights; a comparison detail table.

**Regional Performance & Logistics:** regional KPIs, comparison, revenue contribution, profitability health, trends, insights and detail table; then shipping cost, ship-mode performance, regional shipping cost, shipping cost vs profitability, order-to-shipment processing time, logistics trends and insights.

**Data Explorer:** row-level table with column selection, CSV export and a data quality summary.

## Metric definitions

| Metric | Definition |
|---|---|
| Revenue | Sum of `Sales` |
| Profit | Sum of `Profit` |
| Orders | Distinct `Order_ID` values |
| Profit margin | Total profit ÷ total revenue × 100. Zero revenue gives no margin (0 on the Executive Overview card). |
| Average order value | Revenue ÷ distinct `Order_ID` values. Not the mean of line-item sales. |
| Revenue share | Group revenue ÷ filtered total revenue. Undefined when the total is zero. |

- Group margins (category, region, segment, discount band) are group profit ÷ group revenue. Row-level margins are never averaged.
- Distinct order counts per segment or region are not additive: one order can appear in more than one group.
- Loss analysis reports two different counts per segment: orders that contain at least one loss-making line, and orders whose total profit is negative. They are shown separately and not summed across segments.
- ATLAS counts orders by `Order_ID` alone. In this dataset 660 Order IDs are reused across customers or dates, so ATLAS's order count (25,035) is lower than the composite-key count used in CAUSE (25,754).

## Period comparison

The Executive Overview compares the selected window with the equal-length window immediately before it.

- `previous_end = start − 1 day`, `previous_start = previous_end − (period_days − 1)`.
- A comparison is shown only when the whole previous window falls inside the dataset and has data. Otherwise the card says "Comparison unavailable".
- Percentage change = (current − previous) ÷ |previous| × 100. A zero or missing baseline gives no percentage.
- Margin change is the difference in margin, shown in percentage points.
- Profit is not shown as a growth rate when the previous period was a loss. A move from −100 to −50 is reported as "Loss narrowed by $50", and moves to zero or positive profit as "Reached break-even" or "Returned to profit".

## Trend charts

Sparklines and trend charts use only rows inside the selected window. The time bucket depends on the window length:

| Window | Bucket |
|---|---|
| Up to 45 days | Daily |
| 46–180 days | Weekly |
| Over 180 days | Monthly |

Empty buckets stay empty (NaN) rather than being filled with zero. Margin per bucket is bucket profit ÷ bucket revenue, and zero-revenue buckets have no margin. Orders per bucket are distinct `Order_ID` values in that bucket, so bucket counts are not forced to add up to the KPI total.

## Discount bands

Used on the Executive Overview and Segment Performance pages.

| Band | Discount |
|---|---|
| No discount | 0 |
| 0–10% | above 0 up to 10% |
| 10–20% | above 10% up to 20% |
| 20–30% | above 20% up to 30% |
| Above 30% | above 30% |

Bands are non-overlapping; 10%, 20% and 30% fall in the band they close. Each band shows revenue, profit and aggregate margin.

## Shipping and logistics

- **Order-to-shipment days** = `Ship_Date − Order_Date`. This is processing time until shipment, not delivery time to the customer.
- An interval is valid when both dates exist and the day count is not negative. Negative intervals are kept for auditing but excluded from averages; missing and negative intervals are counted.
- Average and median processing days, and the same-day rate (share of valid intervals equal to 0 days), are line-item metrics.
- **Shipping cost ratio** = total shipping cost ÷ revenue × 100, undefined when revenue is zero.
- Shipping cost is never subtracted from recorded profit.
- The ship-mode filter applies only to the logistics section and does not change the page's other filters.

## Data Explorer

- Search is a case-insensitive partial match across `Order_ID`, `Product_Name`, `Customer_Name` and `Customer_ID`. Repeated spaces in the query are collapsed.
- Country selection is either all countries or a chosen list. An empty list gives an empty result, with a note in the sidebar.
- The table previews up to 500 rows. Exports always include every matching row.
- Two CSV downloads (UTF-8, no index):
  - `atlas_filtered_superstore.csv`: all columns
  - `atlas_filtered_superstore_visible.csv`: only the columns selected for the table
- The data quality summary for the current view shows row and column counts, missing values per column, exact full-row duplicates, repeated Order IDs (rows minus distinct Order IDs), distinct orders and the date range. Repeated Order IDs are expected in line-item data and are reported separately from full-row duplicates.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

140 tests across:

| File | Covers |
|---|---|
| `test_project1_kpis.py` | Dataset row count, date range, published totals, column types |
| `test_kpi_sparklines.py` | Bucketing, empty buckets, comparison wording |
| `test_executive_analytics.py` | Group summaries, discount bands, insights |
| `test_segment_analytics.py` | Segment KPIs, loss analysis, discount profitability |
| `test_regional_analytics.py` | Regional KPIs and aggregation |
| `test_logistics_analytics.py` | Processing intervals and shipping metrics |
| `test_data_explorer.py` | Filters, search, quality summary, CSV export |
| `test_phase8_qa.py` | Totals checked against independent pandas calculations, ship-mode filter isolation, export and search |
| `test_ui_polish.py` | Page navigation and theme |

## Project structure

```text
project1-atlas-analytics/
├── app_kpi_dashboard.py      # Executive Overview (entry point)
├── pages/
│   ├── 1_Segment_Performance.py
│   ├── 2_Regional_Logistics.py
│   └── 3_Data_Explorer.py
├── utils/
│   ├── data_loader.py        # Load, validate and prepare the dataset
│   ├── kpi_metrics.py        # Sparkline aggregation, comparison and currency formatting
│   ├── executive_analytics.py
│   ├── segment_analytics.py
│   ├── regional_analytics.py
│   ├── logistics_analytics.py
│   ├── data_explorer.py      # Filtering, search, quality summary, CSV export
│   ├── navigation.py, theme.py, palette.py
├── data/Global_Superstore.xlsx
├── tests/
├── test_loader.py            # Manual loader check
└── requirements.txt
```
