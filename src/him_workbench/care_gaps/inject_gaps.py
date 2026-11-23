"""Add documented, reproducible workflow failures to a copy of the Synthea cohort.

Synthea's simulated care is close to perfect, so on its own the measures show almost no
gaps and none of the documentation problems real clinics have. This script copies the
diabetic patients to a new folder and changes a few of them on purpose. Every change is
written to a log, so anyone reading the repo can see exactly what was altered and why.

The source folder is never modified. Output bundles keep only the resource types the
measures read (no claims or billing resources), which also makes them much smaller.

Usage:
    python3 -m him_workbench.care_gaps.inject_gaps ~/synthea/output_caregap/fhir ~/synthea/output_caregap_injected \
        --log data/injected_gaps.json
"""
import argparse
import base64
import json
import random
import uuid
from datetime import date
from pathlib import Path

from .fhir_load import A1C_LOINC, DIABETES_CODES, RETINAL_EXAM_SNOMED, facts_from_bundle, iter_patient_files
from .measures import PERIOD_END, PERIOD_START, cms122, cms131

KEEP_TYPES = {"Patient", "Condition", "Observation", "Procedure", "Encounter",
              "DocumentReference", "ServiceRequest"}
SCENARIO_COUNTS = {"a1c_in_note": 4, "open_order": 3, "uncontrolled": 5, "outside_eye_exam": 4}
TAG = "[Test scenario added by inject_gaps.py]"
LATEST_EVENT = date(2026, 11, 1)  # keep injected dates before the simulation's end date


def _code(resource):
    return ((resource.get("code") or {}).get("coding") or [{}])[0].get("code", "")


def _day(value):
    return date.fromisoformat(value[:10]) if value else None


def _in_period(day):
    return day is not None and PERIOD_START <= day <= PERIOD_END


