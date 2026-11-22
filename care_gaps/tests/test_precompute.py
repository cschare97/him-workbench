"""Tests for precompute_summaries.py with a fake model. Spec text here is made up."""
import copy
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from explain import explain
from precompute_summaries import precompute

SPECS = {"CMS122": {"url": "https://example.test/122", "meta": {"Copyright": "n", "Disclaimer": ""},
                    "sections": {"Numerator": "Made-up NCQA numerator.", "Guidance": "Made-up NCQA guidance."}}}

GAP = {"measure": "CMS122 A1c >9%", "status": "met", "gap": True, "kind": "documentation",
       "reason": "no structured A1c result in the period, but a note on 2026-10-07 mentions an A1c",
       "evidence": [{"id": "E1", "type": "note", "label": "Note", "date": "2026-10-07", "value": None,
                     "text": "Outside lab A1c 6.1%."}]}
PASS = {"measure": "CMS131 Eye exam", "status": "met", "gap": False, "kind": "", "reason": "retinal exam on 2026-04-01",
        "evidence": []}
GOOD = "A note on 2026-10-07 mentions an A1c of 6.1% [E1]. Enter it as structured data [R3]."
BAD = "The A1c was 7.9% [E1]."


def snapshot():
    return {"patients": [{"id": "p1", "name": "Carlos Wolf", "results": [copy.deepcopy(GAP), copy.deepcopy(PASS)]}]}


def test_keeps_a_checked_answer_only_for_gaps():
    snap = snapshot()
    counts = precompute(snap, "m", SPECS, generate=lambda m, p: GOOD, today=date(2026, 11, 22))
    gap, passing = snap["patients"][0]["results"]
    assert counts["kept"] == 1 and gap["summary"] == {"model": "m", "date": "2026-11-22", "text": GOOD}
    assert "summary" not in passing


def test_rejected_answer_is_not_stored():
    snap = snapshot()
    counts = precompute(snap, "m", SPECS, generate=lambda m, p: BAD)
    assert counts["rejected by check"] == 1 and "summary" not in snap["patients"][0]["results"][0]


def test_model_errors_are_counted_not_stored():
    def broken(m, p):
        raise OSError("refused")
    snap = snapshot()
    assert precompute(snap, "m", SPECS, generate=broken)["model error"] == 1


def test_old_summary_is_removed_when_the_new_answer_fails():
    snap = snapshot()
    snap["patients"][0]["results"][0]["summary"] = {"model": "old", "date": "2026-01-01", "text": GOOD}
    precompute(snap, "m", SPECS, generate=lambda m, p: BAD)
    assert "summary" not in snap["patients"][0]["results"][0]


def test_prompt_never_contains_ncqa_text():
    prompts = []
    precompute(snapshot(), "m", SPECS, generate=lambda m, p: prompts.append(p) or GOOD)
    assert prompts and all("Made-up NCQA" not in p for p in prompts)


def test_page_shows_stored_summary_without_a_model():
    result = copy.deepcopy(GAP)
    result["summary"] = {"model": "qwen2.5:7b", "date": "2026-11-22", "text": GOOD}
    out = explain(result, SPECS, model=None, public=True)
    assert out["mode"] == "precomputed" and out["model"] == "qwen2.5:7b" and out["summary_date"] == "2026-11-22"


def test_tampered_stored_summary_is_not_shown():
    result = copy.deepcopy(GAP)
    result["summary"] = {"model": "qwen2.5:7b", "date": "2026-11-22", "text": BAD}
    out = explain(result, SPECS, model=None, public=True)
    assert out["mode"] == "fallback" and "passes the check" in out["problems"][0]
