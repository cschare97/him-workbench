import json

import pytest

from him_workbench import audit
from him_workbench.fhir import describe, load_fhir_patients, patient_from_fhir


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


def pat(pid="p1", **kw):
    base = {"resourceType": "Patient", "id": pid, "gender": "female", "birthDate": "1980-03-14",
            "name": [{"use": "official", "family": "Alvarado", "given": ["Maria", "Elena"]}],
            "address": [{"postalCode": "90210"}],
            "telecom": [{"system": "email", "value": "x@example.com"}, {"system": "phone", "value": "555-0100"}]}
    base.update(kw)
    return base


def test_maps_the_fields_the_matcher_uses():
    p, why = patient_from_fhir(pat())
    assert why == "" and (p.id, p.first, p.last, p.dob, p.sex, p.zip, p.phone) == (
        "p1", "Maria", "Alvarado", "1980-03-14", "F", "90210", "555-0100")


def test_official_name_beats_maiden_and_nickname():
    names = [{"use": "maiden", "family": "Old", "given": ["Mary"]}, {"use": "nickname", "family": "Alvarado", "given": ["Mia"]},
             {"use": "official", "family": "Alvarado", "given": ["Maria"]}]
    assert patient_from_fhir(pat(name=names))[0].first == "Maria"
    assert patient_from_fhir(pat(name=names[:1]))[0] is None   # a maiden name alone is not a usable name


def test_other_and_unknown_gender_are_blank_not_a_conflict():
    assert patient_from_fhir(pat(gender="other"))[0].sex == ""
    assert patient_from_fhir(pat(gender="unknown"))[0].sex == ""
    assert patient_from_fhir(pat(gender="male"))[0].sex == "M"


def test_deceased_left_out_unless_asked():
    assert patient_from_fhir(pat(deceasedBoolean=True)) == (None, "deceased")
    assert patient_from_fhir(pat(deceasedDateTime="2020-01-01"))[1] == "deceased"
    assert patient_from_fhir(pat(deceasedBoolean=True), include_deceased=True)[0] is not None


def test_non_patient_and_missing_name_or_id():
    assert patient_from_fhir({"resourceType": "Observation"}) == (None, "not_patient")
    assert patient_from_fhir(pat(name=[]))[1] == "no_name"
    assert patient_from_fhir({"resourceType": "Patient", "name": pat()["name"]})[1] == "no_name"


def test_loads_bundle_ndjson_single_resource_and_folder_with_a_report(tmp_path):
    (tmp_path / "bundle.json").write_text(json.dumps({"resourceType": "Bundle", "entry": [
        {"resource": pat("a")}, {"resource": {"resourceType": "Encounter", "id": "e"}}, {"resource": pat("gone", deceasedBoolean=True)}]}))
    (tmp_path / "bulk.ndjson").write_text(json.dumps(pat("b")) + "\n\n" + json.dumps(pat("a")) + "\n")  # "a" repeats
    (tmp_path / "single.json").write_text(json.dumps(pat("c")))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "notes.txt").write_text("ignored")
    patients, stats = load_fhir_patients(tmp_path)
    assert sorted(p.id for p in patients) == ["a", "b", "c"]
    assert stats == {"files": 4, "patients": 3, "deceased_left_out": 1, "no_name_or_id": 0, "unreadable_files": 1}
    assert "1 deceased left out" in describe(stats) and "1 unreadable files" in describe(stats)


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_fhir_patients(tmp_path / "nope")


def test_fhir_patients_run_through_the_matcher(tmp_path):
    from him_workbench.matching import find_duplicates
    a = pat("1", name=[{"use": "official", "family": "Alvarado", "given": ["Robert"]}], gender="male")
    b = pat("2", name=[{"use": "official", "family": "Alvarado", "given": ["Bob"]}], gender="male")
    (tmp_path / "x.ndjson").write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    patients, _ = load_fhir_patients(tmp_path)
    assert find_duplicates(patients)[0].label == "match"


def test_load_patients_labels_the_source(tmp_path):
    from him_workbench.data import load_patients
    (tmp_path / "p.json").write_text(json.dumps(pat("a")))
    patients, label = load_patients(["--fhir", str(tmp_path)])
    assert len(patients) == 1 and label.startswith("FHIR files from") and "1 patients from 1 files" in label
    assert load_patients([], default_csv=tmp_path / "missing.csv") == ([], "")
