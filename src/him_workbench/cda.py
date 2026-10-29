"""Read an HL7 C-CDA document (a CCD, discharge summary, referral note and so on) into the workbench's
Patient and Chart records. Local files only, no network, standard library only.

FHIR is JSON resources. CDA is one XML document: a header (who, when, which organization) and a body of
sections, each section a narrative table for people plus coded `entry` elements for machines. This reader
uses the coded entries and goes to the narrative only to find a name the entry points at.

Sections are found by their LOINC code and mapped onto the same Chart the FHIR reader fills, so the chart
page, the terminal chart and the assistant's chart tool work on a CCD with no changes:

  11450-4 Problems       -> problems     48765-2 Allergies    -> allergies   8716-3 Vital signs -> vitals
  10160-0 Medications    -> medications  11369-6 Immunizations -> immunizations
  30954-2 Results        -> labs         29762-2 Social history -> social     46240-8 Encounters -> visits

Nothing is dropped silently. A section that has narrative text and no coded entries, an entry with no
usable code or value, and every section this reader does not map (procedures, plan of treatment ...) are
counted in `chart.skipped`. Statements the document makes that are not list items ("no known allergies",
"immunization refused", an entry marked negated) go in `CdaDocument.notes`. `CdaDocument.issues` lists
what the document is missing or gets wrong, checked against the C-CDA CCD requirements.

Safety: a document with a DOCTYPE or ENTITY declaration is refused, so XML entity expansion and external
entities cannot be used against this reader. Files over MAX_BYTES are refused.

Limits:
  - Medications keep the name, status and start date. Dose, route and frequency are not read.
  - Reference ranges and interpretation flags on results are not read (the FHIR chart does not read them either).
  - Every entry in the Problems section goes in `problems`, whatever its type (diagnosis, complaint, finding).
    CDA has no "(disorder)" tag to split them on the way SNOMED display names in Synthea FHIR do.
  - Codes are kept as the document gives them. Nothing is translated between code systems.
  - Only UTF-8 / ASCII-compatible encodings are accepted (UTF-16 and UTF-32 are refused).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import audit
from .chart import (EARLIER_KEPT, SCREEN_EARLIER_KEPT, Allergy, Chart, Coded, Immunization, Medication,
                    Problem, Result, Visit)
from .matching import Patient

MAX_BYTES = 50 * 1024 * 1024
NS = "urn:hl7-org:v3"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

# Templates (C-CDA R2.1). Roots only; the dated extensions are ignored on purpose, so R1.1 and R2.1 both read.
T_CCD = "2.16.840.1.113883.10.20.22.1.2"
T_US_HEADER = "2.16.840.1.113883.10.20.22.1.1"
T_PROBLEM_OBS = "2.16.840.1.113883.10.20.22.4.4"
T_PROBLEM_STATUS = "2.16.840.1.113883.10.20.22.4.6"
# Sub-observations that hang off another observation and are never list items themselves.
_SUB_OBS = {"2.16.840.1.113883.10.20.22.4.6", "2.16.840.1.113883.10.20.22.4.31", "2.16.840.1.113883.10.20.22.4.32",
            "2.16.840.1.113883.10.20.22.4.8", "2.16.840.1.113883.10.20.22.4.9", "2.16.840.1.113883.10.20.22.4.28",
            "2.16.840.1.113883.10.20.22.4.145"}

# FHIR system URIs, so a code reads the same whichever reader produced it.
SYSTEMS = {
    "2.16.840.1.113883.6.96": "http://snomed.info/sct",
    "2.16.840.1.113883.6.1": "http://loinc.org",
    "2.16.840.1.113883.6.88": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "2.16.840.1.113883.12.292": "http://hl7.org/fhir/sid/cvx",
    "2.16.840.1.113883.6.90": "http://hl7.org/fhir/sid/icd-10-cm",
    "2.16.840.1.113883.6.3": "http://hl7.org/fhir/sid/icd-10",
    "2.16.840.1.113883.6.103": "http://hl7.org/fhir/sid/icd-9-cm",
    "2.16.840.1.113883.6.69": "http://hl7.org/fhir/sid/ndc",
    "2.16.840.1.113883.6.12": "http://www.ama-assn.org/go/cpt",
    "2.16.840.1.113883.4.9": "http://fdasis.nlm.nih.gov",
}
SYSTEM_NAMES = {v: n for v, n in zip(SYSTEMS.values(), ["SNOMED CT", "LOINC", "RxNorm", "CVX", "ICD-10-CM", "ICD-10",
                                                          "ICD-9-CM", "NDC", "CPT", "UNII"])}

# section LOINC -> (short name, what the chart does with it)
SECTIONS = {
    "11450-4": "Problems", "48765-2": "Allergies", "10160-0": "Medications", "8716-3": "Vital signs",
    "30954-2": "Results", "29762-2": "Social history", "11369-6": "Immunizations", "46240-8": "Encounters",
}
CCD_REQUIRED = ["48765-2", "10160-0", "11450-4", "30954-2", "29762-2", "8716-3"]   # CCD 1..1 sections, see README
_EXPECTED_SYSTEM = {"11450-4": {"http://snomed.info/sct", "http://hl7.org/fhir/sid/icd-10-cm"},
                    "10160-0": {"http://www.nlm.nih.gov/research/umls/rxnorm"},
                    "11369-6": {"http://hl7.org/fhir/sid/cvx"}}

# Document-level type codes seen most often; anything else is shown as the document's own displayName.
DOC_TYPES = {"34133-9": "Summarization of episode note (CCD)", "18842-5": "Discharge summary",
             "11488-4": "Consultation note", "57133-1": "Referral note", "34117-2": "History and physical",
             "11506-3": "Progress note", "28570-0": "Procedure note", "34109-9": "Note"}

_NULL_PROBLEM_CODES = {"396782006"}          # SNOMED "No known problems"
_NULL_ALLERGY_CODES = {"716186003", "409137002", "428607008", "428281000124107", "428191000124101"}   # no known allergy ...
_STATUS_WORDS = {"55561003": "active", "73425007": "inactive", "413322009": "resolved"}  # SNOMED problem status values


class CdaError(ValueError):
    """The file is not a readable C-CDA document."""


@dataclass
class CdaDocument:
    patient: Patient | None = None
    chart: Chart = field(default_factory=Chart)
    meta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)       # things the document states that are not list items
    issues: list[tuple[str, str]] = field(default_factory=list)   # (severity, message); severity error|warning|info
    sections: list[tuple[str, str, int, str]] = field(default_factory=list)   # (loinc, title, entries, how it was read)
    code_systems: Counter = field(default_factory=Counter)        # code system -> coded values read


# ---- small XML helpers -------------------------------------------------------------------------------------

def _q(*names: str) -> str:
    return "/".join(f"{{{NS}}}{n}" if n not in ("*", ".", "..") else n for n in names)


def _kids(el, name):
    return [] if el is None else el.findall(_q(name))


def _kid(el, name):
    return None if el is None else el.find(_q(name))


def _path(el, *names):
    return None if el is None else el.find(_q(*names))


def _paths(el, *names):
    return [] if el is None else el.findall(_q(*names))


_EMPTY = ET.Element("x")


def _n(el):
    """An Element is falsy when it has no children, so `el or default` is wrong; this is the safe form."""
    return el if el is not None else _EMPTY


def _roots(el) -> set[str]:
    return {t.get("root", "") for t in _kids(el, "templateId")}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _ts(value: str | None) -> str:
    """HL7 timestamp -> 'YYYY-MM-DD'. Partial dates stay partial ('2024', '2024-03'). Time and zone are dropped."""
    v = (value or "").strip()
    m = re.match(r"^(\d{4})(\d{2})?(\d{2})?", v)
    if not m:
        return ""
    return "-".join(p for p in m.groups() if p)


def _time(el, which: str = "low") -> str:
    """Start ('low') or end ('high') of an effectiveTime-like element. A single `value` counts as the start."""
    if el is None:
        return ""
    child = _kid(el, which)
    if child is not None and child.get("value"):
        return _ts(child.get("value"))
    if which == "low" and el.get("value"):
        return _ts(el.get("value"))
    return ""


def _first_time(parent, name: str = "effectiveTime") -> tuple[str, str]:
    """(start, end) from the first effectiveTime that has a value, low or high. Medications carry a second one
    for the dose frequency; the first is the period."""
    for t in _kids(parent, name):
        s, e = _time(t, "low"), _time(t, "high")
        if s or e:
            return s, e
    return "", ""


class _Narrative:
    """id -> text for the elements of one section's narrative block, so `<reference value="#m1"/>` can be read."""

    def __init__(self, section):
        self.by_id: dict[str, str] = {}
        text = _kid(section, "text")
        if text is not None:
            for el in text.iter():
                i = el.get("ID")
                if i:
                    self.by_id[i] = _norm("".join(el.itertext()))

    def resolve(self, code_el) -> str:
        ref = _path(code_el, "originalText", "reference")
        if ref is not None and (ref.get("value") or "").startswith("#"):
            return self.by_id.get(ref.get("value")[1:], "")
        orig = _kid(code_el, "originalText")
        return _norm("".join(orig.itertext())) if orig is not None else ""


