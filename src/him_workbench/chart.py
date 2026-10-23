"""Build a patient chart (problems, medications, visits, vitals and labs, immunizations, allergies)
from the resources in a FHIR bundle. Local files only, no network.

Written against Synthea's R4 export, where a bundle holds one patient's whole record. Only the fields below
are read. Anything this module does not use is ignored, and `Chart.skipped` counts resources whose type the
chart does not show, so nothing disappears without a count.

Problem list: Synthea writes a Condition for every diagnosis, finding and situation. SNOMED display names end
with a type tag such as "(disorder)" or "(situation)". Disorders go in `problems`. Everything else goes in
`findings`, so a "Medication review due (situation)" never shows up as a disease.
Limit: the tag cannot tell clinical findings from social ones. "Prediabetes (finding)" and "Body mass index 30+ -
obesity (finding)" are clinical, but "Full-time employment (finding)" and "Stress (finding)" are social history,
and all of them land in `findings`. The chart labels that section so nobody reads it as "not diagnoses".

Vitals and labs: the chart keeps the most recent value for each test, with the date. The full history is not kept.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROBLEM_TAGS = {"disorder", "disease"}
_VITALS = "vital-signs"
_LAB = "laboratory"


@dataclass
class Coded:
    display: str
    code: str = ""
    system: str = ""


@dataclass
class Problem:
    what: Coded
    status: str = ""
    onset: str = ""
    resolved: str = ""


@dataclass
class Medication:
    what: Coded
    status: str = ""
    date: str = ""


@dataclass
class Visit:
    kind: str
    start: str = ""
    end: str = ""
    cls: str = ""


@dataclass
class Result:
    what: Coded
    value: str = ""
    date: str = ""


@dataclass
class Immunization:
    what: Coded
    date: str = ""


@dataclass
class Allergy:
    what: Coded
    status: str = ""


@dataclass
class Chart:
    patient_id: str = ""
    problems: list[Problem] = field(default_factory=list)
    findings: list[Problem] = field(default_factory=list)
    medications: list[Medication] = field(default_factory=list)
    visits: list[Visit] = field(default_factory=list)
    vitals: list[Result] = field(default_factory=list)
    labs: list[Result] = field(default_factory=list)
    immunizations: list[Immunization] = field(default_factory=list)
    allergies: list[Allergy] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


def _coded(cc) -> Coded:
    """First coding of a CodeableConcept, falling back to its text."""
    if not isinstance(cc, dict):
        return Coded("")
    codings = [c for c in cc.get("coding") or [] if isinstance(c, dict)]
    text = str(cc.get("text") or "")
    if codings:
        c = codings[0]
        return Coded(str(c.get("display") or text), str(c.get("code") or ""), str(c.get("system") or ""))
    return Coded(text)


def _status(cc) -> str:
    return _coded(cc).code


def _date(value) -> str:
    return str(value or "")[:10]


def semantic_tag(display: str) -> str:
    """The trailing "(tag)" of a SNOMED display name, lower-case, or "" when there is none."""
    d = display.strip()
    if d.endswith(")") and "(" in d:
        return d[d.rindex("(") + 1:-1].strip().lower()
    return ""


def clean(display: str) -> str:
    """Drop the trailing SNOMED type tag, "Well child visit (procedure)" -> "Well child visit"."""
    tag = semantic_tag(display)
    return display[:display.rindex("(")].strip() if tag else display


def _condition(res: dict) -> Problem:
    return Problem(
        what=_coded(res.get("code")),
        status=_status(res.get("clinicalStatus")),
        onset=_date(res.get("onsetDateTime")),
        resolved=_date(res.get("abatementDateTime")),
    )


def _observation_value(res: dict) -> str:
    q = res.get("valueQuantity")
    if isinstance(q, dict) and q.get("value") is not None:
        v = q["value"]
        v = round(v, 2) if isinstance(v, float) else v
        return f"{v} {q.get('unit') or ''}".strip()
    if res.get("valueCodeableConcept"):
        return _coded(res["valueCodeableConcept"]).display
    if res.get("valueString") is not None:
        return str(res["valueString"])
    return ""


def _categories(res: dict) -> set[str]:
    out = set()
    for cat in res.get("category") or []:
        for c in (cat or {}).get("coding") or []:
            out.add(str(c.get("code") or ""))
    return out


def _ref_key(ref: str) -> str:
    """'urn:uuid:abc' and 'Medication/abc' both become 'abc'."""
    return str(ref or "").replace("urn:uuid:", "").rsplit("/", 1)[-1]


def _medication_name(res: dict, index: dict) -> Coded:
    direct = _coded(res.get("medicationCodeableConcept"))
    if direct.display:
        return direct
    ref = res.get("medicationReference")
    if isinstance(ref, dict):
        target = index.get(_ref_key(ref.get("reference")))
        if isinstance(target, dict) and target.get("resourceType") == "Medication":
            named = _coded(target.get("code"))
            if named.display:
                return named
        if ref.get("display"):
            return Coded(str(ref["display"]))
    return Coded("(name not in record)")


def chart_from_resources(resources, patient_id: str = "") -> Chart:
    chart = Chart(patient_id=patient_id)
    resources = [r for r in resources]
    index = {str(r["id"]): r for r in resources if isinstance(r, dict) and r.get("id")}
    latest: dict[tuple[str, str], Result] = {}
    for res in resources:
        if not isinstance(res, dict):
            continue
        kind = res.get("resourceType")
        if kind == "Patient":
            chart.patient_id = chart.patient_id or str(res.get("id") or "")
        elif kind == "Condition":
            p = _condition(res)
            (chart.problems if semantic_tag(p.what.display) in PROBLEM_TAGS else chart.findings).append(p)
        elif kind == "MedicationRequest":
            chart.medications.append(Medication(
                what=_medication_name(res, index),
                status=str(res.get("status") or ""),
                date=_date(res.get("authoredOn")),
            ))
        elif kind == "Encounter":
            period = res.get("period") or {}
            types = res.get("type") or []
            chart.visits.append(Visit(
                kind=_coded(types[0]).display if types else "",
                start=_date(period.get("start")), end=_date(period.get("end")),
                cls=str((res.get("class") or {}).get("code") or ""),
            ))
        elif kind == "Observation":
            cats = _categories(res)
            group = _VITALS if _VITALS in cats else _LAB if _LAB in cats else ""
            what = _coded(res.get("code"))
            value = _observation_value(res)
            if not group or not what.display or not value:
                chart.skipped["Observation"] += 1
                continue
            r = Result(what, value, _date(res.get("effectiveDateTime")))
            key = (group, what.code or what.display)
            if key not in latest or r.date >= latest[key].date:
                latest[key] = r
        elif kind == "Immunization":
            chart.immunizations.append(Immunization(_coded(res.get("vaccineCode")),
                                                    _date(res.get("occurrenceDateTime"))))
        elif kind == "AllergyIntolerance":
            chart.allergies.append(Allergy(_coded(res.get("code")), _status(res.get("clinicalStatus"))))
        elif kind != "Medication":  # used to name medication requests, not shown on its own
            chart.skipped[str(kind)] += 1
    for (group, _), r in latest.items():
        (chart.vitals if group == _VITALS else chart.labs).append(r)
    chart.visits.sort(key=lambda v: v.start, reverse=True)
    chart.problems.sort(key=lambda p: p.onset, reverse=True)
    chart.medications.sort(key=lambda m: m.date, reverse=True)
    chart.immunizations.sort(key=lambda i: i.date, reverse=True)
    chart.vitals.sort(key=lambda r: r.what.display)
    chart.labs.sort(key=lambda r: r.what.display)
    return chart


def load_chart(path) -> Chart:
    """Read one FHIR Bundle (or a single resource) from a .json file. Raises ValueError/OSError when unreadable."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        resources = [e["resource"] for e in data.get("entry") or []
                     if isinstance(e, dict) and isinstance(e.get("resource"), dict)]
    elif isinstance(data, dict):
        resources = [data]
    else:
        raise ValueError("not a FHIR resource or Bundle")
    return chart_from_resources(resources)


