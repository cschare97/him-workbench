# Power BI: referrals, ROI deadlines and data quality

The web Dashboard (`/dashboard`) and these CSV files come from the same tables in `src/him_workbench/analytics.py`, so the two should show the same numbers. Use that to check your Power BI build.

**Demo data only unless you point it at your own trackers.** The demo files are invented by a seeded generator (`src/him_workbench/demo_data.py`). The improving data-quality trend in them is put there on purpose. Do not present it as a real result.

**Not tested here.** Power BI Desktop runs on Windows only, and this was written where it can't run. The CSVs and the numbers they hold are tested. The DAX below is written to standard patterns but has not been run in Power BI, so reconcile each measure against the web Dashboard (last section) before you trust it.

## 1. Make the files

```
python scripts/seed_demo_cli.py                      # invented demo data in data/demo
python scripts/export_dashboard_cli.py --demo        # CSVs in data/dashboard-demo
python scripts/export_dashboard_cli.py               # or: from your real trackers, in data/dashboard
```

| File | One row per | Notes |
|---|---|---|
| `referrals.csv` | referral | dates sent / scheduled / completed, days between them, state, `no_response` flag |
| `roi_requests.csv` | ROI request | received, due, closed, days to close, `closed_on_time`, `days_late`, `overdue` |
| `quality_snapshots.csv` | snapshot x metric | long format: `kind` is `problem` or `missing`; counts only |
| `dim_date.csv` | calendar day | year, quarter, month, week, weekday |

No patient ids, names, reasons, diagnoses or notes are in any file. Blank cells mean "does not apply yet" (for example `days_to_complete` on an open referral).

## 2. Load them (in your Windows VM)

Get the export folder into Windows, for example through a Parallels shared folder.

**Easiest: the Excel file.** Add `--xlsx` when you export (needs `pip install openpyxl`, or `pip install -e ".[powerbi]"`). It writes `powerbi.xlsx`: one sheet per table, header row and column types included. In Power BI Desktop: **Get data > Excel workbook**, pick the file, tick all four sheets, **Load**. Nothing to fix in Power Query, so skip the type list below, except marking the date table.

**Or the CSVs.** In Power BI Desktop: **Get data > Text/CSV**, load all four. This was tested: Power BI did *not* use the first row as column names (columns came in as Column1, Column2...), so in **Transform data** click **Use first row as headers** on each table first, then set column types:

- Dates: `created`, `scheduled_on`, `appointment_date`, `completed_on`, `declined_on` (referrals); `received`, `original_due`, `due`, `verified_on`, `closed_on` (roi_requests); `snapshot_date` (quality_snapshots); `date`, `week_start` (dim_date). Type **Date**.
- Whole numbers: every `days_*`, `no_response`, `extended`, `closed_on_time`, `overdue`, `is_weekend`, `records`, `records_affected`.
- Mark `dim_date` as the date table (right-click it > Mark as date table > `date`).

## 3. Relationships

All many-to-one from the fact table to `dim_date[date]`, single direction.

| From | To | Active |
|---|---|---|
| `referrals[created]` | `dim_date[date]` | yes |
| `referrals[completed_on]` | `dim_date[date]` | no |
| `roi_requests[received]` | `dim_date[date]` | yes |
| `roi_requests[closed_on]` | `dim_date[date]` | no |
| `quality_snapshots[snapshot_date]` | `dim_date[date]` | yes |

## 4. Measures (DAX)

```dax
-- Referrals
Referrals = COUNTROWS ( referrals )
Open Referrals = CALCULATE ( [Referrals], referrals[state] <> "closed" )
No Response = SUM ( referrals[no_response] )
Median Days to Schedule = MEDIAN ( referrals[days_to_schedule] )
Median Days to Complete = MEDIAN ( referrals[days_to_complete] )

-- ROI
ROI Requests = COUNTROWS ( roi_requests )
ROI Open = CALCULATE ( [ROI Requests], roi_requests[closed_on] = BLANK () )
ROI Overdue = SUM ( roi_requests[overdue] )
ROI Closed = COUNT ( roi_requests[closed_on] )
ROI Closed On Time = SUM ( roi_requests[closed_on_time] )
ROI On-Time % = DIVIDE ( [ROI Closed On Time], [ROI Closed] )
Median Days to Close = MEDIAN ( roi_requests[days_to_close] )

-- ROI by the month it was closed (uses the inactive relationship)
Closed On Time by Close Date =
    CALCULATE ( [ROI Closed On Time], USERELATIONSHIP ( roi_requests[closed_on], dim_date[date] ) )
Closed Late by Close Date =
    CALCULATE ( [ROI Closed] - [ROI Closed On Time], USERELATIONSHIP ( roi_requests[closed_on], dim_date[date] ) )

-- Data quality (use with one snapshot date in context, for example on a line chart by date)
Problems per 100 Records =
    DIVIDE (
        CALCULATE ( SUM ( quality_snapshots[records_affected] ), quality_snapshots[kind] = "problem" ),
        MAX ( quality_snapshots[records] )
    ) * 100
```

`MEDIAN` skips blanks, which is what you want: a referral that was never scheduled is not zero days. "Problems per 100 Records" adds the per-check counts, and one record can trip several checks, so it is a trend indicator, not a share of records. Over a date range it is not meaningful, only per snapshot.

## 5. Pages

1. **Referral turnaround.** Cards: Referrals, Open Referrals, No Response, Median Days to Schedule. Clustered bar: `referrals[specialty]` by Median Days to Schedule (sorted descending). Slicers: `dim_date[year_month]` (on created), `priority`, `coordinator`.
2. **ROI deadline.** Cards: ROI Requests, ROI Open, ROI Overdue, ROI On-Time %. Stacked column: `dim_date[year_month]` by Closed On Time by Close Date and Closed Late by Close Date. Table of open requests with `days_left`, sorted ascending (negative means overdue). Slicer: `requester_type`.
3. **Data quality.** Line chart: `quality_snapshots[snapshot_date]` by Problems per 100 Records. Bar: `label` by `records_affected` for the latest snapshot, filtered to `kind = problem`.

Chart habits that match the web page: one measure per axis, no dual axes; blue for the main series and orange for "late"; label the last point, not every point; thin bars; show a table view (right-click a visual > Show as a table).

## 6. Check it against the web page

Run `python scripts/ui_cli.py --demo-data` and open `/dashboard` for the same demo data. These should match exactly: Referrals, Open Referrals, No Response, Median Days to Schedule, Median Days to Complete, ROI Requests, ROI Open, ROI Overdue, ROI On-Time %, Median Days to Close, and the latest Problems per 100 Records. If one differs, the DAX measure is wrong, not the data.
