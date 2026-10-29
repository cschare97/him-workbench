from pathlib import Path

import pytest

from him_workbench.cda import CdaError, load_ccd, parse_ccd, text_report

SAMPLE = Path(__file__).parent / "fixtures" / "sample_ccd.xml"


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    from him_workbench import audit
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture(scope="module")
def xml() -> str:
    return SAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc():
    return load_ccd(SAMPLE)


def drop(xml: str, loinc: str) -> str:
    """Remove the whole <component><section> block whose code is `loinc`."""
    i = xml.index(f'code="{loinc}"')
    start = xml.rindex("<component><section>", 0, i)
    end = xml.index("</section></component>", i) + len("</section></component>")
    return xml[:start] + xml[end:]


# ---- header ----

def test_patient_from_header(doc):
    p = doc.patient
    assert (p.id, p.first, p.last, p.dob, p.sex, p.zip) == ("TEST-0001", "Patricia", "Sample", "1962-07-14", "F", "90210")
    assert p.phone == "+1-555-010-0199"          # 'tel:' removed; call-me given name 'Pat' not used
    assert doc.chart.patient_id == "TEST-0001"


def test_document_metadata(doc):
    m = doc.meta
    assert m["is_ccd"] and m["doc_type_code"] == "34133-9" and m["created"] == "2026-03-15" and m["version"] == "2"
    assert m["author_org"] == "Example Family Clinic" and m["custodian"] == "Example Health Records Dept"
    assert m["document_id"] == "2.16.840.1.113883.19.5.99999.1^DOC-0001"


def test_clean_ccd_has_no_issues(doc):
    assert doc.issues == []


def test_unknown_sex_is_blank_not_a_conflict(xml):
    d = parse_ccd(xml.replace('administrativeGenderCode code="F"', 'administrativeGenderCode code="UN"'))
    assert d.patient.sex == ""


# ---- chart mapping ----

def test_problems_status_and_dates(doc):
    got = {p.what.code: (p.status, p.onset, p.resolved) for p in doc.chart.problems}
    assert got == {"44054006": ("active", "2018-03-01", ""), "38341003": ("active", "2015-06-10", ""),
                   "10509002": ("resolved", "2025-01-05", "2025-01-20")}
    assert doc.chart.problems[0].what.system == "http://snomed.info/sct"


def test_medication_name_from_narrative_reference(doc):
    meds = {m.what.code: m for m in doc.chart.medications}
    assert meds["314076"].what.display == "Lisinopril 10 mg tablet"      # name only exists in the narrative table
    assert meds["861007"].date == "2018-04-01" and meds["861007"].status == "active"   # period, not the frequency element
    assert meds["861007"].what.system == "http://www.nlm.nih.gov/research/umls/rxnorm"


def test_allergy(doc):
    assert [(a.what.display, a.what.code, a.status) for a in doc.chart.allergies] == [("Penicillin G", "7980", "active")]


def test_vitals_labs_social(doc):
    c = doc.chart
    assert {r.what.display: r.value for r in c.vitals} == {
        "Systolic blood pressure": "132 mm[Hg]", "Diastolic blood pressure": "84 mm[Hg]", "Body weight": "82.5 kg"}
    labs = {r.what.display: r for r in c.labs}
    assert labs["Hemoglobin A1c"].value == "6.9 %" and labs["Hemoglobin A1c"].date == "2026-03-12"
    assert labs["Hemoglobin A1c"].earlier == [("2025-09-10", "7.4 %")]       # most recent first, older kept
    assert labs["Urine color"].value == "Yellow"
    assert [(r.what.display, r.value) for r in c.social] == [("Tobacco smoking status", "Former smoker")]


def test_visit_and_immunization(doc):
    assert [(v.start, v.end, v.kind) for v in doc.chart.visits] == [
        ("2026-03-15", "2026-03-15", "Office or other outpatient visit, established patient")]
    assert [(i.what.code, i.date) for i in doc.chart.immunizations] == [("140", "2025-10-04")]


