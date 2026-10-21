from datetime import date

import pytest

from him_workbench import audit
from him_workbench.matching import Patient
from him_workbench.quality import analyze

TODAY = date(2026, 1, 1)


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


def P(id, first="Robert", last="Alvarado", dob="1980-03-14", sex="M", zip="90210", phone=""):
    return Patient(id, first, last, dob, sex, zip, phone)


def test_clean_records_have_no_problems():
    r = analyze([P("1"), P("2", "Maria", "Okafor", "1991-11-02", "F", "60614", "555-010-1234")], TODAY)
    assert r.records == 2 and sum(r.problems.values()) == 0
    assert r.missing["first"] == 0 and r.missing["phone"] == 1  # first record has no phone


def test_missing_values_counted():
    r = analyze([P("1", first="", dob="", sex=" ", zip="")], TODAY)
    assert r.missing["first"] == 1 and r.missing["dob"] == 1 and r.missing["sex"] == 1 and r.missing["zip"] == 1


def test_date_problems():
    r = analyze([P("1", dob="1980-13-45"), P("2", dob="03/14/1980"), P("3", dob="2027-01-01"),
                 P("4", dob="1890-01-01"), P("5", dob="1980-03-14")], TODAY)
    assert r.problems["dob_malformed"] == 2
    assert r.problems["dob_in_future"] == 1
    assert r.problems["dob_over_120"] == 1


def test_sex_zip_phone_problems():
    r = analyze([P("1", sex="Male", zip="9021", phone="555-1234"),
                 P("2", sex="m", zip="90210-1234", phone="1-555-010-1234")], TODAY)
    assert r.problems["sex_unexpected"] == 1
    assert r.problems["zip_malformed"] == 1
    assert r.problems["phone_malformed"] == 1


def test_name_problems():
    r = analyze([P("1", first=" Robert "), P("2", last="ALVARADO"), P("3", first="robert"),
                 P("4", last="Smith2"), P("5", first="Baby Girl"), P("6", last="O'Brien-Smith Jr.")], TODAY)
    assert r.problems["name_whitespace"] == 1
    assert r.problems["name_case"] == 2
    assert r.problems["name_odd_characters"] == 1
    assert r.problems["name_placeholder"] == 1


def test_same_name_and_dob_groups():
    r = analyze([P("1"), P("2", "robert", "ALVARADO"), P("3", dob="1981-03-14"), P("4", "Maria", "Okafor")], TODAY)
    assert r.same_name_dob_groups == 1 and r.same_name_dob_records == 2


def test_output_never_contains_patient_values(tmp_path):
    r = analyze([P("1", first="Zzyzx", last="Qwertyuiop", dob="bad-date")], TODAY)
    assert "Zzyzx" not in r.text() and "Qwertyuiop" not in r.summary() and "bad-date" not in str(r.to_dict())
    log = (tmp_path / "audit.jsonl").read_text()
    assert "Zzyzx" not in log and "quality_report" in log


def test_empty_list():
    r = analyze([], TODAY)
    assert r.records == 0 and "0 records" in r.summary()


def test_field_blank_in_every_record_is_reported_separately():
    r = analyze([P("1"), P("2", "Maria", "Okafor", "1991-11-02", "F", "60614")], TODAY)  # no phones at all
    assert r.empty_fields == ["phone"]
    assert "phone" not in r.summary().split("Empty in every record")[0].split("Missing values:")[1]
    assert "Empty in every record (probably not in the source): phone" in r.summary()
    assert "probably not in the source" in r.text()


def test_accented_and_apostrophe_names_are_not_flagged():
    r = analyze([P("1", first="José", last="Nuñez"), P("2", first="Zoë", last="O\u2019Brien"),
                 P("3", first="Søren", last="Müller-Peña")], TODAY)
    assert r.problems["name_odd_characters"] == 0 and r.problems["name_case"] == 0


def test_digits_and_symbols_still_flagged():
    r = analyze([P("1", last="Smith2"), P("2", first="R@bert"), P("3", first="-Robert")], TODAY)
    assert r.problems["name_odd_characters"] == 3
