"""Tests for explain.py. A fake model stands in for Ollama, so these run anywhere.

The spec text here is made up; real spec text is copyrighted and never stored in this repo.
"""

from him_workbench.care_gaps.explain import check_grounding, cited_sections, explain, select_sections

SPECS = {
    "CMS122": {"url": "https://example.test/122", "meta": {"Copyright": "Notice 122"},
               "sections": {"Numerator": "Made-up numerator: above 9.0% or missing counts.",
                            "Guidance": "Made-up guidance: a result in the record counts.",
                            "Initial Population": "Made-up population 18-75.",
                            "Denominator Exclusions": "Made-up exclusions."}},
    "CMS951": {"url": "https://example.test/951", "meta": {"Copyright": "Notice 951"},
               "sections": {"Numerator": "Made-up kidney numerator.", "Guidance": "Made-up uACR guidance."}},
}

DOC_GAP = {
    "measure": "CMS122 A1c >9%", "status": "met", "gap": True, "kind": "documentation",
    "reason": "no structured A1c result in the period, but a note on 2026-10-07 mentions an A1c",
    "evidence": [
        {"id": "E1", "type": "lab", "label": "HbA1c (last result before the period)", "date": "2025-10-01", "value": 6.06, "text": None},
        {"id": "E2", "type": "note", "label": "Note mentioning an A1c", "date": "2026-10-07", "value": None,
         "text": "Outside lab A1c 6.1% drawn on 2026-10-07."},
    ],
}


def fake(answer):
    return lambda model, prompt: answer


# --- section selection ---

def test_documentation_gap_cites_numerator_and_guidance():
    assert select_sections(DOC_GAP) == ["Numerator", "Guidance"]


def test_not_in_denominator_cites_initial_population():
    assert select_sections({"measure": "CMS951 Kidney health", "status": "not in denominator"}) == ["Initial Population"]


def test_excluded_cites_exclusions():
    assert select_sections({"measure": "CMS122 A1c >9%", "status": "excluded"}) == ["Denominator Exclusions"]


# --- licensing rules ---

def test_public_mode_hides_ncqa_text_but_keeps_link():
    s = cited_sections(DOC_GAP, SPECS, public=True)
    assert all(x["text"] is None for x in s) and all(x["url"] == "https://example.test/122" for x in s)


def test_public_mode_shows_nkf_text():
    result = {"measure": "CMS951 Kidney health", "status": "not met", "kind": "care"}
    assert all(x["text"] for x in cited_sections(result, SPECS, public=True))


def test_local_mode_shows_text_with_notice():
    s = cited_sections(DOC_GAP, SPECS, public=False)
    assert s[0]["text"].startswith("Made-up numerator") and s[0]["notice"].startswith("Notice 122")


def test_public_mode_never_sends_ncqa_text_to_the_model():
    seen = {}
    def capture(model, prompt):
        seen["prompt"] = prompt
        return "x [E1]"
    explain(DOC_GAP, SPECS, model="m", public=True, generate=capture)
    assert "Made-up numerator" not in seen["prompt"]


# --- grounding check ---

def test_good_answer_passes():
    answer = ("There is no structured A1c in 2026 [E1]. A note on 2026-10-07 says an outside lab found 6.1% [E2]. "
              "A missing result counts as failing [S1]. Enter the outside result as structured data [S2].")
    sections = cited_sections(DOC_GAP, SPECS, public=False)
    assert check_grounding(answer, DOC_GAP, DOC_GAP["evidence"], sections) == []


def test_invented_number_is_caught():
    answer = "The A1c was 8.4% [E2]."
    assert any("8.4" in p for p in check_grounding(answer, DOC_GAP, DOC_GAP["evidence"], []))


def test_calculated_number_is_caught():
    answer = "The last result was 371 days before the note [E1]."
    assert any("371" in p for p in check_grounding(answer, DOC_GAP, DOC_GAP["evidence"], []))


def test_made_up_citation_is_caught():
    answer = "A note mentions it [E9]."
    assert any("E9" in p for p in check_grounding(answer, DOC_GAP, DOC_GAP["evidence"], []))


def test_answer_without_citations_is_caught():
    assert "no citations" in check_grounding("Looks fine.", DOC_GAP, DOC_GAP["evidence"], [])


def test_citing_a_hidden_section_is_caught():
    hidden = cited_sections(DOC_GAP, SPECS, public=True)  # text hidden, so S1 can't be cited
    assert any("S1" in p for p in check_grounding("Missing counts [S1].", DOC_GAP, DOC_GAP["evidence"], hidden))


def test_leading_zero_dates_match():
    assert check_grounding("Seen on October 07 [E2].", DOC_GAP, DOC_GAP["evidence"], []) == []


# --- end to end ---

def test_no_model_uses_fallback():
    out = explain(DOC_GAP, SPECS, model=None)
    assert out["mode"] == "fallback" and "Documentation gap" in out["text"]


def test_rejected_answer_falls_back_and_keeps_the_reason():
    out = explain(DOC_GAP, SPECS, model="m", generate=fake("The A1c was 12.5% [E2]."))
    assert out["mode"] == "fallback" and out["rejected_answer"] and "12.5" in out["problems"][0]