def chart_files_from(path) -> dict[str, Path]:
    """Map patient id -> bundle file, without opening the files.
    A folder of Synthea bundles (named First_Last_<id>.json) is indexed by the id at the end of the file name.
    A single bundle file is read once to find its patient id. The id is checked again when a chart is opened."""
    p = Path(path)
    if p.is_file():
        try:
            return {load_chart(p).patient_id: p}
        except (OSError, ValueError):
            return {}
    out: dict[str, Path] = {}
    for f in sorted(p.glob("*.json")) if p.is_dir() else []:
        if "_" in f.stem:
            out[f.stem.rsplit("_", 1)[-1]] = f
    return out


def active(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.status == "active"]


def text_chart(chart: Chart, name: str = "") -> str:
    """A plain-text chart for the terminal."""
    def line(label, what: Coded, extra=""):
        code = f"  [{what.code}]" if what.code else ""
        return f"  {label}{clean(what.display)}{code}{('  ' + extra) if extra else ''}"

    out = [f"Chart for {name or chart.patient_id or 'patient'}", ""]
    act = active(chart.problems)
    out.append(f"Problem list: {len(act)} active of {len(chart.problems)} diagnoses")
    out += [line("", p.what, f"since {p.onset}" if p.onset else "") for p in act] or ["  No active problems"]
    past = [p for p in chart.problems if p.status != "active"]
    if past:
        out += ["", f"Past problems (resolved or inactive): {len(past)}"]
        out += [line("", p.what, f"{p.onset} to {p.resolved}".strip() if p.resolved else p.onset) for p in past]
    out += ["", f"Medications: {len(chart.medications)}"]
    out += [line("", m.what, f"{m.status} {m.date}".strip()) for m in chart.medications] or ["  none"]
    if chart.allergies:
        out += ["", f"Allergies: {len(chart.allergies)}"] + [line("", a.what, a.status) for a in chart.allergies]
    out += ["", f"Vitals (most recent): {len(chart.vitals)}"]
    out += [line("", r.what, f"{r.value}  {r.date}") for r in chart.vitals] or ["  none"]
    out += ["", f"Labs (most recent): {len(chart.labs)}"]
    out += [line("", r.what, f"{r.value}  {r.date}") for r in chart.labs[:15]] or ["  none"]
    if len(chart.labs) > 15:
        out.append(f"  ... and {len(chart.labs) - 15} more")
    out += ["", f"Visits: {len(chart.visits)} (latest first)"]
    out += [f"  {v.start}  {v.cls or '-':<5} {clean(v.kind)}" for v in chart.visits[:10]] or ["  none"]
    if len(chart.visits) > 10:
        out.append(f"  ... and {len(chart.visits) - 10} earlier")
    out += ["", f"Immunizations: {len(chart.immunizations)}"]
    out += [line("", i.what, i.date) for i in chart.immunizations[:10]] or ["  none"]
    if len(chart.immunizations) > 10:
        out.append(f"  ... and {len(chart.immunizations) - 10} earlier")
    if chart.findings:
        out += ["", f"Other findings and social history: {len(chart.findings)} (can include clinical items such as prediabetes)"]
        out += [line("", f.what, f.onset) for f in chart.findings]
    if chart.skipped:
        out += ["", "Not shown: " + ", ".join(f"{n} {k}" for k, n in sorted(chart.skipped.items()))]
    return "\n".join(out)
