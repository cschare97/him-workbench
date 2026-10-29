"""Start the local web UI.

  pip install -e ".[ui]"
  python scripts/ui_cli.py --model gemma4:e4b      (open http://127.0.0.1:5050)
  add --drugs to turn on drug-name lookup (sends drug names to NLM over the internet)
  add --education to turn on disease education from MedlinePlus (sends codes and disease names to NLM over the internet)
  add --fhir PATH to read patients from FHIR files (a Patient, a Bundle, or NDJSON; a file or a folder)
                   also turns on Document search: a generated note per patient, searched by meaning via
                   a local Ollama embedding model (ollama pull nomic-embed-text; HIM_EMBED_MODEL to change it)
  add --ccd PATH to read C-CDA / CCD documents (one .xml file, or a folder of them); see the CCD documents page
  add --demo-data to load invented referrals, ROI requests and quality history from data/demo for the Dashboard
                   (create it first: python scripts/seed_demo_cli.py). Your real trackers are not touched.
  add --sync PATH to check a scheduling/EHR export (JSON, see README "Sync diagnostic"); --demo-data loads invented data for it
  add --demo-dups to inject 15 known synthetic duplicates (ids start with "dup-")
  HCPCS Level II loads automatically from data/hcpcs if present (./scripts/fetch_hcpcs.sh)

Binds to 127.0.0.1 only. Synthetic data only. The model is optional: without one, every page
works and only the prompt bar says to pick a model.
"""

import os
import sys

from assistant_cli import with_demo_dups
from him_workbench.data import find_hcpcs_file, find_icd_file, load_patients
from him_workbench.icd10 import ICD10Index
from him_workbench.hcpcs import HCPCSIndex
from him_workbench.analytics import QualityHistory
from him_workbench.cda import ccd_files_from
from him_workbench.chart import chart_files_from
from him_workbench.medlineplus import MedlinePlusClient
from him_workbench.roi import RoiStore
from him_workbench.referrals import ReferralStore
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
    hcpcs_path = find_hcpcs_file()
    hcpcs = None
    if hcpcs_path:
        try:
            hcpcs = HCPCSIndex.from_file(hcpcs_path)
        except ValueError as exc:
            print(f"HCPCS file found but could not be read, continuing without it: {exc}")
    patients, source = load_patients(args)
    if "--demo-dups" in args and patients:
        before = len(patients)
        patients = with_demo_dups(patients)
        added = len(patients) - before
        source += f"; plus {added} demo duplicates added (ids start with dup-)"
        print(f"({added} synthetic duplicates injected; their ids start with 'dup-')")
    print(f"ICD-10: {icd.source if icd else 'NOT LOADED'} | HCPCS: {hcpcs.source if hcpcs else 'NOT LOADED'} "
          f"| patients: {len(patients)} | model: {model or 'none'}")
    print(f"Open http://127.0.0.1:{port}   (Ctrl+C to stop)")
    drugs = RxNormClient() if "--drugs" in args else None
    if drugs:
        print("Drug lookup ON: drug names you ask about are sent to NLM's RxNav service.")
    education = MedlinePlusClient() if "--education" in args else None
    if education:
        print("Disease education ON: codes and disease names you ask about are sent to NLM's MedlinePlus service.")
    chart_files = chart_files_from(args[args.index("--fhir") + 1]) if "--fhir" in args else {}
    ccd_files = ccd_files_from(args[args.index("--ccd") + 1]) if "--ccd" in args else []
    sync_data = None
    if "--sync" in args:
        from him_workbench.sync_diagnostic import load as load_sync
        sync_data = load_sync(args[args.index("--sync") + 1])
    demo = "--demo-data" in args
    base = "data/demo" if demo else "data"
    if demo:
        print("DEMO DATA: referrals, ROI requests and quality history come from data/demo (invented). Real trackers are not loaded.")
    create_app(icd=icd, hcpcs=hcpcs, patients=patients, model=model, drugs=drugs, education=education, patient_source=source,
               chart_files=chart_files, ccd_files=ccd_files, roi_store=RoiStore(f"{base}/roi.db"),
               referral_store=ReferralStore(f"{base}/referrals.db"), quality_history=QualityHistory(f"{base}/quality.db"),
               demo_data=demo, sync_data=sync_data).run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