def _coded(el, narr: _Narrative | None = None, systems: Counter | None = None) -> Coded:
    """A CD/CE/CV element -> Coded. Display name order: displayName, then originalText (or the narrative text it points to),
    then the first usable translation. A nullFlavor code with only original text keeps the text and no code."""
    if el is None:
        return Coded("")
    cands = [el] + _kids(el, "translation")
    text = (narr.resolve(el) if narr else "") or _norm("".join(_n(_kid(el, "originalText")).itertext()))
    for c in cands:
        code, oid = c.get("code") or "", c.get("codeSystem") or ""
        if code and not c.get("nullFlavor"):
            system = SYSTEMS.get(oid, f"urn:oid:{oid}" if oid else "")
            if systems is not None:
                systems[system or "(no code system)"] += 1
            display = c.get("displayName") or text or (el.get("displayName") or "")
            return Coded(display, code, system)
    return Coded(el.get("displayName") or text)


def _value_text(obs, narr: _Narrative, systems: Counter) -> str:
    """The value of an observation as a short string, '' when there is none this reader understands."""
    v = _kid(obs, "value")
    if v is None or v.get("nullFlavor") == "NI":
        return ""
    t = (v.get(_XSI_TYPE) or "").split(":")[-1]
    if t in ("PQ", "") and v.get("value") is not None and v.get("value") != "":
        num = v.get("value")
        unit = (v.get("unit") or "").strip()
        unit = {"1": ""}.get(unit, unit)
        return f"{num} {unit}".strip()
    if t in ("CD", "CE", "CV", "CO") or (v.get("code") and not v.get("value")):
        c = _coded(v, narr, systems)
        return c.display or c.code
    if t in ("ST", "ED"):
        return _norm("".join(v.itertext()))
    if t == "INT" and v.get("value") is not None:
        return v.get("value")
    if t == "BL" and v.get("value") is not None:
        return "yes" if v.get("value").lower() == "true" else "no"
    if t in ("TS", "IVL_TS") and (v.get("value") or _time(v)):
        return _ts(v.get("value")) or _time(v)
    return ""