def test_refused_vaccine_is_reported_not_listed(doc):
    assert not any(i.what.code == "208" for i in doc.chart.immunizations)
    assert any("refused" in n and "COVID-19" in n for n in doc.notes)


def test_unmapped_section_is_counted(doc):
    assert doc.chart.skipped["Procedures section"] == 1


def test_code_systems_counted(doc):
    assert doc.code_systems["http://snomed.info/sct"] == 4 and doc.code_systems["http://hl7.org/fhir/sid/cvx"] == 2


def test_chart_works_with_existing_chart_code(doc):
    from him_workbench.chart import chart_to_dict, text_chart
    d = chart_to_dict(doc.chart, "Patricia Sample")
    assert d["summary"].startswith("2 active and 1 past problems, 2 medications, 1 allergies")
    assert "Hemoglobin A1c" in text_chart(doc.chart)


# ---- what the document leaves out or gets wrong ----

def test_missing_required_section_is_flagged(xml):
    d = parse_ccd(drop(xml, "8716-3"))
    assert ("warning", "A CCD must have a Vital signs section (LOINC 8716-3); this one does not.") in d.issues
    assert d.chart.vitals == []


def test_not_a_ccd_is_not_held_to_ccd_sections(xml):
    x = xml.replace('<templateId root="2.16.840.1.113883.10.20.22.1.2" extension="2015-08-01"/>', "")
    x = drop(x.replace('code="34133-9"', 'code="18842-5"'), "8716-3")
    d = parse_ccd(x)
    assert d.meta["doc_type"] == "Discharge summary" and not d.meta["is_ccd"]
    assert not any("must have" in m for _, m in d.issues)
    assert any("Not a CCD" in m for _, m in d.issues)


def test_narrative_only_section_is_counted_not_hidden(xml):
    i = xml.index('code="29762-2"')
    s, e = xml.index("<entry", i), xml.index("</entry>", i) + len("</entry>")
    d = parse_ccd(xml[:s] + xml[e:])
    assert d.chart.social == [] and d.chart.skipped["Social history section (narrative only)"] == 1
    assert any(l == "29762-2" and "narrative only" in how for l, _, _, how in d.sections)


def test_no_known_allergies_is_a_note_not_a_list_item(xml):
    x = xml.replace('code="419511003"', 'code="716186003"')
    d = parse_ccd(x)
    assert d.chart.allergies == [] and any("no known allergies" in n for n in d.notes)


def test_medication_not_in_rxnorm_is_flagged(xml):
    x = xml.replace('code="314076" codeSystem="2.16.840.1.113883.6.88"', 'code="12345-6789-01" codeSystem="2.16.840.1.113883.6.69"')
    d = parse_ccd(x)
    assert any("coded in NDC" in m for _, m in d.issues)


def test_result_without_value_is_counted(xml):
    d = parse_ccd(xml.replace('<value xsi:type="ST">Yellow</value>', ""))
    assert d.chart.skipped["Results entry (Urine color: no value)"] == 1
    assert "Urine color" not in [r.what.display for r in d.chart.labs]


def test_missing_header_pieces(xml):
    d = parse_ccd(xml.replace('<birthTime value="19620714"/>', "").replace(' extension="TEST-0001"', ""))
    msgs = [m for _, m in d.issues]
    assert any("birth date is missing" in m for m in msgs)


def test_partial_birth_date_is_kept_and_noted(xml):
    d = parse_ccd(xml.replace("19620714", "1962"))
    assert d.patient.dob == "1962" and any("partial" in m for _, m in d.issues)


def test_nonxml_body_is_an_error_issue(xml):
    head = xml[:xml.index("<component><structuredBody>")]
    d = parse_ccd(head + '<component><nonXMLBody><text mediaType="application/pdf">AAAA</text></nonXMLBody></component></ClinicalDocument>')
    assert d.patient and any(sev == "error" and "nonXMLBody" in m for sev, m in d.issues)


# ---- refusing bad input ----

