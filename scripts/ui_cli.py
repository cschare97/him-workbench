"""Start the local web UI.

  pip install -e ".[ui]"
  python scripts/ui_cli.py --model gemma4:e4b      (open http://127.0.0.1:5050)
  add --drugs to turn on drug-name lookup (sends drug names to NLM over the internet)
  add --education to turn on disease education from MedlinePlus (sends codes and disease names to NLM over the internet)
  add --fhir PATH to read patients from FHIR files (a Patient, a Bundle, or NDJSON; a file or a folder)
  add --demo-dups to inject 15 known synthetic duplicates (ids start with "dup-")

Binds to 127.0.0.1 only. Synthetic data only. The model is optional: without one, every page
works and only the prompt bar says to pick a model.
"""

import os
import sys

from assistant_cli import with_demo_dups
from him_workbench.data import find_icd_file, load_patients
from him_workbench.icd10 import ICD10Index
from him_workbench.medlineplus import MedlinePlusClient
from him_workbench.rxnorm import RxNormClient


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
    patients, source = load_patients(args)
    if "--demo-dups" in args and patients:
        before = len(patients)
        patients = with_demo_dups(patients)
        added = len(patients) - before
        source += f"; plus {added} demo duplicates added (ids start with dup-)"
        print(f"({added} synthetic duplicates injected; their ids start with 'dup-')")
    print(f"ICD-10: {icd.source if icd else 'NOT LOADED'} | patients: {len(patients)} | model: {model or 'none'}")
    print(f"Open http://127.0.0.1:{port}   (Ctrl+C to stop)")
    drugs = RxNormClient() if "--drugs" in args else None
    if drugs:
        print("Drug lookup ON: drug names you ask about are sent to NLM's RxNav service.")
    education = MedlinePlusClient() if "--education" in args else None
    if education:
        print("Disease education ON: codes and disease names you ask about are sent to NLM's MedlinePlus service.")
    create_app(icd=icd, patients=patients, model=model, drugs=drugs, education=education, patient_source=source).run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