def _group_results(found: dict, keep: int) -> list[Result]:
    out = []
    for results in found.values():
        results.sort(key=lambda r: r.date, reverse=True)
        r = results[0]
        r.earlier = [(o.date, o.value) for o in results[1:keep + 1]]
        out.append(r)
    out.sort(key=lambda r: r.what.display)
    return out


# ---- header -----------------------------------------------------------------------------------------------

def _person_name(name_el) -> tuple[str, str]:
    given = [g for g in _kids(name_el, "given") if _norm("".join(g.itertext())) and g.get("qualifier") != "CL"]
    family = _kid(name_el, "family")
    first = _norm("".join(given[0].itertext())) if given else ""
    last = _norm("".join(family.itertext())) if family is not None else ""
    return first, last


def _org_name(org) -> str:
    n = _kid(org, "name")
    return _norm("".join(n.itertext())) if n is not None else ""


def _read_header(root, doc: CdaDocument) -> None:
    meta = doc.meta
    meta["title"] = _norm("".join(_n(_kid(root, "title")).itertext()))
    code = _kid(root, "code")
    loinc = (code.get("code") if code is not None else "") or ""
    meta["doc_type_code"] = loinc
    meta["doc_type"] = DOC_TYPES.get(loinc) or (code.get("displayName") if code is not None else "") or "(not stated)"
    meta["created"] = _ts(_n(_kid(root, "effectiveTime")).get("value"))
    meta["template_ids"] = sorted(_roots(root))
    meta["is_ccd"] = T_CCD in _roots(root)
    meta["document_id"] = ""
    did = _kid(root, "id")
    if did is not None:
        meta["document_id"] = f"{did.get('root', '')}{'^' + did.get('extension') if did.get('extension') else ''}"
    meta["version"] = _n(_kid(root, "versionNumber")).get("value") or ""
    meta["author_org"] = _org_name(_path(root, "author", "assignedAuthor", "representedOrganization"))
    meta["custodian"] = _org_name(_path(root, "custodian", "assignedCustodian", "representedCustodianOrganization"))

    role = _path(root, "recordTarget", "patientRole")
    if role is None:
        doc.issues.append(("error", "No recordTarget/patientRole: the document does not say whose record it is."))
        return
    ids = _kids(role, "id")
    pid = next((i for i in ids if i.get("extension")), ids[0] if ids else None)
    patient_id = (pid.get("extension") or pid.get("root") or "") if pid is not None else ""
    pat = _kid(role, "patient")
    names = _kids(pat, "name")
    name = next((n for n in names if n.get("use") == "L"), names[0] if names else None)
    first, last = _person_name(name) if name is not None else ("", "")
    dob = _ts(_n(_path(pat, "birthTime")).get("value"))
    sexcode = (_n(_path(pat, "administrativeGenderCode")).get("code") or "").upper()
    zipc = next((_norm("".join(z.itertext())) for a in _kids(role, "addr") for z in _kids(a, "postalCode")), "")
    phone = next((t.get("value", "").removeprefix("tel:") for t in _kids(role, "telecom")
                  if (t.get("value") or "").startswith("tel:")), "")
    doc.chart.patient_id = patient_id
    if not patient_id:
        doc.issues.append(("warning", "The patient has no id in recordTarget, so records from this file cannot be matched by id."))
    if not (first and last):
        doc.issues.append(("warning", "The patient's name is missing or incomplete in the header."))
    if not dob:
        doc.issues.append(("warning", "The patient's birth date is missing from the header."))
    elif len(dob) < 10:
        doc.issues.append(("info", f"Birth date is partial ({dob}); the duplicate matcher treats a partial date as less certain."))
    if patient_id and first and last:
        doc.patient = Patient(id=patient_id, first=first, last=last, dob=dob,
                              sex={"M": "M", "F": "F"}.get(sexcode, ""), zip=zipc, phone=phone)


