"""Flag A1c results that look like source-data problems, not care problems.

Two checks on every diabetic patient's HbA1c history (all dates, not just the measurement period):
- low: a value under 4%. Possible in real life (for example over-treatment), but rare enough that a
  reviewer would ask about it. Synthea produces several.
- repeat: the same value 3 or more results in a row. Real lab results almost never repeat exactly;
  this is a simulator pattern (Synthea sometimes carries one value forward week to week).

These flags never change a measure result. They are written to their own file so the care-gap
snapshot (and the summaries stored in it) is never rebuilt by this check.

Usage (from the repo root):
    python3 -m him_workbench.care_gaps.data_quality ~/synthea/output_caregap_injected --json data/data_quality.json
"""
import argparse
import json
from datetime import date

from .fhir_load import A1C_LOINC, iter_patient_files, load_if_diabetic
from .measures import PERIOD_END, PERIOD_START, has_diabetes

LOW_A1C = 4.0
REPEAT_RUN = 3
SHOW_RUNS = 2  # longest runs spelled out per patient; the rest are counted


def a1c_history(facts):
    """(day, value) for every A1c with a value and a date, oldest first."""
    return sorted((o.day, o.value) for o in facts.observations
                  if o.code == A1C_LOINC and o.value is not None and o.day is not None)


def low_values(history, limit=LOW_A1C):
    return [(day, value) for day, value in history if value < limit]


def repeat_runs(history, min_run=REPEAT_RUN):
    """Runs of identical consecutive values: (value, first day, last day, count)."""
    runs, start = [], 0
    for i in range(1, len(history) + 1):
        if i == len(history) or history[i][1] != history[start][1]:
            if i - start >= min_run:
                runs.append((history[start][1], history[start][0], history[i - 1][0], i - start))
            start = i
    return runs


def check(facts):
    """Flags for one patient, or None when nothing looks off."""
    history = a1c_history(facts)
    low, repeats = low_values(history), repeat_runs(history)
    if not low and not repeats:
        return None
    notes = []
    if low:
        lowest = min(value for _, value in low)
        notes.append(f"{len(low)} A1c result{'s' if len(low) != 1 else ''} under {LOW_A1C:g}% "
                     f"(lowest {lowest:g}%, latest on {low[-1][0].isoformat()})")
    longest = sorted(repeats, key=lambda run: (-run[3], run[1]))[:SHOW_RUNS]
    for value, first, last, count in sorted(longest, key=lambda run: run[1]):
        notes.append(f"A1c {value:g}% recorded {count} times in a row ({first.isoformat()} to {last.isoformat()})")
    if len(repeats) > SHOW_RUNS:
        notes.append(f"{len(repeats) - SHOW_RUNS} more runs of repeated values")
    low_in_period = [(day, value) for day, value in low if PERIOD_START <= day <= PERIOD_END]
    in_period = [(day, value) for day, value in history if PERIOD_START <= day <= PERIOD_END]
    # CMS122 grades only the most recent A1c in the period, so this is the value a pass or fail rests on.
    latest_low = bool(in_period) and in_period[-1][1] < LOW_A1C
    if latest_low:
        notes.insert(0, f"Most recent A1c in the measurement period is {in_period[-1][1]:g}% on "
                        f"{in_period[-1][0].isoformat()}, under {LOW_A1C:g}%. CMS122 grades the most recent value, "
                        f"so confirm it before trusting a pass")
    return {"id": facts.patient_id, "name": facts.name, "low_count": len(low),
            "low_in_period": len(low_in_period), "latest_in_period_low": latest_low,
            "repeat_runs": len(repeats), "notes": notes}


def scan(fhir_dir):
    flagged, checked = [], 0
    for path in iter_patient_files(fhir_dir):
        facts = load_if_diabetic(path)
        if facts is None or not has_diabetes(facts):
            continue
        checked += 1
        result = check(facts)
        if result:
            flagged.append(result)
    flagged.sort(key=lambda p: p["name"])
    return {"generated": date.today().isoformat(),
            "rules": {"low_a1c_under": LOW_A1C, "repeat_run_at_least": REPEAT_RUN},
            "patients_checked": checked,
            "low_patients": sum(1 for p in flagged if p["low_count"]),
            "low_in_period_patients": sum(1 for p in flagged if p["low_in_period"]),
            "latest_low_patients": sum(1 for p in flagged if p["latest_in_period_low"]),
            "repeat_patients": sum(1 for p in flagged if p["repeat_runs"]),
            "patients": flagged}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fhir_dir")
    parser.add_argument("--json", help="write the file the /care-gaps page reads (data/data_quality.json)")
    args = parser.parse_args()
    result = scan(args.fhir_dir)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1)
    print(f"Checked {result['patients_checked']} diabetic patients: {result['low_patients']} with an A1c "
          f"under {LOW_A1C:g}% ({result['low_in_period_patients']} in the measurement period, {result['latest_low_patients']} where it is the most recent in the period), {result['repeat_patients']} with the same A1c {REPEAT_RUN}+ times in a row.")


if __name__ == "__main__":
    main()