def test_doctype_and_entities_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
            '<ClinicalDocument xmlns="urn:hl7-org:v3">&b;</ClinicalDocument>')
    with pytest.raises(CdaError, match="DOCTYPE"):
        parse_ccd(bomb)
    xxe = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           '<ClinicalDocument xmlns="urn:hl7-org:v3">&x;</ClinicalDocument>')
    with pytest.raises(CdaError, match="DOCTYPE"):
        parse_ccd(xxe)


def test_lowercase_doctype_still_refused():
    with pytest.raises(CdaError, match="DOCTYPE"):
        parse_ccd('<?xml version="1.0"?><!doctype x [<!entity a "b">]><ClinicalDocument xmlns="urn:hl7-org:v3"/>')


@pytest.mark.parametrize("bad, why", [
    ("not xml at all", "well-formed"),
    ("<ClinicalDocument>", "well-formed"),
    ('<Bundle xmlns="http://hl7.org/fhir"/>', "Not a CDA"),
    ("<ClinicalDocument/>", "Not a CDA"),            # right name, no HL7 namespace
])
def test_not_a_cda_document(bad, why):
    with pytest.raises(CdaError, match=why):
        parse_ccd(bad)


def test_utf16_refused():
    with pytest.raises(CdaError, match="UTF-16"):
        parse_ccd('<ClinicalDocument xmlns="urn:hl7-org:v3"/>'.encode("utf-16"))


def test_too_big_refused(monkeypatch):
    monkeypatch.setattr("him_workbench.cda.MAX_BYTES", 100)
    with pytest.raises(CdaError, match="larger than"):
        parse_ccd(SAMPLE.read_bytes())


def test_missing_file_is_oserror(tmp_path):
    with pytest.raises(OSError):
        load_ccd(tmp_path / "nope.xml")


def test_audit_log_has_counts_only(tmp_path, monkeypatch, xml):
    import json
    from him_workbench import audit
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")
    parse_ccd(xml)
    line = (tmp_path / "audit.jsonl").read_text()
    assert "Sample" not in line and "TEST-0001" not in line and json.loads(line)["tool"] == "cda_parse"


def test_text_report_lists_sections_and_chart(doc):
    r = text_report(doc)
    assert "Patient: Patricia Sample" in r and "Procedures" in r and "not mapped" in r and "Problem list: 2 active" in r


def test_nullflavor_section_is_a_note_not_a_reader_gap(xml):
    i = xml.index('code="29762-2"')
    start = xml.rindex("<section>", 0, i)
    s, e = xml.index("<entry", i), xml.index("</entry>", i) + len("</entry>")
    x = xml[:start] + '<section nullFlavor="NI">' + xml[start + len("<section>"):s] + xml[e:]
    d = parse_ccd(x)
    assert "Social history section (narrative only)" not in d.chart.skipped
    assert any("nullFlavor NI" in n for n in d.notes)
    assert any(l == "29762-2" and "nullFlavor NI" in how for l, _, _, how in d.sections)


def test_negated_allergy_with_a_name(xml):
    d = parse_ccd(xml.replace('<observation classCode="OBS" moodCode="EVN">\n          <templateId root="2.16.840.1.113883.10.20.22.4.7"/>',
                              '<observation classCode="OBS" moodCode="EVN" negationInd="true">\n          <templateId root="2.16.840.1.113883.10.20.22.4.7"/>'))
    assert d.chart.allergies == [] and any("'Penicillin G' is marked negated" in n for n in d.notes)


def test_patient_id_prefers_the_id_with_an_extension(xml):
    x = xml.replace('<id root="2.16.840.1.113883.19.5" extension="TEST-0001"/>',
                    '<id root="9.9.9"/><id root="2.16.840.1.113883.19.5" extension="TEST-0001"/>')
    assert parse_ccd(x).patient.id == "TEST-0001"


def test_legal_name_preferred_over_first_listed(xml):
    x = xml.replace('<name use="L">', '<name use="A"><given>Alias</given><family>Other</family></name><name use="L">')
    assert parse_ccd(x).patient.last == "Sample"
