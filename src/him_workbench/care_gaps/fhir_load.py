"""Load a Synthea FHIR patient bundle into a small, flat set of facts.

Only the fields the diabetes measures need are kept, so the measure
logic in measures.py never has to walk raw FHIR.
"""
import base64
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# SNOMED codes for type 1 and type 2 diabetes. Prediabetes (15777000) is
# deliberately left out: CMS122 counts only type 1 and type 2 diabetes.
DIABETES_CODES = {"44054006", "46635009"}

A1C_LOINC = "4548-4"
EGFR_LOINC = "33914-3"
UACR_LOINC = "14959-1"
RETINAL_EXAM_SNOMED = "722161008"  # Diabetic retinal eye exam (procedure)

HOSPICE_PALLIATIVE_WORDS = ("hospice", "palliative")


def _day(value):
    """Return the date part of a FHIR dateTime string, or None."""
    return date.fromisoformat(value[:10]) if value else None


def clean_name(name):
    """Drop the numbers Synthea appends to each name part ("Annamae625 Beatty507" -> "Annamae Beatty")."""
    return " ".join(part.rstrip("0123456789") for part in name.split() if part.rstrip("0123456789"))


def _first_coding(concept):
    codings = (concept or {}).get("coding") or [{}]
    return codings[0]


def _note_text(doc):
    """Decode the plain-text attachment of a DocumentReference, or return ''."""
    for content in doc.get("content") or []:
        attachment = content.get("attachment") or {}
        data = attachment.get("data")
        if data and "text" in (attachment.get("contentType") or "text/plain"):
            try:
                return base64.b64decode(data).decode("utf-8", errors="replace")
            except ValueError:
                return ""
    return ""


@dataclass
class Observation:
    code: str
    day: date
    value: float | None


@dataclass
class Condition:
    code: str
    display: str
    onset: date | None
    abated: date | None


@dataclass
class PatientFacts:
    patient_id: str
    name: str
    birth_date: date
    deceased: date | None
    conditions: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    procedures: list = field(default_factory=list)  # (code, display, day)
    encounters: list = field(default_factory=list)  # (class_code, display, day)
    notes: list = field(default_factory=list)       # (day, text)
    orders: list = field(default_factory=list)      # (code, day, status)

    def age_on(self, day):
        years = day.year - self.birth_date.year
        if (day.month, day.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


def facts_from_bundle(bundle):
    resources = [entry["resource"] for entry in bundle.get("entry", [])]
    patient = next(r for r in resources if r["resourceType"] == "Patient")
    name = patient.get("name", [{}])[0]
    facts = PatientFacts(
        patient_id=patient["id"],
        name=clean_name(" ".join(name.get("given", []) + [name.get("family", "")])),
        birth_date=_day(patient["birthDate"]),
        deceased=_day(patient.get("deceasedDateTime")),
    )
    for r in resources:
        kind = r["resourceType"]
        if kind == "Condition":
            coding = _first_coding(r.get("code"))
            facts.conditions.append(Condition(
                code=coding.get("code", ""),
                display=coding.get("display", ""),
                onset=_day(r.get("onsetDateTime")),
                abated=_day(r.get("abatementDateTime")),
            ))
        elif kind == "Observation":
            coding = _first_coding(r.get("code"))
            quantity = r.get("valueQuantity") or {}
            facts.observations.append(Observation(
                code=coding.get("code", ""),
                day=_day(r.get("effectiveDateTime")),
                value=quantity.get("value"),
            ))
        elif kind == "Procedure":
            coding = _first_coding(r.get("code"))
            when = r.get("performedDateTime") or (r.get("performedPeriod") or {}).get("start")
            facts.procedures.append((coding.get("code", ""), coding.get("display", ""), _day(when)))
        elif kind == "Encounter":
            type_coding = _first_coding((r.get("type") or [{}])[0])
            start = (r.get("period") or {}).get("start")
            facts.encounters.append(((r.get("class") or {}).get("code", ""),
                                     type_coding.get("display", ""), _day(start)))
        elif kind == "DocumentReference":
            text = _note_text(r)
            if text:
                facts.notes.append((_day(r.get("date")), text))
        elif kind == "ServiceRequest":
            coding = _first_coding(r.get("code"))
            facts.orders.append((coding.get("code", ""), _day(r.get("authoredOn")), r.get("status", "")))
    return facts


def load_patient(path):
    with open(path, encoding="utf-8") as handle:
        return facts_from_bundle(json.load(handle))


def load_if_diabetic(path):
    """Return facts only when the raw file mentions a diabetes code; skips parsing everyone else.

    A cheap text check before json parsing. It can let through a file that only mentions the
    code somewhere odd, but has_diabetes() still decides; it never drops a real diabetic.
    """
    text = Path(path).read_text(encoding="utf-8")
    if not any(f'"{code}"' in text for code in DIABETES_CODES):
        return None
    return facts_from_bundle(json.loads(text))


def iter_patient_files(fhir_dir):
    """Yield patient bundle paths, skipping Synthea's hospital and practitioner files."""
    for path in sorted(Path(fhir_dir).glob("*.json")):
        if path.name.startswith(("hospitalInformation", "practitionerInformation")):
            continue
        yield path
