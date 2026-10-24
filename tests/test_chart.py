import json

from him_workbench.chart import chart_from_resources, load_chart, semantic_tag, text_chart


def cond(display, status="active", onset="2020-01-05T10:00:00-05:00", code="1"):
    return {"resourceType": "Condition", "clinicalStatus": {"coding": [{"code": status}]},
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": code, "display": display}]},
            "onsetDateTime": onset}


def obs(display, value, cat="vital-signs", date="2024-01-01T08:00:00Z", code="x"):
    return {"resourceType": "Observation", "category": [{"coding": [{"code": cat}]}],
            "code": {"coding": [{"code": code, "display": display}]},
            "valueQuantity": {"value": value, "unit": "mmHg"}, "effectiveDateTime": date}


def test_semantic_tag():
    assert semantic_tag("Hypertension (disorder)") == "disorder"
    assert semantic_tag("Medication review due (situation)") == "situation"
    assert semantic_tag("No tag here") == ""


def test_disorders_are_problems_and_situations_are_not():
    c = chart_from_resources([cond("Essential hypertension (disorder)"), cond("Medication review due (situation)"),
                              cond("Full-time employment (finding)")])
    assert [p.what.display for p in c.problems] == ["Essential hypertension (disorder)"]
    assert len(c.findings) == 2


def test_condition_keeps_code_status_and_dates():
    c = chart_from_resources([cond("Asthma (disorder)", status="resolved", code="195967001")])
    p = c.problems[0]
    assert (p.what.code, p.status, p.onset) == ("195967001", "resolved", "2020-01-05")


def test_medication_uses_rxnorm_concept_and_status():
    c = chart_from_resources([{"resourceType": "MedicationRequest", "status": "active", "authoredOn": "2023-05-01T00:00:00Z",
                               "medicationCodeableConcept": {"coding": [{"code": "314076", "display": "lisinopril 10 MG Oral Tablet"}]}}])
    m = c.medications[0]
    assert (m.what.display, m.status, m.date) == ("lisinopril 10 MG Oral Tablet", "active", "2023-05-01")


def test_vitals_keep_only_the_latest_value_per_test():
    c = chart_from_resources([obs("Systolic BP", 120, date="2023-01-01T00:00:00Z", code="8480-6"),
                              obs("Systolic BP", 135, date="2024-06-01T00:00:00Z", code="8480-6"),
                              obs("Systolic BP", 110, date="2022-01-01T00:00:00Z", code="8480-6")])
    assert [(r.value, r.date) for r in c.vitals] == [("135 mmHg", "2024-06-01")]


def test_labs_and_vitals_are_separate_and_other_observations_are_counted():
    c = chart_from_resources([obs("Glucose", 90, cat="laboratory", code="2339-0"), obs("Heart rate", 70, code="8867-4"),
                              obs("Tobacco", 1, cat="social-history", code="72166-2")])
    assert [r.what.display for r in c.labs] == ["Glucose"] and [r.what.display for r in c.vitals] == ["Heart rate"]
    assert [r.what.display for r in c.social] == ["Tobacco"] and not c.skipped


def test_visits_sorted_latest_first():
    enc = lambda d: {"resourceType": "Encounter", "class": {"code": "AMB"}, "type": [{"coding": [{"display": "Check-up"}]}],
                     "period": {"start": d + "T09:00:00Z"}}
    c = chart_from_resources([enc("2020-01-01"), enc("2024-03-03"), enc("2022-02-02")])
    assert [v.start for v in c.visits] == ["2024-03-03", "2022-02-02", "2020-01-01"]


def test_unused_resource_types_are_counted_not_dropped():
    c = chart_from_resources([{"resourceType": "Claim"}, {"resourceType": "Claim"}, {"resourceType": "Provenance"}])
    assert c.skipped == {"Claim": 2, "Provenance": 1}


def test_immunization_and_allergy():
    c = chart_from_resources([
        {"resourceType": "Immunization", "vaccineCode": {"coding": [{"code": "140", "display": "Influenza"}]},
         "occurrenceDateTime": "2023-10-01T00:00:00Z"},
        {"resourceType": "AllergyIntolerance", "clinicalStatus": {"coding": [{"code": "active"}]},
         "code": {"coding": [{"display": "Penicillin"}]}}])
    assert c.immunizations[0].date == "2023-10-01" and c.allergies[0].what.display == "Penicillin"


def test_bad_or_empty_input_does_not_crash():
    c = chart_from_resources([None, "x", {}, {"resourceType": "Condition"}, {"resourceType": "Observation"}])
    assert c.patient_id == "" and len(c.problems) == 0


