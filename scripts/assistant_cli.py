"""Ask questions in plain English; a local Ollama model calls the matcher and ICD-10 lookup.

  python scripts/assistant_cli.py                 (lists your Ollama models)
  python scripts/assistant_cli.py --model NAME    (starts the chat)
  add --demo-dups to inject 15 known synthetic duplicates (ids start with "dup-")

Needs Ollama running. To use Ollama on another machine: export OLLAMA_HOST=http://<ip>:11434
Synthetic data only. Do not type real patient information.
"""

import os
import random
import sys
from pathlib import Path

from evaluate_matching import CORRUPTIONS, load
from him_workbench.assistant import Assistant, list_models, ollama_chat
from him_workbench.icd10 import ICD10Index
from him_workbench.matching import Patient

BANNER = ("Synthetic data only. Do not enter real patient information.\n"
          "Reference tool, not coding, billing, or clinical advice. Type 'quit' to exit.\n")


def with_demo_dups(patients, n=15, seed=7):
    rng = random.Random(seed)
    out = list(patients)
    for p in rng.sample(patients, min(n, len(patients))):
        dup = CORRUPTIONS[rng.choice(list(CORRUPTIONS))](p, rng)
        if dup:
            out.append(Patient(f"dup-{p.id}", dup.first, dup.last, dup.dob, dup.sex, dup.zip))
    return out


def show_result(name: str, r: dict) -> None:
    """Print the tool's raw result. This is the source of truth, not the model's retelling."""
    if "error" in r:
        print(f"  [result] ERROR: {r['error']}")
    elif name == "search_icd10":
        print(f"  [result] {len(r['results'])} code(s) from {r.get('source_file')}")
        for x in r["results"]:
            print(f"    {x['code']:9} {x['description']}")
    elif name == "lookup_icd10":
        print(f"  [result] {r.get('status')}: {r.get('code')} {r.get('description', r.get('note', ''))}")
    elif name in ("scan_duplicates", "compare_patients"):
        pairs = r["pairs"] if name == "scan_duplicates" else [r]
        if name == "scan_duplicates":
            print(f"  [result] {len(pairs)} pair(s) from {r['records_scanned']} records")
        for x in pairs:
            f = " ".join(f"{k}={v}" for k, v in x["fields"].items())
            print(f"    {x['a'][:12]} ~ {x['b'][:16]}  score={x['score']}  {x['label'].upper()}  [{f}]")
            for why in x.get("reasons", []):
                print(f"        held for review: {why}")


def find_icd() -> Path | None:
    c = sorted(p for p in Path("data/icd10cm").rglob("*.txt")
               if "codes" in p.name.lower() and "addenda" not in p.name.lower()
               and "order" not in p.name.lower())
    return c[-1] if c else None


def main() -> None:
    args = sys.argv[1:]
    model = os.environ.get("HIM_MODEL")
    if "--model" in args:
        model = args[args.index("--model") + 1]
    if not model:
        try:
            names = list_models()
        except Exception as exc:
            sys.exit(f"Could not reach Ollama ({exc}). Is it running? OLLAMA_HOST={os.environ.get('OLLAMA_HOST', 'default')}")
        print("Pick a model that supports tool calling, then run again with --model NAME:")
        for n in names:
            print("  ", n)
        return

    icd_path = find_icd()
    icd = ICD10Index.from_file(icd_path) if icd_path else None
    pat_path = Path("data/synthea/csv/patients.csv")
    patients = load(pat_path) if pat_path.exists() else []
    if "--demo-dups" in args and patients:
        patients = with_demo_dups(patients)
        print("(15 synthetic duplicates injected; their ids start with 'dup-')")
    print(BANNER)
    print(f"Model: {model} | ICD-10: {icd.source if icd else 'NOT LOADED'} | "
          f"Synthetic patients: {len(patients)}\n")

    bot = Assistant(ollama_chat(model), icd=icd, patients=patients,
                    on_tool=lambda n, a: print(f"  [tool] {n} {a}"),
                    on_result=show_result)
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"quit", "exit"}:
            break
        if q:
            try:
                print("\n" + bot.ask(q) + "\n")
            except Exception as exc:
                print(f"\nError talking to Ollama: {exc}\n")


if __name__ == "__main__":
    main()
