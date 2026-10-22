"""Start the local web UI.

  pip install -e ".[ui]"
  python scripts/ui_cli.py --model gemma4:e4b      (open http://127.0.0.1:5050)
  add --demo-dups to inject 15 known synthetic duplicates (ids start with "dup-")

Binds to 127.0.0.1 only. Synthetic data only. The model is optional: without one, every page
works and only the prompt bar says to pick a model.
"""

import os
import sys
from pathlib import Path

from assistant_cli import with_demo_dups
from him_workbench.data import find_icd_file, load_synthea_patients
from him_workbench.icd10 import ICD10Index


def main() -> None:
    try:
        from him_workbench.webapp import create_app
    except ModuleNotFoundError as exc:
        if exc.name == "flask":
            sys.exit('Flask is not installed. Run: pip install -e ".[ui]"')
        raise
    args = sys.argv[1:]
    model = os.environ.get("HIM_MODEL")
    if "--model" in args:
        model = args[args.index("--model") + 1]
    port = int(args[args.index("--port") + 1]) if "--port" in args else 5050

    icd_path = find_icd_file()
    icd = ICD10Index.from_file(icd_path) if icd_path else None
    pat_path = Path("data/synthea/csv/patients.csv")
    patients = load_synthea_patients(pat_path) if pat_path.exists() else []
    if "--demo-dups" in args and patients:
        patients = with_demo_dups(patients)
        print("(15 synthetic duplicates injected; their ids start with 'dup-')")
    print(f"ICD-10: {icd.source if icd else 'NOT LOADED'} | patients: {len(patients)} | model: {model or 'none'}")
    print(f"Open http://127.0.0.1:{port}   (Ctrl+C to stop)")
    create_app(icd=icd, patients=patients, model=model).run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
