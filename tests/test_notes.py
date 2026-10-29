from him_workbench.chart import chart_from_resources
from him_workbench.notes import generate_note


def cond(display, status="active", onset="2020-01-05T10:00:00-05:00"):
    return {"resourceType": "Condition", "clinicalStatus": {"coding": [{"code": status}]},
            "code": {"coding": [{"display": display}]}, "onsetDateTime": onset}


def med(display):
    return {"resourceType": "MedicationRequest", "status": "active", "authoredOn": "2023-05-01T00:00:00Z",
            "medicationCodeableConcept": {"coding": [{"display": display}]}}


def visit(kind, start):
    return {"resourceType": "Encounter", "type": [{"coding": [{"display": kind}]}],
            "period": {"start": start}, "class": {"code": "AMB"}}


def test_note_mentions_active_problems_and_meds_not_resolved_ones():
    c = chart_from_resources([cond("Essential hypertension (disorder)"),
                              cond("Asthma (disorder)", status="resolved"),
                              med("lisinopril 10 MG Oral Tablet")])
    note = generate_note("p1", c, name="Jordan Casey")
    assert "Jordan Casey" in note.text and "Essential hypertension" in note.text
    assert "Asthma" not in note.text          # resolved, not active
    assert "lisinopril" in note.text


def test_note_mentions_most_recent_visit():
    c = chart_from_resources([visit("Office visit", "2026-01-01"), visit("Well child visit", "2026-06-01")])
    note = generate_note("p1", c)
    assert "2026-06-01" in note.text and "Well child visit" in note.text


def test_empty_chart_still_produces_a_note():
    note = generate_note("p1", chart_from_resources([]), name="Pat Doe")
    assert "Pat Doe" in note.text and "none on record" in note.text and "No visits on record" in note.text


def test_duplicate_problems_are_collapsed_once():
    # Synthea-style data can record the same condition name at several stages/encounters; a note
    # shouldn't repeat it every time, since duplicates add length without adding information.
    c = chart_from_resources([cond("Chronic kidney disease stage 1 (disorder)"),
                              cond("Chronic kidney disease stage 1 (disorder)"),
                              cond("Chronic kidney disease stage 1 (disorder)")])
    note = generate_note("p1", c)
    assert note.text.count("Chronic kidney disease stage 1") == 1


def test_a_very_long_problem_list_is_capped_not_dropped():
    conditions = [cond(f"Condition number {i} (disorder)") for i in range(40)]
    note = generate_note("p1", chart_from_resources(conditions))
    assert "Condition number 0" in note.text   # still useful, just not exhaustive
    assert "more)" in note.text
    assert "Condition number 39" not in note.text  # past the cap


def test_an_overly_long_note_is_truncated_so_it_stays_embeddable():
    # Even after capping each list, a patient could still produce a note too long for a local
    # embedding model's context window. The note must never just fail outright — it gets cut.
    conditions = [cond(f"A very long and highly specific staged diagnosis description, stage {i} "
                        f"of a chronic progressive condition with extensive documentation (disorder)")
                  for i in range(20)]
    meds = [med(f"A similarly long and specific medication name, dose, route and strength, "
                f"regimen variant number {i}, 10 MG Oral Tablet, taken twice daily with food")
            for i in range(20)]
    note = generate_note("p1", chart_from_resources(conditions + meds))
    assert len(note.text) <= 4000 + len("... [truncated]")
    assert note.text.endswith("... [truncated]")
