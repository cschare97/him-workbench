"""Convert Synthea FHIR bundles into SimEHR case files (https://github.com/HealthRex/SimEHR).

SimEHR is an open-source EHR simulation for testing computer-use agents. Its charts are plain JSON
(public/cases/<id>.json plus an index.json manifest, schema in SimEHR's public/cases/SCHEMA.md).
This reads the same local Synthea bundles the workbench already uses. No network, synthetic data only.

    python scripts/simehr_export_cli.py --out /path/to/SimEHR/public/cases data/synthea/fhir/Abe_Rogahn_*.json

Choices, so nothing is quietly changed:
  - Problems are SNOMED "(disorder)"/"(disease)" conditions only, the same rule chart.py uses. Findings and
    situations are dropped and counted. Synthea has no ICD-10, so the code system is written as "SNOMED-CT".
  - No reference ranges exist in Synthea, so refLow/refHigh/flag are left empty. Nothing is flagged H or L.
  - SimEHR has no questionnaire category, so survey answers (PHQ-9, GAD-7, ...) go in as "social-history".
  - Immunizations go in orderHistory as "Immunization/Injection" (SimEHR has no immunization list).
  - Blood pressure is renamed Systolic BP / Diastolic BP, the names the SimEHR sidebar looks for.
  - Lab results are grouped into panels using Synthea's DiagnosticReport; screenings get the Mental Health
    Screening panel so SimEHR shows them on Flowsheets.
  - Left empty: advisories, healthMaintenance, recentActivity. Nothing is invented for them.
Credit: SimEHR code is Apache-2.0, its sample data is CC BY 4.0. Synthea is by MITRE.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from him_workbench.chart import (  # noqa: E402
    PROBLEM_TAGS, _categories, _coded, _date, _medication_name, _observation_value, _ref_key, clean, semantic_tag,
)
from him_workbench.fhir import _pick_name  # noqa: E402

_CLASS = {"AMB": "ambulatory", "IMP": "inpatient", "EMER": "emergency", "ACUTE": "inpatient", "VR": "ambulatory"}
_SEX = {"male": "M", "female": "F"}
# SimEHR's sidebar finds blood pressure by these exact names (components/PatientSidebar.tsx).
_NAME_FIX = {"Systolic Blood Pressure": "Systolic BP", "Diastolic Blood Pressure": "Diastolic BP"}
_UNIT_FIX = {"Cel": "\u00b0C", "[degF]": "\u00b0F"}   # UCUM codes would otherwise show raw


def _unit(u: str) -> str:
    u = _UNIT_FIX.get(u, u)
    return "" if u.startswith("{") else u   # annotation units such as {score} mean "no unit"


_SCREEN = re.compile(r"PHQ|GAD-?7|AUDIT|DAST|HARK|PRAPARE|depress|anxiety", re.I)
_SCREEN_PANEL = "Mental Health Screening"   # SimEHR moves observations with this panel to Flowsheets


_OBS_GROUP = {"vital-signs": "vital-signs", "laboratory": "laboratory", "social-history": "social-history",
              "survey": "social-history"}


def _ext_text(res: dict, suffix: str) -> str:
    for e in res.get("extension") or []:
        if str(e.get("url", "")).endswith(suffix):
            for sub in e.get("extension") or []:
                if sub.get("url") == "text":
                    return str(sub.get("valueString") or "")
    return ""


def _age(dob: str, on: str) -> int:
    try:
        b, d = date.fromisoformat(dob), date.fromisoformat(on)
    except ValueError:
        return 0
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def _patient(res: dict, as_of: str, allergies: list) -> dict:
    name = _pick_name(res.get("name") or []) or {}
    given = [g for g in name.get("given") or [] if g]
    last = str(name.get("family") or "")
    first = given[0] if given else ""
    addr = next(iter(res.get("address") or []), {})
    street = ", ".join(x for x in [", ".join(addr.get("line") or []), addr.get("city", ""),
                                   f"{addr.get('state', '')} {addr.get('postalCode', '')}".strip()] if x)
    phones = [{"label": str(t.get("use") or "phone").capitalize(), "num": str(t["value"])}
              for t in res.get("telecom") or [] if t.get("system") == "phone" and t.get("value")]
    if phones:
        phones[0]["star"] = True
    lang = ""
    for c in res.get("communication") or []:
        lang = _coded(c.get("language")).display or lang
    dob = str(res.get("birthDate") or "")
    out = {
        # SYN- plus the first 8 characters of the Synthea id: unique per patient, plainly synthetic.
        "mrn": "SYN-" + str(res.get("id", ""))[:8].upper(),
        "name": f"{last}, {first}", "legal": f"{last}, {' '.join(given)}",
        "dob": dob, "sex": _SEX.get(str(res.get("gender", "")).lower(), "U"), "age": _age(dob, as_of),
        "language": lang.split(" (")[0], "race": _ext_text(res, "us-core-race"),
        "ethnicity": _ext_text(res, "us-core-ethnicity"), "address": street, "phones": phones,
        "allergies": allergies,
    }
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def _allergy(res: dict) -> dict:
    out = {"allergen": clean(_coded(res.get("code")).display)}
    reactions = [m for r in res.get("reaction") or [] for m in r.get("manifestation") or []]
    if reactions:
        out["reaction"] = clean(_coded(reactions[0]).display)
    sev = next((r.get("severity") for r in res.get("reaction") or [] if r.get("severity")), None)
    if sev:
        out["severity"] = str(sev).capitalize()
    return out


def _encounter(res: dict) -> dict:
    period = res.get("period") or {}
    types = res.get("type") or []
    cls = str((res.get("class") or {}).get("code") or "")
    out = {
        "id": str(res["id"]), "class": _CLASS.get(cls, cls.lower()),
        "type": clean(_coded(types[0]).display) if types else "Visit",
        "date": _date(period.get("start")),
        "dischargeDate": _date(period.get("end")) if cls in ("IMP", "ACUTE") else None,
        "department": next((str(l["location"]["display"]) for l in res.get("location") or []
                            if isinstance(l.get("location"), dict) and l["location"].get("display")), ""),
        "provider": next((str(p["individual"]["display"]) for p in res.get("participant") or []
                          if isinstance(p.get("individual"), dict) and p["individual"].get("display")), ""),
        "reason": next((clean(_coded(r).display) for r in res.get("reasonCode") or []), ""),
    }
    return {k: v for k, v in out.items() if v not in ("", None) or k == "dischargeDate"}


def _status_word(code: str) -> str:
    return {"active": "Active", "recurrence": "Active", "relapse": "Active", "resolved": "Resolved",
            "remission": "Resolved", "inactive": "Resolved"}.get(code, code.capitalize())


def _dosage(res: dict) -> dict:
    d = next(iter(res.get("dosageInstruction") or []), {})
    out: dict = {}
    if d.get("text"):
        out["sig"] = str(d["text"])
    route = _coded(d.get("route")).display
    if route:
        out["route"] = route
    rep = (d.get("timing") or {}).get("repeat") or {}
    if rep.get("frequency") and rep.get("period") is not None:
        unit = {"d": "day", "h": "hour", "wk": "week", "mo": "month"}.get(str(rep.get("periodUnit")), "")
        if unit:
            out["freq"] = f"{rep['frequency']} per {rep['period']:g} {unit}"
    dose = next(iter(d.get("doseAndRate") or []), {}).get("doseQuantity") or {}
    if dose.get("value") is not None:
        out["dose"] = f"{dose['value']:g} {dose.get('unit') or ''}".strip()
    return out


def _observations(res: dict, group: str, enc_id: str, n: list) -> list[dict]:
    when = str(res.get("effectiveDateTime") or "")[:16]
    if not when:
        return []
    parts = []
    q = res.get("valueQuantity")
    if _observation_value(res):
        parts.append((_coded(res.get("code")), q if isinstance(q, dict) else None, _observation_value(res)))
    else:
        for c in res.get("component") or []:
            if isinstance(c, dict) and _observation_value(c):
                cq = c.get("valueQuantity")
                parts.append((_coded(c.get("code")), cq if isinstance(cq, dict) else None, _observation_value(c)))
    out = []
    for what, qty, text in parts:
        if not what.display:
            continue
        n[0] += 1
        value: object = text
        unit = ""
        if qty and qty.get("value") is not None:
            v = qty["value"]
            value = round(v, 2) if isinstance(v, float) else v
            unit = _unit(str(qty.get("unit") or ""))
        o = {"id": f"o{n[0]}", "code": what.code, "system": "LOINC" if "loinc" in what.system else what.system,
             "name": _NAME_FIX.get(clean(what.display), clean(what.display)), "category": group, "value": value, "unit": unit,
             "refLow": None, "refHigh": None, "flag": None, "status": "final", "datetime": when}
        if enc_id:
            o["encounterId"] = enc_id
        out.append({k: v for k, v in o.items() if v != ""})
    return out


def _note(res: dict, enc_by_ref: set, n: int) -> dict | None:
    att = next((c.get("attachment") for c in res.get("content") or [] if isinstance(c.get("attachment"), dict)), {})
    if not att.get("data"):
        return None
    try:
        text = base64.b64decode(att["data"]).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return None
    types = (res.get("type") or {}).get("coding") or [{}]
    author = next((str(a["display"]) for a in res.get("author") or [] if a.get("display")), "")
    enc = next((_ref_key(e.get("reference")) for e in (res.get("context") or {}).get("encounter") or []), "")
    out = {"id": f"n{n}", "type": str(types[0].get("display") or "Clinical note"), "author": author,
           "datetime": str(res.get("date") or "")[:16], "status": "signed", "text": text}
    if enc in enc_by_ref:
        out["encounterId"] = enc
    return {k: v for k, v in out.items() if v != ""}


def convert(bundle_path: Path, case_id: str) -> tuple[dict, Counter]:
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    resources = [e["resource"] for e in data.get("entry") or [] if isinstance(e.get("resource"), dict)]
    index = {str(r["id"]): r for r in resources if r.get("id")}
    skipped: Counter = Counter()
    patient = next((r for r in resources if r.get("resourceType") == "Patient"), None)
    if not patient:
        raise ValueError("no Patient resource in bundle")
    encounters = sorted((_encounter(r) for r in resources if r.get("resourceType") == "Encounter"),
                        key=lambda e: e.get("date", ""), reverse=True)
    enc_ids = {e["id"] for e in encounters}
    as_of = encounters[0]["date"] if encounters else date.today().isoformat()

    # Synthea's DiagnosticReport says which panel each result belongs to. SimEHR groups its Labs screen by
    # panel and accession, so without this every result at one time lands in a single group called "Lab".
    panel_of: dict[str, tuple[str, str]] = {}
    for r in resources:
        if r.get("resourceType") == "DiagnosticReport":
            name = clean(_coded(r.get("code")).display)
            acc = "SYN-" + str(r.get("id", ""))[:10].upper()
            for ref in r.get("result") or []:
                panel_of[_ref_key(ref.get("reference"))] = (name, acc)
    problems, meds, obs, notes, history = [], [], [], [], []
    allergies = []
    n_obs, n_note = [0], 0
    for r in resources:
        kind = r.get("resourceType")
        enc = _ref_key((r.get("encounter") or {}).get("reference"))
        enc = enc if enc in enc_ids else ""
        if kind == "AllergyIntolerance":
            allergies.append(_allergy(r))
        elif kind == "Condition":
            c = _coded(r.get("code"))
            if semantic_tag(c.display) not in PROBLEM_TAGS:
                skipped["Condition (finding/situation, not a problem)"] += 1
                continue
            p = {"code": c.code, "system": "SNOMED-CT", "name": clean(c.display),
                 "onset": _date(r.get("onsetDateTime"))[:4],
                 "status": _status_word(_coded(r.get("clinicalStatus")).code)}
            if enc:
                p["encounterId"] = enc
            problems.append({k: v for k, v in p.items() if v})
        elif kind == "MedicationRequest":
            what = _medication_name(r, index)
            rx = "RxNorm" if "rxnorm" in what.system else what.system
            m = {"id": f"m{len(meds) + 1}", "code": what.code, "system": rx, "name": clean(what.display),
                 "status": "active" if r.get("status") == "active" else "discontinued", "intent": "order",
                 "started": _date(r.get("authoredOn")), **_dosage(r)}
            if enc:
                m["encounterId"] = enc
            meds.append({k: v for k, v in m.items() if v})
        elif kind == "Observation":
            group = next((_OBS_GROUP[c] for c in sorted(_categories(r)) if c in _OBS_GROUP), "")
            got = _observations(r, group, enc, n_obs) if group else []
            for o in got:
                panel, acc = panel_of.get(str(r.get("id")), ("", ""))
                if group == "laboratory" and panel:
                    o["panel"], o["accession"] = panel, acc
                elif group == "social-history" and _SCREEN.search(o["name"] + " " + panel):
                    o["panel"] = _SCREEN_PANEL
            if got:
                obs += got
            else:
                skipped["Observation (no category or no value)"] += 1
        elif kind == "DocumentReference":
            n_note += 1
            note = _note(r, enc_ids, n_note)
            if note:
                notes.append(note)
            else:
                skipped["DocumentReference (no readable text)"] += 1
        elif kind == "Immunization":
            history.append({"id": f"oh{len(history) + 1}", "date": _date(r.get("occurrenceDateTime")),
                            "type": "Immunization/Injection", "name": clean(_coded(r.get("vaccineCode")).display),
                            "status": "completed"})
        elif kind not in ("Patient", "Encounter", "Medication"):
            skipped[str(kind)] += 1
    obs.sort(key=lambda o: o["datetime"], reverse=True)
    notes.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    pat = _patient(patient, as_of, allergies)
    case = {
        "id": case_id, "label": f"{pat['name']} (synthetic, Synthea)",
        "provider": encounters[0].get("provider", "") if encounters else "",
        "department": encounters[0].get("department", "") if encounters else "",
        "patient": pat, "encounters": encounters, "problems": problems, "medications": meds,
        "observations": obs, "notes": notes, "orderHistory": history,
        "advisories": [], "healthMaintenance": [], "recentActivity": [],
    }
    return case, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bundles", nargs="+", type=Path, help="Synthea FHIR bundle .json files")
    ap.add_argument("--out", type=Path, required=True, help="SimEHR's public/cases folder")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "index.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"default": "", "cases": []}
    for path in args.bundles:
        case_id = "synthea-" + path.stem.rsplit("_", 1)[-1][:8]
        try:
            case, skipped = convert(path, case_id)
        except (OSError, ValueError, KeyError) as e:
            print(f"skip {path.name}: {e}", file=sys.stderr)
            continue
        (args.out / f"{case_id}.json").write_text(json.dumps(case, indent=1), encoding="utf-8")
        manifest["cases"] = [c for c in manifest["cases"] if c["id"] != case_id] + [
            {"id": case_id, "label": case["label"], "file": f"{case_id}.json", "specialty": "Primary Care"}]
        print(f"{case_id}: {len(case['encounters'])} encounters, {len(case['problems'])} problems, "
              f"{len(case['medications'])} meds, {len(case['observations'])} observations, "
              f"{len(case['notes'])} notes, {len(case['orderHistory'])} immunizations"
              + (f"; not carried over: {dict(skipped)}" if skipped else ""))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