def test_model_error_falls_back():
    def broken(model, prompt):
        raise OSError("connection refused")
    out = explain(DOC_GAP, SPECS, model="m", generate=broken)
    assert out["mode"] == "fallback" and "connection refused" in out["problems"][0]


def test_grounded_answer_is_used():
    out = explain(DOC_GAP, SPECS, model="m", generate=fake("A note on 2026-10-07 mentions an A1c of 6.1% [E2]."))
    assert out["mode"] == "model" and out["model"] == "m"


def test_missing_spec_cache_still_explains():
    out = explain(DOC_GAP, specs=None, model=None)
    assert out["sections"][0]["text"] is None and out["text"]


# --- found by testing with a real model (qwen2.5:7b) ---

def test_made_up_bracket_labels_are_caught():
    problems = check_grounding("No structured result [Result]. Review the chart [Reason]. Note [E2].",
                               DOC_GAP, DOC_GAP["evidence"], [])
    assert any("Reason" in p and "Result" in p for p in problems)


def test_grouped_citations_are_accepted():
    assert check_grounding("A note on 2026-10-07 mentions it [E1, E2].", DOC_GAP, DOC_GAP["evidence"], []) == []


def test_saying_met_for_a_gap_is_caught():
    problems = check_grounding("The measure is met because no result is on file [E1].", DOC_GAP, DOC_GAP["evidence"], [])
    assert any("'met'" in p for p in problems)


def test_prompt_states_outcome_and_why_note_does_not_count():
    from him_workbench.care_gaps.explain import build_prompt
    prompt = build_prompt(DOC_GAP, DOC_GAP["evidence"], [])
    assert "has a care gap" in prompt and "invisible to the measure" in prompt
    assert "is 'met'" not in prompt.split("Project rules:")[1]


# --- 5d: project rules get their own [R#] tags ---

def test_rules_are_tagged_and_citable():
    from him_workbench.care_gaps.explain import project_rules
    rules = project_rules(DOC_GAP)
    assert [r["id"] for r in rules] == ["R1", "R2", "R3"]
    answer = ("A note on 2026-10-07 mentions an A1c [E2]. The measure only sees structured results [R2]. "
              "Enter it as structured data [R3].")
    assert check_grounding(answer, DOC_GAP, DOC_GAP["evidence"], []) == []


def test_passing_patient_has_no_next_step_rule():
    from him_workbench.care_gaps.explain import project_rules
    passing = {"measure": "CMS122 A1c >9%", "status": "not met", "gap": False, "kind": "", "reason": "most recent A1c 6.8%"}
    assert [r["label"] for r in project_rules(passing)] == ["Measure logic result"]


def test_prompt_reserves_s_tags_for_spec_text():
    from him_workbench.care_gaps.explain import build_prompt
    prompt = build_prompt(DOC_GAP, DOC_GAP["evidence"], cited_sections(DOC_GAP, SPECS, public=False))
    assert "[R3] Suggested next step" in prompt and "[S#] only for what the official spec text" in prompt


# --- 5d: page data ---

SNAP = {"patients": [{"id": "p1", "name": "Carlos Wolf", "scenario": "a1c_in_note", "results": [DOC_GAP]}]}


def test_page_data_finds_patient_and_measure():
    from him_workbench.care_gaps.explain import page_data
    v = page_data(SNAP, "p1", "CMS122", specs=SPECS)
    assert v["patient_name"] == "Carlos Wolf" and v["kind_label"] == "Documentation gap" and v["gap"]
    assert v["builtin"].startswith("CMS122") and v["mode"] == "fallback"


def test_page_data_unknown_patient_or_measure_is_none():
    from him_workbench.care_gaps.explain import page_data
    assert page_data(SNAP, "nope", "CMS122", specs=SPECS) is None
    assert page_data(SNAP, "p1", "CMS999", specs=SPECS) is None


def test_page_data_public_hides_ncqa_text():
    from him_workbench.care_gaps.explain import page_data
    v = page_data(SNAP, "p1", "CMS122", public=True, specs=SPECS)
    assert v["public"] and all(s["text"] is None for s in v["sections"])


# --- fixes from the first look at the page ---

def test_missing_care_and_out_of_range_get_different_next_steps():
    from him_workbench.care_gaps.explain import next_step
    missing = {"measure": "CMS131 Eye exam", "status": "not met", "gap": True, "kind": "care",
               "reason": "no structured retinal exam on file for the period"}
    high = {"measure": "CMS122 A1c >9%", "status": "met", "gap": True, "kind": "care",
            "reason": "most recent A1c 11.8% on 2026-07-30 is above 9%"}
    assert "schedule" in next_step(missing) and "out-of-range" not in next_step(missing)
    assert "out-of-range" in next_step(high) and "schedule" not in next_step(high)


def test_rule_text_reads_as_a_sentence():
    from him_workbench.care_gaps.explain import project_rules
    r1 = project_rules(DOC_GAP)[0]["text"]
    assert "measure (counts against the clinic). No structured" in r1 and r1.endswith(".")
