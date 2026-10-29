"""Run the scheduling/EHR sync diagnostic on invented demo data or on a JSON file you map yourself.

    python scripts/sync_diag_cli.py --demo
    python scripts/sync_diag_cli.py --write-demo data/sync-demo.json
    python scripts/sync_diag_cli.py --file data/sync-demo.json [--json]

Synthetic data only. Never point this at a real patient export.
"""
import argparse
import json
import sys

from him_workbench import sync_diagnostic as sd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--demo", action="store_true", help="generate invented data and diagnose it")
    g.add_argument("--file", help="JSON file in the documented shape")
    g.add_argument("--write-demo", metavar="PATH", help="write the invented demo data to PATH and stop")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    ap.add_argument("--limit", type=int, default=8, help="findings shown per rule in the text report")
    a = ap.parse_args(argv)
    try:
        if a.write_demo:
            data, _ = sd.make_demo()
            with open(a.write_demo, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            print(f"wrote {a.write_demo}")
            return 0
        data = sd.make_demo()[0] if a.demo else sd.load(a.file)
        rep = sd.diagnose(data)
    except (sd.DataError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"counts": rep.counts, "checked": rep.checked, "findings": [f.as_dict() for f in rep.findings]}, indent=1))
    else:
        print(sd.text_report(rep, a.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
