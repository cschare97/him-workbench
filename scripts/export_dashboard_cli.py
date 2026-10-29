"""Write the dashboard tables as CSV files for Power BI (see docs/power-bi.md).

  python scripts/export_dashboard_cli.py --demo                  # from data/demo  -> data/dashboard-demo
  python scripts/export_dashboard_cli.py                         # from your real trackers -> data/dashboard
  python scripts/export_dashboard_cli.py --out some/folder

Files: referrals.csv, roi_requests.csv, quality_snapshots.csv, dim_date.csv. No patient ids, names, reasons or notes.
"""

import sys
from datetime import date

from him_workbench.analytics import QualityHistory, export_csv
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
