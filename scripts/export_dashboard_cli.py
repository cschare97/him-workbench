"""Write the dashboard tables as CSV files for Power BI (see docs/power-bi.md).

  python scripts/export_dashboard_cli.py --demo                  # from data/demo  -> data/dashboard-demo
  python scripts/export_dashboard_cli.py                         # from your real trackers -> data/dashboard
  python scripts/export_dashboard_cli.py --out some/folder
  add --xlsx to also write powerbi.xlsx (one sheet per table, typed columns; easiest to load in Power BI).
                 needs: pip install -e ".[powerbi]"

Files: referrals.csv, roi_requests.csv, quality_snapshots.csv, dim_date.csv. No patient ids, names, reasons or notes.
"""

import sys
from datetime import date

from him_workbench.analytics import QualityHistory, export_csv, export_xlsx
from him_workbench.referrals import ReferralStore
from him_workbench.roi import RoiStore


def main() -> int:
    args = sys.argv[1:]
    demo = "--demo" in args
    base = "data/demo" if demo else "data"
    out = args[args.index("--out") + 1] if "--out" in args else ("data/dashboard-demo" if demo else "data/dashboard")
    counts = export_csv(out, ReferralStore(f"{base}/referrals.db"), RoiStore(f"{base}/roi.db"),
                        QualityHistory(f"{base}/quality.db"), date.today())
    print(f"Wrote {out}/" + ("   (DEMO data)" if demo else ""))
    for name, n in counts.items():
        print(f"  {name:<22}{n:>6} rows")
    if "--xlsx" in args:
        try:
            export_xlsx(out)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {out}/powerbi.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