# ---- sections ---------------------------------------------------------------------------------------------

def _problem_status(obs, act) -> str:
    for rel in _kids(obs, "entryRelationship"):
        sub = _kid(rel, "observation")
        if sub is not None and T_PROBLEM_STATUS in _roots(sub):
            code = _n(_kid(sub, "value")).get("code") or ""
            if code in _STATUS_WORDS:
                return _STATUS_WORDS[code]
    if _time(_kid(obs, "effectiveTime"), "high"):
        return "resolved"
    status = _n((_kid(act, "statusCode") if act is not None else None)).get("code") or ""
    return {"active": "active", "completed": "resolved", "aborted": "inactive", "suspended": "inactive"}.get(status, "active" if not status else status)


def _entry_observations(entry, template: str | None = None):
    """The list-item observations inside one entry: the ones with `template`, else every observation that is not a
    known sub-observation (status, age at onset, severity, reaction ...)."""
    all_obs = list(entry.iter(_q("observation")))
    if template:
        hit = [o for o in all_obs if template in _roots(o)]
        if hit:
            return hit
    return [o for o in all_obs if not (_roots(o) & _SUB_OBS)]


def _read_section(sec, loinc: str, doc: CdaDocument, found: dict) -> tuple[int, str]:
    """Read one mapped section. Returns (entries seen, how it was read)."""
    chart, notes, systems = doc.chart, doc.notes, doc.code_systems
    narr = _Narrative(sec)
    entries = _kids(sec, "entry")
    label = SECTIONS[loinc]
    unread = 0
    expected = _EXPECTED_SYSTEM.get(loinc)
    off_system = Counter()

    def count(c: Coded):
        if expected and c.code and c.system not in expected:
            off_system[SYSTEM_NAMES.get(c.system, c.system or "unknown")] += 1

    def skip(what: str):
        nonlocal unread
        unread += 1
        chart.skipped[f"{label} entry ({what})"] += 1

    for entry in entries:
        if loinc == "11450-4":
            act = _kid(entry, "act")
            obs_list = _entry_observations(entry, T_PROBLEM_OBS)
            if not obs_list:
                skip("no observation")
            for obs in obs_list:
                v = _kid(obs, "value")
                c = _coded(v, narr, systems) if v is not None else Coded("")
                if not c.display and not c.code:
                    c = _coded(_kid(obs, "code"), narr, systems)
                if obs.get("negationInd") == "true" or c.code in _NULL_PROBLEM_CODES:
                    notes.append(f"Problems: the document states {'no known problems' if c.code in _NULL_PROBLEM_CODES else 'a negated problem (' + (c.display or c.code) + ')'}.")
                    continue
                if not c.display and not c.code:
                    skip("no code or name")
                    continue
                count(c)
                et = _kid(obs, "effectiveTime")
                chart.problems.append(Problem(c, _problem_status(obs, act), _time(et, "low"), _time(et, "high")))
        elif loinc == "10160-0":
            subs = [s for s in entry.iter(_q("substanceAdministration"))]
            if not subs:
                skip("no substanceAdministration")
            for sa in subs:
                mat = _path(sa, "consumable", "manufacturedProduct", "manufacturedMaterial")
                c = _coded(_kid(mat, "code"), narr, systems)
                if not c.display:
                    nm = _kid(mat, "name")
                    c = Coded(_norm("".join(nm.itertext())) if nm is not None else "", c.code, c.system)
                if sa.get("negationInd") == "true":
                    notes.append(f"Medications: '{c.display or 'a medication'}' is marked as not given/not taken.")
                    continue
                if not c.display and not c.code:
                    skip("no code or name")
                    continue
                count(c)
                status = _n(_kid(sa, "statusCode")).get("code") or ""
                chart.medications.append(Medication(c, status, _first_time(sa)[0]))
        elif loinc == "48765-2":
            obs_list = _entry_observations(entry, "2.16.840.1.113883.10.20.22.4.7")
            if not obs_list:
                skip("no observation")
            for obs in obs_list:
                role = _path(obs, "participant", "participantRole", "playingEntity")
                c = _coded(_kid(role, "code"), narr, systems) if role is not None else Coded("")
                if not c.display and role is not None:
                    nm = _kid(role, "name")
                    c = Coded(_norm("".join(nm.itertext())) if nm is not None else "", c.code, c.system)
                v = _n(_kid(obs, "value")).get("code") or ""
                role_code = _kid(role, "code") if role is not None else None
                if v in _NULL_ALLERGY_CODES or (role_code is not None and role_code.get("nullFlavor") == "NA"):
                    notes.append("Allergies: the document states no known allergies.")
                    continue
                if obs.get("negationInd") == "true":
                    notes.append(f"Allergies: '{c.display or c.code or 'an allergy'}' is marked negated (the patient does not have it), so it is not in the list.")
                    continue
                if not c.display and not c.code:
                    skip("no substance")
                    continue
                act = _kid(entry, "act")
                st = _n((_kid(act, "statusCode") if act is not None else None)).get("code") or "active"
                chart.allergies.append(Allergy(c, "active" if st == "active" else st))
        elif loinc == "11369-6":
            subs = [s for s in entry.iter(_q("substanceAdministration"))]
            if not subs:
                skip("no substanceAdministration")
            for sa in subs:
                mat = _path(sa, "consumable", "manufacturedProduct", "manufacturedMaterial")
                c = _coded(_kid(mat, "code"), narr, systems)
                if sa.get("negationInd") == "true":
                    notes.append(f"Immunizations: '{c.display or c.code or 'a vaccine'}' is recorded as refused or not given, so it is not in the list.")
                    continue
                if not c.display and not c.code:
                    skip("no code or name")
                    continue
                count(c)
                chart.immunizations.append(Immunization(c, _first_time(sa)[0]))
        elif loinc == "46240-8":
            encs = [e for e in entry.iter(_q("encounter"))]
            if not encs:
                skip("no encounter")
            for e in encs:
                c = _coded(_kid(e, "code"), narr, systems)
                s, end = _first_time(e)
                chart.visits.append(Visit(c.display or c.code, s, end, ""))
        else:   # 8716-3 vitals, 30954-2 results, 29762-2 social history: observations
            group = {"8716-3": "vitals", "30954-2": "labs", "29762-2": "social"}[loinc]
            obs_list = [o for o in entry.iter(_q("observation")) if not (_roots(o) & _SUB_OBS)]
            if not obs_list:
                skip("no observation")
            for obs in obs_list:
                c = _coded(_kid(obs, "code"), narr, systems)
                value = _value_text(obs, narr, systems)
                if obs.get("negationInd") == "true":
                    notes.append(f"{label}: '{c.display or c.code}' is marked as negated, so it is not in the list.")
                    continue
                if not (c.display or c.code) or not value:
                    skip("no code or value" if not (c.display or c.code) else f"{c.display or c.code}: no value")
                    continue
                when = _first_time(obs)[0]
                found.setdefault(group, {}).setdefault(c.code or c.display, []).append(Result(c, value, when))
    for system, n in off_system.items():
        doc.issues.append(("info", f"{label}: {n} entries are coded in {system}, not {', '.join(sorted(SYSTEM_NAMES.get(s, s) for s in expected))}."))
    return len(entries), ("coded entries" if entries else "")


