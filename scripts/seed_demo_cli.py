"""Create invented demo data for the Dashboard in data/demo (referrals, ROI requests, quality history).

  python scripts/seed_demo_cli.py            # refuses to overwrite existing demo files
  python scripts/seed_demo_cli.py --force    # replace them
  python scripts/ui_cli.py --demo-data       # then view it on the Dashboard page

Everything is invented by a seeded generator. Your real data/roi.db and data/referrals.db are never touched.
"""

import sys

from him_workbench.demo_data import seed_demo


def main() -> int:
    try:
        paths = seed_demo("data/demo", force="--force" in sys.argv)
    except FileExistsError as e:
        print(e)
        return 1
    print("Wrote invented demo data:\n  " + "\n  ".join(paths.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
