"""Run the diabetes measures over a folder of Synthea FHIR bundles.

Usage:
    python3 report.py ~/synthea/output_caregap/fhir --json ../data/care_gaps.json
    python3 report.py <injected folder> --json ../data/care_gaps.json --scenarios injected_gaps.json
"""
import argparse
import csv
import json
from collections import Counter
from datetime import date

from fhir_load import iter_patient_files, load_if_diabetic
from measures import CARE, DOCUMENTATION, OPEN_ORDER, PERIOD_END, PERIOD_START, SIMPLIFICATIONS, evaluate, has_diabetes

KINDS = (CARE, DOCUMENTATION, OPEN_ORDER)


def is_gap(measure, status):
    """True when this result is a care gap. CMS122 is inverse, so 'met' is the gap there."""
    if measure.startswith("CMS122"):
        return status == "met"
    return status == "not met"


def run(fhir_dir):
    rows = []
    for path in iter_patient_files(fhir_dir):
        facts = load_if_diabetic(path)
        if facts is None or not has_diabetes(facts):
            continue  # non-diabetics can't be in any denominator
        for result in evaluate(facts):
            rows.append({"patient_id": facts.patient_id, "name": facts.name,
                         "measure": result.measure, "status": result.status,
                         "reason": result.reason, "kind": result.kind,
                         "evidence": result.evidence})
    return rows


def summary_table(rows):
    table = []
    for measure in sorted({r["measure"] for r in rows}):
        mine = [r for r in rows if r["measure"] == measure]
        counts = Counter(r["status"] for r in mine)
        denom = counts["met"] + counts["not met"]
        gap_rows = [r for r in mine if is_gap(measure, r["status"])]
        by_kind = Counter(r.get("kind", "") for r in gap_rows)
        table.append({"measure": measure, "denominator": denom, "met": counts["met"],
                      "gaps": len(gap_rows), "by_kind": {k: by_kind[k] for k in KINDS},
                      "excluded": counts["excluded"],
                      "not_in_denominator": counts["not in denominator"],
                      "rate": round(100 * counts["met"] / denom, 1) if denom else None,
                      "inverse": measure.startswith("CMS122")})
    return table


def summarize(rows):
    lines = []
    for s in summary_table(rows):
        rate = f"{s['rate']}%" if s["rate"] is not None else "n/a"
        label = "failing (inverse)" if s["inverse"] else "passing"
        kinds = ", ".join(f"{k} {n}" for k, n in s["by_kind"].items() if n)
        lines.append(f"{s['measure']}: denominator {s['denominator']}, met {s['met']} ({rate} {label}), "
                     f"gaps {s['gaps']} ({kinds or 'none'}), excluded {s['excluded']}")
    return lines


def to_json(rows, scenarios=None):
    """Page-ready snapshot: summary plus one entry per patient in at least one denominator.

    scenarios maps patient_id -> injected scenario name, so the page can label test patients.
    """
    scenarios = scenarios or {}
    patients = {}
    for r in rows:
        p = patients.setdefault(r["patient_id"], {"id": r["patient_id"], "name": r["name"],
                                                  "scenario": scenarios.get(r["patient_id"], ""),
                                                  "results": []})
        p["results"].append({"measure": r["measure"], "status": r["status"], "reason": r["reason"],
                             "gap": is_gap(r["measure"], r["status"]), "kind": r.get("kind", ""),
                             "evidence": r.get("evidence", [])})
    in_any = [p for p in patients.values()
              if any(x["status"] != "not in denominator" for x in p["results"])]
    for p in in_any:
        p["gap_count"] = sum(x["gap"] for x in p["results"])
    in_any.sort(key=lambda p: (-p["gap_count"], p["name"]))
    gap_kinds = Counter(x["kind"] for p in in_any for x in p["results"] if x["gap"])
    return {"generated": date.today().isoformat(),
            "period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
            "summary": summary_table(rows), "simplifications": SIMPLIFICATIONS,
            "gap_kinds": {k: gap_kinds[k] for k in KINDS},
            "injected": bool(scenarios),
            "patients": in_any}


def load_scenarios(path):
    with open(path, encoding="utf-8") as handle:
        return {entry["patient_id"]: entry["scenario"] for entry in json.load(handle)["changes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fhir_dir")
    parser.add_argument("--csv", help="write one row per patient per measure")
    parser.add_argument("--json", help="write the snapshot the /care-gaps page reads")
    parser.add_argument("--scenarios", help="injected_gaps.json from inject_gaps.py, to label test patients")
    args = parser.parse_args()
    rows = run(args.fhir_dir)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            fields = ["patient_id", "name", "measure", "status", "reason", "kind"]
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    if args.json:
        scenarios = load_scenarios(args.scenarios) if args.scenarios else None
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(to_json(rows, scenarios), handle, indent=1)
    for line in summarize(rows):
        print(line)
    print("\nSimplifications vs. the official specs:")
    for item in SIMPLIFICATIONS:
        print(f"- {item}")


if __name__ == "__main__":
    main()
