"""Write checked model summaries into the snapshot, so a hosted demo can show them with no model.

Runs on your own machine with a local Ollama model. For every care gap in data/care_gaps.json:
  - asks the model for a summary in public mode (NCQA spec text is never in the prompt),
  - keeps the answer only if it passes the grounding check,
  - stores it on the result as {"model", "date", "text"}.

Answers that fail the check are counted and left out. Nothing unchecked is ever stored.
Run it after report.py, because report.py rebuilds the snapshot from scratch.

Usage (from the repo root):
    python3 care_gaps/precompute_summaries.py --model qwen2.5:7b
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date

from explain import SNAPSHOT, check_grounding, cited_sections, explain, ollama_generate, project_rules
from spec_loader import load as load_specs


def precompute(snapshot, model, specs, generate=ollama_generate, today=None, progress=None):
    """Add summaries to every gap result in place. Returns counts by outcome."""
    today = (today or date.today()).isoformat()
    counts = Counter()
    gaps = [(p, r) for p in snapshot["patients"] for r in p["results"] if r.get("gap")]
    for n, (patient, result) in enumerate(gaps, start=1):
        result.pop("summary", None)  # never keep a summary from an older run or model
        out = explain(result, specs, model=model, public=True, generate=generate)
        if out["mode"] == "model":
            result["summary"] = {"model": model, "date": today, "text": out["text"]}
            counts["kept"] += 1
        elif any(p.startswith("model unavailable") for p in out["problems"]):
            counts["model error"] += 1
        else:
            counts["rejected by check"] += 1
        if progress:
            progress(n, len(gaps), patient["name"], result["measure"], out)
    snapshot["summaries"] = {"model": model, "date": today, **counts}
    return counts


def stored_summary_ok(result, specs):
    """Re-check a stored summary before showing it, against the current rules and evidence."""
    summary = result.get("summary")
    if not summary:
        return False
    sections = cited_sections(result, specs, public=True)
    return check_grounding(summary["text"], result, result.get("evidence", []), sections,
                           project_rules(result)) == []


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="local Ollama model, e.g. qwen2.5:7b")
    args = parser.parse_args()

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    def progress(n, total, name, measure, out):
        mark = "kept" if out["mode"] == "model" else "skipped: " + "; ".join(out["problems"])
        print(f"[{n}/{total}] {name} | {measure} | {mark}", flush=True)

    counts = precompute(snapshot, args.model, load_specs(), progress=progress)
    if counts["model error"] and not counts["kept"]:
        sys.exit("Every call failed. Is Ollama running? (See the project notes on OLLAMA_HOST.)")
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    print(f"\nKept {counts['kept']}, rejected by the grounding check {counts['rejected by check']}, "
          f"model errors {counts['model error']}. Saved to {SNAPSHOT.name}.")


if __name__ == "__main__":
    main()