def _patient_ref(bundle):
    entry = next(e for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient")
    return entry.get("fullUrl") or "urn:uuid:" + entry["resource"]["id"]


def _add(bundle, resource):
    rid = str(uuid.UUID(int=random.getrandbits(128)))  # seeded, so reruns give the same ids
    resource["id"] = rid
    bundle["entry"].append({"fullUrl": "urn:uuid:" + rid, "resource": resource,
                            "request": {"method": "POST", "url": resource["resourceType"]}})


def add_note(bundle, day, text):
    _add(bundle, {
        "resourceType": "DocumentReference", "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "34109-9", "display": "Note"}]},
        "subject": {"reference": _patient_ref(bundle)}, "date": f"{day.isoformat()}T09:00:00Z",
        "content": [{"attachment": {"contentType": "text/plain; charset=utf-8",
                                    "data": base64.b64encode(f"{text}\n\n{TAG}".encode()).decode()}}],
    })


def add_a1c_order(bundle, day):
    _add(bundle, {
        "resourceType": "ServiceRequest", "status": "active", "intent": "order",
        "code": {"coding": [{"system": "http://loinc.org", "code": A1C_LOINC,
                             "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}]},
        "subject": {"reference": _patient_ref(bundle)}, "authoredOn": f"{day.isoformat()}T09:00:00Z",
        "note": [{"text": TAG}],
    })


def remove_a1c_in_period(bundle):
    """Remove structured A1c results in the period. Returns the removed (day, value) pairs."""
    removed, kept = [], []
    for e in bundle["entry"]:
        r = e["resource"]
        day = _day(r.get("effectiveDateTime"))
        if r["resourceType"] == "Observation" and _code(r) == A1C_LOINC and _in_period(day):
            removed.append((day, (r.get("valueQuantity") or {}).get("value")))
        else:
            kept.append(e)
    bundle["entry"] = kept
    return sorted(removed)


def set_latest_a1c(bundle, value):
    """Raise the most recent in-period A1c (every result on that day). Returns (day, old value)."""
    a1cs = [e["resource"] for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Observation" and _code(e["resource"]) == A1C_LOINC
            and _in_period(_day(e["resource"].get("effectiveDateTime")))]
    latest = max(_day(r["effectiveDateTime"]) for r in a1cs)
    old = None
    for r in a1cs:
        if _day(r["effectiveDateTime"]) == latest:
            old = r["valueQuantity"]["value"]
            r["valueQuantity"]["value"] = value
    return latest, old


def remove_eye_exams(bundle, since):
    """Remove retinal exam procedures on or after `since`. Returns the removed dates."""
    removed, kept = [], []
    for e in bundle["entry"]:
        r = e["resource"]
        when = r.get("performedDateTime") or (r.get("performedPeriod") or {}).get("start")
        if r["resourceType"] == "Procedure" and _code(r) == RETINAL_EXAM_SNOMED and _day(when) and _day(when) >= since:
            removed.append(_day(when))
        else:
            kept.append(e)
    bundle["entry"] = kept
    return sorted(removed)


def _event_day(days):
    """Latest day from a list that is inside the period and not after LATEST_EVENT, else a fixed fallback."""
    usable = [d for d in days if _in_period(d) and d <= LATEST_EVENT]
    return max(usable) if usable else date(2026, 6, 15)


def apply(scenario, bundle):
    """Apply one scenario to a bundle in place. Returns a plain-English description of the change."""
    if scenario == "a1c_in_note":
        removed = remove_a1c_in_period(bundle)
        day = _event_day([d for d, _v in removed])
        value = next((v for d, v in reversed(removed) if d == day), None) or 7.2
        add_note(bundle, day, f"Outside lab results reviewed. Hemoglobin A1c {value:.1f}% drawn at an "
                              f"outside lab on {day}. Scanned copy filed under Media.")
        return (f"removed {len(removed)} structured A1c result(s) in 2026 and added a note on {day} "
                f"saying an outside lab A1c was {value:.1f}%")
    if scenario == "open_order":
        removed = remove_a1c_in_period(bundle)
        day = _event_day([d for d, _v in removed])
        add_a1c_order(bundle, day)
        return f"removed {len(removed)} structured A1c result(s) in 2026 and added an A1c order on {day} with no result"
    if scenario == "uncontrolled":
        value = round(random.uniform(9.3, 11.8), 1)
        day, old = set_latest_a1c(bundle, value)
        return f"changed the most recent A1c on {day} from {old}% to {value}%"
    if scenario == "outside_eye_exam":
        removed = remove_eye_exams(bundle, since=PERIOD_START.replace(year=PERIOD_START.year - 1))
        day = _event_day(removed)
        add_note(bundle, day, f"Patient reports a dilated eye exam at an outside ophthalmology office on {day}. "
                              "No diabetic retinopathy per patient. Outside report not yet received.")
        return (f"removed {len(removed)} retinal exam record(s) since 2025 and added a note on {day} "
                "about an outside eye exam")
    raise ValueError(f"unknown scenario {scenario}")


def eligible(scenario, facts):
    """A patient is eligible when they currently PASS the measure the scenario would break."""
    if scenario in ("a1c_in_note", "open_order", "uncontrolled"):
        return cms122(facts).status == "not met"
    if scenario == "outside_eye_exam":
        return cms131(facts).status == "met"
    return False


def trim(bundle):
    bundle["entry"] = [e for e in bundle["entry"] if e["resource"]["resourceType"] in KEEP_TYPES]
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--log", default="injected_gaps.json")
    args = parser.parse_args()
    random.seed(args.seed)

    src, out = Path(args.src_dir).resolve(), Path(args.out_dir).resolve()
    if src == out:
        raise SystemExit("out_dir must be different from src_dir; the source is never modified")
    out.mkdir(parents=True, exist_ok=True)

    bundles = {}
    for path in iter_patient_files(src):
        text = path.read_text(encoding="utf-8")
        if any(f'"{c}"' in text for c in DIABETES_CODES):
            bundles[path.name] = trim(json.loads(text))

    facts = {name: facts_from_bundle(b) for name, b in bundles.items()}
    used, changes = set(), []
    for scenario, count in SCENARIO_COUNTS.items():
        pool = sorted(n for n in bundles if n not in used and eligible(scenario, facts[n]))
        picked = random.sample(pool, min(count, len(pool)))
        for name in picked:
            used.add(name)
            change = apply(scenario, bundles[name])
            changes.append({"patient_id": facts[name].patient_id, "name": facts[name].name,
                            "scenario": scenario, "change": change})

    for name, bundle in bundles.items():
        (out / name).write_text(json.dumps(bundle), encoding="utf-8")

    log = {"seed": args.seed, "source": src.name, "patients_written": len(bundles),
           "kept_resource_types": sorted(KEEP_TYPES), "changes": changes}
    Path(args.log).write_text(json.dumps(log, indent=1), encoding="utf-8")
    print(f"Wrote {len(bundles)} diabetic patient bundles to {out}")
    for c in changes:
        print(f"- {c['scenario']}: {c['name']}: {c['change']}")
    print(f"Log: {args.log}")


if __name__ == "__main__":
    main()