def _walk_sections(root):
    body = _path(root, "component", "structuredBody")
    if body is None:
        return None
    out = []

    def visit(component_parent):
        for comp in _kids(component_parent, "component"):
            sec = _kid(comp, "section")
            if sec is not None:
                out.append(sec)
                visit(sec)
    visit(body)
    return out


# ---- public API -------------------------------------------------------------------------------------------

def parse_ccd(data: bytes | str) -> CdaDocument:
    """Parse a C-CDA document. Raises CdaError when it is not one (not XML, wrong root, DOCTYPE/ENTITY, too big)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise CdaError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not read.")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        raise CdaError("UTF-16/UTF-32 documents are not supported. Save the file as UTF-8.")
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.IGNORECASE):
        raise CdaError("The document has a DOCTYPE or ENTITY declaration. C-CDA does not use them, and they can be used "
                       "to attack XML readers, so the file was refused.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CdaError(f"Not well-formed XML: {exc}") from exc
    if root.tag != f"{{{NS}}}ClinicalDocument":
        raise CdaError("Not a CDA document: the root element is not ClinicalDocument in the urn:hl7-org:v3 namespace.")

    doc = CdaDocument()
    _read_header(root, doc)
    if doc.meta.get("is_ccd") is False:
        doc.issues.append(("info", f"Not a CCD: document type is {doc.meta['doc_type']} ({doc.meta['doc_type_code'] or 'no code'}). "
                                   "Read with the same section mapping."))
    if T_US_HEADER not in _roots(root):
        doc.issues.append(("warning", "No US Realm Header templateId (2.16.840.1.113883.10.20.22.1.1)."))

    sections = _walk_sections(root)
    if sections is None:
        doc.issues.append(("error", "No structuredBody: this is a nonXMLBody document (a PDF or text wrapped in CDA), "
                                    "so there are no coded entries to read."))
        return doc

    found: dict[str, dict] = {}
    seen: set[str] = set()
    for sec in sections:
        code = _kid(sec, "code")
        loinc = (code.get("code") if code is not None else "") or ""
        title = _norm("".join(_n(_kid(sec, "title")).itertext())) or (
            code.get("displayName") if code is not None else "") or "(untitled section)"
        entries = _kids(sec, "entry")
        has_text = bool(_norm("".join(_n(_kid(sec, "text")).itertext())))
        if loinc in SECTIONS:
            seen.add(loinc)
            n, how = _read_section(sec, loinc, doc, found)
            if not entries:
                if sec.get("nullFlavor"):
                    # The author says there is nothing to report (NI = no information, NA, ASKU ...). Not a reader gap.
                    how = f"no entries, nullFlavor {sec.get('nullFlavor')} (document gives no information)"
                    said = _norm("".join(_n(_kid(sec, "text")).itertext()))[:60]
                    doc.notes.append(f"{SECTIONS[loinc]}: section marked nullFlavor {sec.get('nullFlavor')}"
                                     + (f' with the text "{said}"' if said else "") + ".")
                elif has_text:
                    how = "no coded entries (narrative only: not read)"
                    doc.chart.skipped[f"{SECTIONS[loinc]} section (narrative only)"] += 1
                else:
                    how = "no entries"
            doc.sections.append((loinc, title, n, how))
        else:
            n = len(entries)
            if n or has_text:
                doc.chart.skipped[f"{title} section"] += max(n, 1)
            doc.sections.append((loinc, title, n, "not mapped (counted in 'not shown')"))

    ch = doc.chart
    ch.vitals = _group_results(found.get("vitals", {}), EARLIER_KEPT)
    ch.labs = _group_results(found.get("labs", {}), EARLIER_KEPT)
    ch.social = _group_results(found.get("social", {}), SCREEN_EARLIER_KEPT)
    ch.visits.sort(key=lambda v: v.start, reverse=True)
    ch.problems.sort(key=lambda p: p.onset, reverse=True)
    ch.medications.sort(key=lambda m: m.date, reverse=True)
    ch.immunizations.sort(key=lambda i: i.date, reverse=True)

    if doc.meta.get("is_ccd"):
        for loinc in CCD_REQUIRED:
            if loinc not in seen:
                doc.issues.append(("warning", f"A CCD must have a {SECTIONS[loinc]} section (LOINC {loinc}); this one does not."))
    for loinc, title, n, how in doc.sections:
        if loinc in SECTIONS and how == "no entries":
            doc.issues.append(("info", f"{SECTIONS[loinc]} section is present but empty."))
    audit.log_call("cda_parse", {"sections": len(doc.sections)},
                   f"{len(ch.problems)} problems, {len(ch.medications)} medications, {len(doc.issues)} issues")
    return doc


def load_ccd(path) -> CdaDocument:
    """Read one C-CDA file. Raises CdaError for a bad document and OSError when the file cannot be opened."""
    p = Path(path)
    if p.stat().st_size > MAX_BYTES:
        raise CdaError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not read.")
    return parse_ccd(p.read_bytes())


def ccd_files_from(path, limit: int = 200) -> list[Path]:
    """A single C-CDA file, or the .xml files directly inside a folder (sorted, at most `limit`). Files are not opened."""
    p = Path(path)
    if p.is_file():
        return [p]
    return sorted(p.glob("*.xml"))[:limit] if p.is_dir() else []


def text_report(doc: CdaDocument, name: str = "") -> str:
    """Document summary for the terminal: header, how each section was read, notes and problems, then the chart."""
    from .chart import text_chart
    m = doc.meta
    who = name or (f"{doc.patient.first} {doc.patient.last}" if doc.patient else "")
    out = [f"{m.get('title') or 'Untitled document'}", f"  Type: {m.get('doc_type')} ({m.get('doc_type_code') or 'no code'})"
           + ("  [CCD]" if m.get("is_ccd") else ""),
           f"  Created: {m.get('created') or 'not stated'}   Version: {m.get('version') or 'not stated'}",
           f"  Author organization: {m.get('author_org') or 'not stated'}   Custodian: {m.get('custodian') or 'not stated'}",
           f"  Document id: {m.get('document_id') or 'not stated'}", ""]
    if doc.patient:
        p = doc.patient
        out.append(f"Patient: {p.first} {p.last}   id {p.id}   born {p.dob or '(blank)'}   sex {p.sex or '(blank)'}   zip {p.zip or '(blank)'}")
    else:
        out.append("Patient: could not be built from the header (see issues).")
    out += ["", "Sections:"]
    out += [f"  {l or '-':<9} {t:<34} {n:>3} entr{'y' if n == 1 else 'ies'}  {how}" for l, t, n, how in doc.sections] or ["  none"]
    if doc.code_systems:
        out += ["", "Code systems used: " + ", ".join(f"{SYSTEM_NAMES.get(s, s)} {n}" for s, n in doc.code_systems.most_common())]
    if doc.notes:
        out += ["", "The document also states:"] + [f"  - {n}" for n in dict.fromkeys(doc.notes)]
    if doc.issues:
        out += ["", "Checks:"] + [f"  [{sev}] {msg}" for sev, msg in doc.issues]
    else:
        out += ["", "Checks: no problems found."]
    out += ["", text_chart(doc.chart, who)]
    return "\n".join(out)