def test_load_chart_reads_a_bundle_and_patient_id(tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p9"}}, {"resource": cond("Asthma (disorder)")}]}))
    c = load_chart(f)
    assert c.patient_id == "p9" and len(c.problems) == 1


def test_text_chart_keeps_situations_out_of_the_problem_list():
    c = chart_from_resources([cond("Asthma (disorder)"), cond("Medication review due (situation)")])
    t = text_chart(c, "Test Patient")
    problem_section = t.split("Medications:")[0]
    assert "Asthma" in problem_section and "1 active of 1 diagnoses" in t
    assert "Medication review due" not in problem_section and "Medication review due" in t.split("Other findings")[1]


def test_medication_name_follows_a_reference_to_a_medication_resource():
    med = {"resourceType": "Medication", "id": "m1", "code": {"coding": [{"code": "198405", "display": "Ibuprofen 100 MG Oral Tablet"}]}}
    req = {"resourceType": "MedicationRequest", "status": "completed", "medicationReference": {"reference": "urn:uuid:m1"}}
    c = chart_from_resources([req, med])
    assert c.medications[0].what.display == "Ibuprofen 100 MG Oral Tablet" and c.medications[0].what.code == "198405"


def test_unresolved_medication_is_labelled_not_blank():
    req = {"resourceType": "MedicationRequest", "status": "completed", "medicationReference": {"reference": "urn:uuid:nope"}}
    assert chart_from_resources([req]).medications[0].what.display == "(name not in record)"


def test_text_chart_lists_past_problems_findings_and_strips_tags():
    c = chart_from_resources([cond("Otitis media (disorder)", status="resolved"), cond("Full-time employment (finding)"),
                              {"resourceType": "Encounter", "class": {"code": "AMB"}, "type": [{"coding": [{"display": "Well child visit (procedure)"}]}],
                               "period": {"start": "2024-01-01T00:00:00Z"}}])
    t = text_chart(c)
    assert "Past problems" in t and "Otitis media" in t and "Full-time employment" in t
    assert "Well child visit" in t and "(procedure)" not in t and "(disorder)" not in t


def test_chart_to_dict_holds_the_chart_and_warns_about_ranges_and_partial_visits():
    from him_workbench.chart import chart_to_dict
    enc = lambda d: {"resourceType": "Encounter", "class": {"code": "AMB"}, "type": [{"coding": [{"display": "Check-up (procedure)"}]}],
                     "period": {"start": f"2020-01-{d:02d}T00:00:00Z"}}
    res = [cond("Asthma (disorder)", code="195967001"), cond("Prediabetes (finding)", code="714628002")] + [enc(i) for i in range(1, 29)] * 2
    d = chart_to_dict(chart_from_resources(res), "Test Patient", "1990-01-01", "F")
    assert d["patient"]["name"] == "Test Patient" and d["active_problems"][0] == {"name": "Asthma", "code": "195967001", "since": "2020-01-05"}
    assert d["other_findings_and_social_history"][0]["name"] == "Prediabetes"
    assert len(d["visits"]) == 40 and "Only the latest 40 of 56 visits" in d["note"]
    assert "no reference ranges" in d["note"]


def bp_panel(sys_, dia, date):
    return {"resourceType": "Observation", "category": [{"coding": [{"code": "vital-signs"}]}],
            "code": {"coding": [{"code": "85354-9", "display": "Blood pressure panel with all children optional"}]},
            "effectiveDateTime": date,
            "component": [
                {"code": {"coding": [{"code": "8480-6", "display": "Systolic Blood Pressure"}]}, "valueQuantity": {"value": sys_, "unit": "mm[Hg]"}},
                {"code": {"coding": [{"code": "8462-4", "display": "Diastolic Blood Pressure"}]}, "valueQuantity": {"value": dia, "unit": "mm[Hg]"}}]}


def test_blood_pressure_panel_components_are_read_not_dropped():
    c = chart_from_resources([bp_panel(118, 76, "2024-01-01T00:00:00Z"), bp_panel(124, 80, "2026-02-03T00:00:00Z")])
    by = {r.what.display: r for r in c.vitals}
    assert by["Systolic Blood Pressure"].value == "124 mm[Hg]" and by["Systolic Blood Pressure"].date == "2026-02-03"
    assert by["Diastolic Blood Pressure"].value == "80 mm[Hg]"
    assert by["Systolic Blood Pressure"].earlier == [("2024-01-01", "118 mm[Hg]")]
    assert not c.skipped


def test_earlier_values_are_kept_newest_first_and_capped():
    from him_workbench.chart import EARLIER_KEPT
    res = [obs("Heart rate", 60 + i, date=f"20{10 + i:02d}-01-01T00:00:00Z", code="8867-4") for i in range(15)]
    r = chart_from_resources(res).vitals[0]
    assert r.value == "74 mmHg" and len(r.earlier) == EARLIER_KEPT and r.earlier[0] == ("2023-01-01", "73 mmHg")


def test_skipped_observations_are_named_by_category():
    c = chart_from_resources([obs("Scan", 3, cat="imaging"), obs("Walk", 1, cat="activity"), obs("Mystery", 1, cat="")])
    assert c.skipped == {"Observation (imaging)": 1, "Observation (activity)": 1, "Observation": 1}


def test_chart_to_dict_carries_earlier_values_and_the_not_read_warning():
    from him_workbench.chart import chart_to_dict
    d = chart_to_dict(chart_from_resources([bp_panel(118, 76, "2024-01-01T00:00:00Z"), bp_panel(124, 80, "2026-02-03T00:00:00Z"),
                                            obs("Scan", 3, cat="imaging")]))
    sys_ = next(v for v in d["vitals_most_recent"] if v["name"] == "Systolic Blood Pressure")
    assert sys_["value"] == "124 mm[Hg]" and sys_["earlier"] == [{"date": "2024-01-01", "value": "118 mm[Hg]"}]
    assert "not read" in d["note"] and "Never call a change better" in d["note"]


def test_social_history_and_questionnaires_are_read_with_their_own_values():
    tobacco = {"resourceType": "Observation", "category": [{"coding": [{"code": "social-history"}]}],
               "code": {"coding": [{"code": "72166-2", "display": "Tobacco smoking status"}]},
               "valueCodeableConcept": {"coding": [{"display": "Never smoker"}]}, "effectiveDateTime": "2026-02-03T00:00:00Z"}
    prapare = {"resourceType": "Observation", "category": [{"coding": [{"code": "survey"}]}], "effectiveDateTime": "2024-01-30T00:00:00Z",
               "code": {"coding": [{"code": "93025-5", "display": "PRAPARE"}]},
               "component": [{"code": {"coding": [{"code": "56051-6", "display": "Housing status"}]},
                              "valueCodeableConcept": {"coding": [{"display": "I have housing"}]}},
                             {"code": {"coding": [{"code": "x", "display": "Worried about losing housing"}]}, "valueBoolean": False}]}
    phq = obs("PHQ-9 total score", 3, cat="survey", code="44261-6")
    c = chart_from_resources([tobacco, prapare, phq])
    assert [(r.what.display, r.value) for r in c.social] == [("Tobacco smoking status", "Never smoker")]
    assert {(r.what.display, r.value) for r in c.screenings} == {
        ("Housing status", "I have housing"), ("Worried about losing housing", "no"), ("PHQ-9 total score", "3 mmHg")}
    assert not c.skipped


def test_questionnaire_earlier_values_are_capped_small():
    from him_workbench.chart import SCREEN_EARLIER_KEPT
    res = [obs("PHQ-2 total score", i, cat="survey", date=f"20{10 + i:02d}-01-01T00:00:00Z", code="55758-7") for i in range(8)]
    assert len(chart_from_resources(res).screenings[0].earlier) == SCREEN_EARLIER_KEPT


def test_chart_to_dict_has_social_and_questionnaire_sections_and_the_score_rule():
    from him_workbench.chart import chart_to_dict
    d = chart_to_dict(chart_from_resources([obs("PHQ-9 total score", 3, cat="survey", code="44261-6"),
                                            obs("Tobacco", 1, cat="social-history", code="72166-2")]))
    assert d["screening_questionnaires"][0]["name"] == "PHQ-9 total score" and d["social_history_observations"][0]["name"] == "Tobacco"
    assert "never say what a score means" in d["note"]


def test_value_concept_tag_is_stripped_and_year_unit_spelled_out():
    obs = lambda extra: {"resourceType": "Observation", "id": "o", "status": "final",
                         "category": [{"coding": [{"code": "survey"}]}],
                         "effectiveDateTime": "2026-02-03", **extra}
    smoke = obs({"code": {"coding": [{"code": "72166-2", "display": "Tobacco smoking status"}]},
                 "valueCodeableConcept": {"coding": [{"code": "266919005", "display": "Never smoked tobacco (finding)"}]}})
    income = obs({"code": {"coding": [{"code": "x", "display": "Income"}]},
                  "valueQuantity": {"value": 50768, "unit": "/a"}})
    c = chart_from_resources([smoke, income])
    vals = {r.what.display: r.value for r in c.screenings}
    assert vals["Tobacco smoking status"] == "Never smoked tobacco"
    assert vals["Income"] == "50768 per year"
