"""Look up HCPCS Level II codes or search by text.

  python scripts/hcpcs_cli.py E0100
  python scripts/hcpcs_cli.py "wheelchair"
  python scripts/hcpcs_cli.py --file data/hcpcs/<file> "search text"
"""

import sys
from pathlib import Path

from him_workbench.hcpcs import HCPCSIndex, looks_like_code


def find_file() -> Path:
    cands = sorted(p for p in Path("data/hcpcs").rglob("*")
                   if p.suffix.lower() in (".txt", ".csv") and "anweb" in p.name.lower()
                   and "layout" not in p.name.lower() and "correction" not in p.name.lower()
                   and "transaction" not in p.name.lower())
    if not cands:
        sys.exit("No HCPCS data file found in data/hcpcs. Run ./scripts/fetch_hcpcs.sh first.")
    return cands[-1]


def main() -> None:
    args = sys.argv[1:]
    path = None
    if args[:1] == ["--file"] and len(args) >= 3:
        path, args = Path(args[1]), args[2:]
    if not args:
        sys.exit(__doc__)
    query = " ".join(args)
    idx = HCPCSIndex.from_file(path or find_file())
    print(f"[{idx.source}, {len(idx)} codes. HCPCS Level II only, not CPT. Reference lookup only.]\n")
    if looks_like_code(query):
        d = idx.describe(query)
        for k, v in d.items():
            print(f"{k}: {v}")
    else:
        for e in idx.search(query, limit=15):
            print(f"{e.display:9} {e.description}")


if __name__ == "__main__":
    main()
