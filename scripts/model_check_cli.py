"""Run the fixed question set against one or more Ollama models and print pass/fail.

  python scripts/model_check_cli.py                       (uses HIM_MODEL)
  python scripts/model_check_cli.py --models gemma4:e4b,qwen3.5:9b
  add --save FILE to write the full questions and answers as JSON (synthetic data only)

Same data as assistant_cli.py --demo-dups. Needs Ollama running. A few minutes per model.
"""

import json
import os
import sys
from pathlib import Path

from assistant_cli import find_icd, with_demo_dups
from evaluate_matching import load
from him_workbench.assistant import ollama_chat
from him_workbench.icd10 import ICD10Index
from him_workbench.model_check import CASES, run_all


def main() -> None:
    args = sys.argv[1:]
    models = []
    if "--models" in args:
        models = [m for m in args[args.index("--models") + 1].split(",") if m]
    elif os.environ.get("HIM_MODEL"):
        models = [os.environ["HIM_MODEL"]]
    if not models:
        sys.exit("Give --models name1,name2 or set HIM_MODEL.")
    save = args[args.index("--save") + 1] if "--save" in args else None

    icd_path = find_icd()
    pat_path = Path("data/synthea/csv/patients.csv")
    if not icd_path or not pat_path.exists():
        sys.exit("Need data/icd10cm and data/synthea. Run fetch_icd10.sh and generate_synthea.sh first.")
    icd = ICD10Index.from_file(icd_path)
    patients = with_demo_dups(load(pat_path))

    table: dict[str, dict[str, bool]] = {}
    saved = {}
    for model in models:
        print(f"\nModel: {model}  ({len(CASES)} questions)")
        results = run_all(ollama_chat(model), icd, patients)
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            tools = ",".join(r.tools) or "-"
            print(f"  {mark}  {r.name:<18} {tools:<42} {r.seconds:>5}s")
            if not r.passed:
                print(f"        {r.reason}")
        ok = sum(r.passed for r in results)
        print(f"  Score: {ok}/{len(results)}")
        table[model] = {r.name: r.passed for r in results}
        saved[model] = [r.__dict__ for r in results]

    if len(models) > 1:
        print("\nSummary")
        print(f"  {'':<18}" + "".join(f"{m[:16]:<18}" for m in models))
        for c in CASES:
            print(f"  {c.name:<18}" + "".join(f"{'PASS' if table[m][c.name] else 'FAIL':<18}" for m in models))
    if save:
        Path(save).write_text(json.dumps(saved, indent=2))
        print(f"\nSaved to {save}")


if __name__ == "__main__":
    main()
