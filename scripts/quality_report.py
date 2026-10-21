"""Data-quality report on a Synthea patients.csv. Prints counts only, never patient values.

  python scripts/quality_report.py [path/to/patients.csv] [--json]
"""

import csv
import json
import sys
from pathlib import Path

from him_workbench.matching import Patient
from him_workbench.quality import analyze


def load_raw(path: Path) -> list[Patient]:
    """Keep values exactly as written (no trimming) so whitespace problems are visible."""
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            out.append(Patient(
                id=str(i),
                first=row.get("FIRST", ""),
                last=row.get("LAST", ""),
                dob=row.get("BIRTHDATE", ""),
                sex=row.get("GENDER", ""),
                zip=row.get("ZIP", ""),
                phone=row.get("PHONE", ""),
            ))
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = Path(args[0]) if args else Path("data/synthea/csv/patients.csv")
    if not path.exists():
        sys.exit(f"{path} not found. Run ./scripts/generate_synthea.sh first.")
    report = analyze(load_raw(path))
    print(json.dumps(report.to_dict(), indent=2) if "--json" in sys.argv else report.text())


if __name__ == "__main__":
    main()
