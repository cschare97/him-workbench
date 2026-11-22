"""Run the diabetes measures over a folder of Synthea FHIR bundles.

Usage:
    python3 report.py ~/synthea/output_caregap/fhir --csv care_gaps.csv --json ../data/care_gaps.json
"""
import argparse
import csv
import json
from collections import Counter
from datetime import date

from fhir_load import iter_patient_files, load_patient
from measures import PERIOD_END, PERIOD_START, SIMPLIFICATIONS, evaluate, has_diabetes


def is_gap(measure, status):
    """True when this result is a care gap. CMS122 is inverse, so 'met' is the gap there."""
    if measure.startswith("CMS122"):
        return status == "met"
    return status == "not met"


def run(fhir_dir):
    rows = []
    for path in iter_patient_files(fhir_dir):
        facts = load_patient(path)
        if not has_diabetes(facts):
            continue  # skip non-diabetics early; they can't be in any denominator
        for result in evaluate(facts):
            rows.append({"patient_id": facts.patient_id, "name": facts.name,
                         "measure": result.measure, "status": result.status,
                         "reason": result.reason})
    return rows


def summary_table(rows):
    table = []
    for measure in sorted({r["measure"] for r in rows}):
        counts = Counter(r["status"] for r in rows if r["measure"] == measure)
        denom = counts["met"] + counts["not met"]
        gaps = counts["met"] if measure.startswith("CMS122") else counts["not met"]
        table.append({"measure": measure, "denominator": denom, "met": counts["met"],
                      "gaps": gaps, "excluded": counts["excluded"],
                      "not_in_denominator": counts["not in denominator"],
                      "rate": round(100 * counts["met"] / denom, 1) if denom else None,
                      "inverse": measure.startswith("CMS122")})
    return table


def summarize(rows):
    lines = []
    for s in summary_table(rows):
        rate = f"{s['rate']}%" if s["rate"] is not None else "n/a"
        label = "failing (inverse)" if s["inverse"] else "passing"
        lines.append(f"{s['measure']}: denominator {s['denominator']}, met {s['met']} ({rate} {label}), "
                     f"excluded {s['excluded']}, not in denominator {s['not_in_denominator']}")
    return lines


def to_json(rows):
    """Page-ready snapshot: summary plus one entry per patient in at least one denominator."""
    patients = {}
    for r in rows:
        p = patients.setdefault(r["patient_id"], {"id": r["patient_id"], "name": r["name"], "results": []})
        p["results"].append({"measure": r["measure"], "status": r["status"], "reason": r["reason"],
                             "gap": is_gap(r["measure"], r["status"])})
    in_any = [p for p in patients.values()
              if any(x["status"] != "not in denominator" for x in p["results"])]
    for p in in_any:
        p["gap_count"] = sum(x["gap"] for x in p["results"])
    in_any.sort(key=lambda p: (-p["gap_count"], p["name"]))
    return {"generated": date.today().isoformat(),
            "period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
            "summary": summary_table(rows), "simplifications": SIMPLIFICATIONS,
            "patients": in_any}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fhir_dir")
    parser.add_argument("--csv", help="write one row per patient per measure")
    parser.add_argument("--json", help="write the snapshot the /care-gaps page reads")
    args = parser.parse_args()
    rows = run(args.fhir_dir)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["patient_id"])
            writer.writeheader()
            writer.writerows(rows)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(to_json(rows), handle, indent=1)
    for line in summarize(rows):
        print(line)
    print("\nSimplifications vs. the official specs:")
    for item in SIMPLIFICATIONS:
        print(f"- {item}")


if __name__ == "__main__":
    main()
