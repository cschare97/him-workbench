"""Run the diabetes measures over a folder of Synthea FHIR bundles.

Usage:
    python3 report.py ~/synthea/output_caregap/fhir --csv care_gaps.csv
"""
import argparse
import csv
from collections import Counter

from fhir_load import iter_patient_files, load_patient
from measures import SIMPLIFICATIONS, evaluate, has_diabetes


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


def summarize(rows):
    lines = []
    for measure in sorted({r["measure"] for r in rows}):
        counts = Counter(r["status"] for r in rows if r["measure"] == measure)
        denom = counts["met"] + counts["not met"]
        rate = f"{100 * counts['met'] / denom:.1f}%" if denom else "n/a"
        label = "failing (inverse)" if measure.startswith("CMS122") else "passing"
        lines.append(f"{measure}: denominator {denom}, met {counts['met']} ({rate} {label}), "
                     f"excluded {counts['excluded']}, not in denominator {counts['not in denominator']}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fhir_dir")
    parser.add_argument("--csv", help="write one row per patient per measure")
    args = parser.parse_args()
    rows = run(args.fhir_dir)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["patient_id"])
            writer.writeheader()
            writer.writerows(rows)
    for line in summarize(rows):
        print(line)
    print("\nSimplifications vs. the official specs:")
    for item in SIMPLIFICATIONS:
        print(f"- {item}")


if __name__ == "__main__":
    main()
