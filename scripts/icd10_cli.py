"""Look up ICD-10-CM codes or search by text.

  python scripts/icd10_cli.py E11.9
  python scripts/icd10_cli.py "type 2 diabetes kidney"
  python scripts/icd10_cli.py --file data/icd10cm/<codes file> "search text"
"""

import sys
from pathlib import Path

from him_workbench.icd10 import ICD10Index, looks_like_code


def find_file() -> Path:
    cands = sorted(
        p for p in Path("data/icd10cm").rglob("*.txt")
        if "codes" in p.name.lower()
        and "addenda" not in p.name.lower()
        and "order" not in p.name.lower()
    )
    if not cands:
        sys.exit("No codes file found in data/icd10cm. Run ./scripts/fetch_icd10.sh first.")
    return cands[-1]


def main() -> None:
    args = sys.argv[1:]
    path = None
    if args[:1] == ["--file"] and len(args) >= 3:
        path, args = Path(args[1]), args[2:]
    if not args:
        sys.exit(__doc__)
    query = " ".join(args)
    idx = ICD10Index.from_file(path or find_file())
    print(f"[{idx.source}, {len(idx)} codes. Reference lookup only, not coding advice.]\n")
    if looks_like_code(query):
        d = idx.describe(query)
        for k, v in d.items():
            print(f"{k}: {v}")
    else:
        for e in idx.search(query, limit=15):
            print(f"{e.display:9} {e.description}")


if __name__ == "__main__":
    main()
