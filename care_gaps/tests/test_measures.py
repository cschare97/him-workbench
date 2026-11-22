"""Edge-case tests for the diabetes measures, using hand-built patients."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fhir_load import (A1C_LOINC, EGFR_LOINC, RETINAL_EXAM_SNOMED, UACR_LOINC,
                       Condition, Observation, PatientFacts)
from measures import cms122, cms131, cms951


def make_patient(birth=date(1970, 5, 1), deceased=None, diabetes=True, visit=True):
    facts = PatientFacts("p1", "Test Patient", birth, deceased)
    if diabetes:
        facts.conditions.append(Condition("44054006", "Diabetes mellitus type 2", date(2015, 1, 1), None))
    if visit:
        facts.encounters.append(("AMB", "General examination", date(2026, 3, 10)))
    return facts


def a1c(facts, day, value):
    facts.observations.append(Observation(A1C_LOINC, day, value))


# --- denominator ---

def test_non_diabetic_not_in_denominator():
    assert cms122(make_patient(diabetes=False)).status == "not in denominator"


def test_prediabetes_does_not_count():
    p = make_patient(diabetes=False)
    p.conditions.append(Condition("15777000", "Prediabetes", date(2020, 1, 1), None))
    assert cms122(p).status == "not in denominator"


def test_age_76_at_end_of_period_excluded_from_denominator():
    assert cms122(make_patient(birth=date(1950, 6, 1))).status == "not in denominator"


def test_age_75_on_last_day_still_counts():
    assert cms122(make_patient(birth=date(1951, 12, 31))).status != "not in denominator"


def test_no_visit_in_period_not_in_denominator():
    assert cms122(make_patient(visit=False)).status == "not in denominator"


def test_deceased_patient_left_out():
    assert cms122(make_patient(deceased=date(2026, 6, 1))).status == "not in denominator"


def test_hospice_is_excluded():
    p = make_patient()
    p.encounters.append(("HH", "Hospice care encounter", date(2026, 8, 1)))
    assert cms122(p).status == "excluded"


# --- CMS122 (inverse: "met" = failing) ---

def test_missing_a1c_counts_as_failing():
    result = cms122(make_patient())
    assert result.status == "met"
    assert "no structured A1c" in result.reason


def test_a1c_from_last_year_does_not_count():
    p = make_patient()
    a1c(p, date(2025, 11, 1), 7.0)
    assert cms122(p).status == "met"


def test_latest_a1c_above_9_fails():
    p = make_patient()
    a1c(p, date(2026, 2, 1), 7.0)
    a1c(p, date(2026, 9, 1), 9.4)
    assert cms122(p).status == "met"


def test_exactly_9_is_not_failing():
    p = make_patient()
    a1c(p, date(2026, 9, 1), 9.0)
    assert cms122(p).status == "not met"


def test_latest_result_wins_even_if_earlier_was_high():
    p = make_patient()
    a1c(p, date(2026, 2, 1), 10.5)
    a1c(p, date(2026, 9, 1), 7.8)
    assert cms122(p).status == "not met"


def test_same_day_results_use_lowest():
    p = make_patient()
    a1c(p, date(2026, 9, 1), 9.6)
    a1c(p, date(2026, 9, 1), 8.8)
    assert cms122(p).status == "not met"


# --- CMS131 eye exam ---

def test_eye_exam_in_period_met():
    p = make_patient()
    p.procedures.append((RETINAL_EXAM_SNOMED, "Diabetic retinal eye exam", date(2026, 4, 2)))
    assert cms131(p).status == "met"


def test_prior_year_exam_counts_without_retinopathy():
    p = make_patient()
    p.procedures.append((RETINAL_EXAM_SNOMED, "Diabetic retinal eye exam", date(2025, 7, 1)))
    assert cms131(p).status == "met"


def test_prior_year_exam_does_not_count_with_retinopathy():
    p = make_patient()
    p.conditions.append(Condition("422034002", "Diabetic retinopathy", date(2022, 1, 1), None))
    p.procedures.append((RETINAL_EXAM_SNOMED, "Diabetic retinal eye exam", date(2025, 7, 1)))
    assert cms131(p).status == "not met"


def test_no_exam_not_met():
    assert cms131(make_patient()).status == "not met"


# --- CMS951 kidney health ---

def test_kidney_needs_both_tests():
    p = make_patient()
    p.observations.append(Observation(EGFR_LOINC, date(2026, 5, 1), 85))
    result = cms951(p)
    assert result.status == "not met"
    assert "uACR" in result.reason


def test_kidney_met_with_both():
    p = make_patient()
    p.observations.append(Observation(EGFR_LOINC, date(2026, 5, 1), 85))
    p.observations.append(Observation(UACR_LOINC, date(2026, 5, 1), 12))
    assert cms951(p).status == "met"


# --- page snapshot ---

def test_snapshot_marks_gaps_and_sorts_worst_first():
    from report import to_json
    rows = [
        {"patient_id": "a", "name": "Ann", "measure": "CMS122 A1c >9%", "status": "not met", "reason": "ok"},
        {"patient_id": "a", "name": "Ann", "measure": "CMS131 Eye exam", "status": "met", "reason": "ok"},
        {"patient_id": "b", "name": "Bob", "measure": "CMS122 A1c >9%", "status": "met", "reason": "missing"},
        {"patient_id": "b", "name": "Bob", "measure": "CMS131 Eye exam", "status": "not met", "reason": "no exam"},
        {"patient_id": "c", "name": "Cy", "measure": "CMS122 A1c >9%", "status": "not in denominator", "reason": "dead"},
    ]
    snap = to_json(rows)
    assert [p["name"] for p in snap["patients"]] == ["Bob", "Ann"]  # Cy is in no denominator
    assert snap["patients"][0]["gap_count"] == 2
    cms122 = next(s for s in snap["summary"] if s["measure"].startswith("CMS122"))
    assert cms122["gaps"] == 1 and cms122["inverse"]


def test_clean_name_drops_synthea_numbers():
    from fhir_load import clean_name
    assert clean_name("Annamae625 Luciana251 Beatty507") == "Annamae Luciana Beatty"
    assert clean_name("Julianne852 O'Reilly797") == "Julianne O'Reilly"
    assert clean_name("Pedro316 Martín25 Farías160") == "Pedro Martín Farías"
