"""Print one patient's chart from a FHIR bundle file (synthetic data only).

  python scripts/chart_cli.py data/synthea/fhir/SOME_PATIENT.json

Local only, no network. Shows active problems, medications, allergies, most recent vitals and labs,
recent visits and immunizations. Resource types the chart does not show are counted at the bottom.
"""

import sys
from pathlib import Path

from him_workbench.chart import load_chart, text_chart
from him_workbench.fhir import load_fhir_patients


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    try:
        chart = load_chart(path)
    except (OSError, ValueError) as e:
        print(f"Could not read {path}: {e}")
        return 1
    patients, _ = load_fhir_patients(path, include_deceased=True)
    name = f"{patients[0].first} {patients[0].last}" if patients else ""
    print(text_chart(chart, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
