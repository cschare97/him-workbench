"""Edge-case tests for the diabetes measures, using hand-built patients."""
from datetime import date

from him_workbench.care_gaps.fhir_load import (A1C_LOINC, EGFR_LOINC, RETINAL_EXAM_SNOMED, UACR_LOINC,
                       Condition, Observation, PatientFacts)
from him_workbench.care_gaps.measures import cms122, cms131, cms951


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
    from him_workbench.care_gaps.report import to_json
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
    from him_workbench.care_gaps.fhir_load import clean_name
    assert clean_name("Annamae625 Luciana251 Beatty507") == "Annamae Luciana Beatty"
    assert clean_name("Julianne852 O'Reilly797") == "Julianne O'Reilly"
    assert clean_name("Pedro316 Martín25 Farías160") == "Pedro Martín Farías"


# --- gap types ---

def test_missing_a1c_with_no_note_is_care_gap():
    assert cms122(make_patient()).kind == "care"


def test_missing_a1c_mentioned_in_note_is_documentation_gap():
    p = make_patient()
    p.notes.append((date(2026, 5, 1), "Outside lab: Hemoglobin A1c 7.4%"))
    result = cms122(p)
    assert result.status == "met" and result.kind == "documentation"
    assert "2026-05-01" in result.reason


def test_note_from_last_year_does_not_count():
    p = make_patient()
    p.notes.append((date(2025, 5, 1), "A1c 7.4%"))
    assert cms122(p).kind == "care"


def test_a1c_ordered_without_result_is_open_order():
    from him_workbench.care_gaps.fhir_load import A1C_LOINC as code
    p = make_patient()
    p.orders.append((code, date(2026, 4, 2), "active"))
    result = cms122(p)
    assert result.kind == "open order" and "2026-04-02" in result.reason


def test_high_a1c_is_care_gap():
    p = make_patient()
    a1c(p, date(2026, 9, 1), 10.1)
    assert cms122(p).kind == "care"


def test_passing_result_has_no_kind():
    p = make_patient()
    a1c(p, date(2026, 9, 1), 7.0)
    assert cms122(p).kind == ""


def test_outside_eye_exam_note_is_documentation_gap():
    p = make_patient()
    p.notes.append((date(2026, 3, 9), "Patient reports a dilated eye exam at an outside office."))
    assert cms131(p).kind == "documentation"


def test_word_a1c_inside_other_word_does_not_match():
    p = make_patient()
    p.notes.append((date(2026, 3, 9), "code XA1C99 in the billing note"))
    assert cms122(p).kind == "care"


# --- CMS951 follows its own spec (ages 18-85, diabetes at start, ESRD/CKD5 exclusions) ---

def test_cms951_includes_age_80_but_cms122_does_not():
    p = make_patient(birth=date(1946, 3, 1))
    assert cms122(p).status == "not in denominator"
    assert cms951(p).status != "not in denominator"


def test_cms951_excludes_age_86():
    assert cms951(make_patient(birth=date(1940, 1, 1))).status == "not in denominator"


def test_diabetes_diagnosed_during_period_counts_for_cms122_not_cms951():
    p = make_patient(diabetes=False)
    p.conditions.append(Condition("44054006", "Diabetes mellitus type 2", date(2026, 4, 1), None))
    assert cms122(p).status != "not in denominator"
    result = cms951(p)
    assert result.status == "not in denominator" and "during the period" in result.reason


def test_cms951_excludes_active_esrd():
    p = make_patient()
    p.conditions.append(Condition("46177005", "End-stage renal disease (disorder)", date(2023, 1, 1), None))
    assert cms951(p).status == "excluded"
    assert cms122(p).status != "excluded"  # ESRD is not a CMS122 exclusion


def test_cms951_excludes_ckd5_by_name():
    p = make_patient()
    p.conditions.append(Condition("999", "Chronic kidney disease stage 5 (disorder)", date(2024, 1, 1), None))
    assert cms951(p).status == "excluded"


def test_cms951_ckd4_is_not_excluded():
    p = make_patient()
    p.conditions.append(Condition("431857002", "Chronic kidney disease stage 4 (disorder)", date(2024, 1, 1), None))
    assert cms951(p).status != "excluded"


def test_cms951_esrd_resolved_before_period_is_not_excluded():
    p = make_patient()
    p.conditions.append(Condition("46177005", "End-stage renal disease", date(2020, 1, 1), date(2024, 6, 1)))
    assert cms951(p).status != "excluded"


# --- evidence (5b) ---

def _ev(results, prefix):
    from him_workbench.care_gaps.measures import evaluate  # noqa: F401
    return next(r for r in results if r.measure.startswith(prefix)).evidence


def test_evidence_ids_are_sequential_and_json_safe():
    import json
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    a1c(p, date(2026, 9, 1), 7.0)
    for r in evaluate(p):
        assert [e["id"] for e in r.evidence] == [f"E{i}" for i in range(1, len(r.evidence) + 1)]
        json.dumps(r.evidence)


def test_missing_a1c_shows_last_result_before_period():
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    a1c(p, date(2025, 11, 3), 7.9)
    labels = {e["label"]: e for e in _ev(evaluate(p), "CMS122")}
    last = labels["HbA1c (last result before the period)"]
    assert last["date"] == "2025-11-03" and last["value"] == 7.9


def test_no_a1c_ever_is_stated_as_absent():
    from him_workbench.care_gaps.measures import evaluate
    assert any(e["type"] == "absent" and "HbA1c" in e["label"] for e in _ev(evaluate(make_patient()), "CMS122"))


def test_documentation_gap_evidence_includes_note_excerpt():
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    p.notes.append((date(2026, 5, 1), "Long visit text. " * 20 + "Outside lab: Hemoglobin A1c 7.4%. " + "More. " * 30))
    notes = [e for e in _ev(evaluate(p), "CMS122") if e["type"] == "note"]
    assert notes and "A1c 7.4%" in notes[0]["text"] and len(notes[0]["text"]) < 300


def test_many_a1c_results_are_capped_with_a_summary():
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    for m in range(1, 10):
        a1c(p, date(2026, m, 1), 7.0)
    ev = _ev(evaluate(p), "CMS122")
    assert len([e for e in ev if e["type"] == "lab"]) == 5
    assert any(e["type"] == "summary" and "9 results" in e["label"] for e in ev)
    assert [e["date"] for e in ev if e["type"] == "lab"][0] == "2026-09-01"  # newest first


def test_cms951_evidence_states_its_own_age_range_and_exclusion():
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    p.conditions.append(Condition("46177005", "End-stage renal disease (disorder)", date(2023, 1, 1), None))
    ev = _ev(evaluate(p), "CMS951")
    assert any(e["label"] == "Measure age range" and e["value"] == "18-85" for e in ev)
    assert any(e["type"] == "exclusion" and "End-stage renal disease" in e["label"] for e in ev)


def test_evidence_does_not_change_results():
    from him_workbench.care_gaps.measures import evaluate
    p = make_patient()
    a1c(p, date(2026, 9, 1), 9.6)
    statuses = [(r.measure, r.status, r.kind) for r in evaluate(p)]
    assert statuses == [(f(p).measure, f(p).status, f(p).kind) for f in (cms122, cms131, cms951)]
