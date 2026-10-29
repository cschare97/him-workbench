"""Read a C-CDA / CCD document and print what is in it (synthetic data only).

  python scripts/ccd_cli.py tests/fixtures/sample_ccd.xml

Local only, no network. Prints the document header, how each section was read, what the document
states that is not a list item ("no known allergies", a refused vaccine), the checks against the CCD
requirements, and then the same chart view as scripts/chart_cli.py. Exit code 1 if the file cannot
be read as a C-CDA document.
"""

import sys
from pathlib import Path

from him_workbench.cda import CdaError, load_ccd, text_report


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    try:
        doc = load_ccd(path)
    except (OSError, CdaError) as e:
        print(f"Could not read {path}: {e}")
        return 1
    print(text_report(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
