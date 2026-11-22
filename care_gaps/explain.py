"""Explain why a patient has a care gap, citing the official spec and the patient's own evidence.

How it works:
  1. Pick the spec sections that apply, by rule, from the measure and the result
     (for example, a missing A1c points at CMS122's Numerator, which says a missing result
     counts as failing). Rules, not embeddings: there are only a few dozen sections, and
     the right one is known from the result.
  2. Build a prompt from the result, its evidence [E1, E2, ...] and those sections
     [S1, S2, ...], and ask a local model for a short explanation that cites them.
  3. Check the answer before showing it (check_grounding). Every citation must exist, and
     every number in the answer must appear in the evidence or the cited sections. If the
     check fails, or no model is available, show the deterministic explanation instead.

Spec text is only used as each steward's terms allow (see spec_loader.py): in public mode,
text for measures outside PUBLIC_TEXT_OK is never shown or sent to a model; the section
is linked instead.

Usage (from the repo root):
    python3 care_gaps/explain.py --patient "Carlos Wolf" --measure CMS122
    python3 care_gaps/explain.py --patient "Carlos Wolf" --measure CMS122 --model qwen2.5:7b
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from spec_loader import PUBLIC_TEXT_OK, load as load_specs

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "data" / "care_gaps.json"

KIND_LABELS = {"care": "Care gap", "documentation": "Documentation gap", "open order": "Open order"}
NEXT_STEP = {
    "care": "Reach out to the patient to schedule the missing test or exam.",
    "care_out_of_range": "Review the out-of-range result with the care team.",
    "documentation": "Review the chart, find the outside result or report, and enter it as structured data so the measure can see it.",
    "open order": "Follow up on the open order with the lab or the patient so a result comes back.",
}


# Facts the model needs so it doesn't misread the result. Stated by the code, not inferred.
WHY_IT_COUNTS = {
    "documentation": "The measure only sees structured results. A value written in a note is invisible "
                     "to the measure until someone enters it as a structured result, so this patient "
                     "currently counts as a gap even though the test may have been done.",
    "open order": "An order with no result does not count. Only a resulted test counts.",
    "care": "",
}


def outcome(result):
    """The result in plain words. Avoids 'met', which means failing on the inverse CMS122."""
    if result["status"] == "not in denominator":
        return "This patient is not counted by this measure."
    if result["status"] == "excluded":
        return "This patient is excluded from this measure."
    if result.get("gap"):
        return "This patient currently has a care gap on this measure (counts against the clinic)."
    return "This patient currently passes this measure."


def next_step(result):
    """Suggested next step for a gap. A care gap is either missing care or an out-of-range result."""
    kind = result.get("kind", "")
    if kind == "care" and "above" in result.get("reason", ""):
        return NEXT_STEP["care_out_of_range"]
    return NEXT_STEP.get(kind, "")


def _sentence(text):
    return text[:1].upper() + text[1:] if text else text


def measure_key(measure_name):
    return measure_name.split()[0]


def select_sections(result):
    """Spec section names that explain this result, most relevant first."""
    key, status, kind = measure_key(result["measure"]), result["status"], result.get("kind", "")
    if status == "not in denominator":
        return ["Initial Population"]
    if status == "excluded":
        return ["Denominator Exclusions"]
    sections = ["Numerator"]
    if kind == "documentation" or key == "CMS951":
        sections.append("Guidance")  # CMS122: results in the record count; CMS131: who may do the exam; CMS951: uACR options
    return sections


def cited_sections(result, specs, public):
    """[{id, name, text, url, notice}] for the selected sections. text is None when it can't be shown."""
    key = measure_key(result["measure"])
    spec = (specs or {}).get(key)
    out = []
    for n, name in enumerate(select_sections(result), start=1):
        if spec is None:
            text, url, notice = None, None, None
        else:
            allowed = not public or key in PUBLIC_TEXT_OK
            text = spec["sections"].get(name) if allowed else None
            url = spec["url"]
            # Both fields: NCQA puts its terms in Copyright, NKF puts them in Disclaimer.
            notice = "\n\n".join(x for x in (spec["meta"].get("Copyright", ""), spec["meta"].get("Disclaimer", "")) if x)
        out.append({"id": f"S{n}", "name": name, "measure": key, "text": text, "url": url, "notice": notice})
    return out


def fallback_text(result):
    """Deterministic explanation built only from the result itself. Always available."""
    kind = result.get("kind", "")
    lines = [f"{result['measure']}: {result['reason']}."]
    if result.get("gap") and kind:
        lines.append(f"Gap type: {KIND_LABELS.get(kind, kind)}. Suggested next step: {next_step(result)}")
    return " ".join(lines)


def project_rules(result):
    """This project's own statements about the result, tagged [R#] so the model can cite them
    without borrowing a spec tag. [S#] stays reserved for the stewards' published words."""
    kind = result.get("kind", "")
    rules = [("Measure logic result", f"{outcome(result)} {_sentence(result['reason'])}.")]
    if WHY_IT_COUNTS.get(kind):
        rules.append(("Why it counts this way", WHY_IT_COUNTS[kind]))
    if result.get("gap") and next_step(result):
        rules.append(("Suggested next step", next_step(result)))
    return [{"id": f"R{n}", "label": label, "text": text} for n, (label, text) in enumerate(rules, start=1)]


def build_prompt(result, evidence, sections, rules=None):
    ev_lines = [f"[{e['id']}] {e['label']}" + (f" | date {e['date']}" if e.get("date") else "")
                + (f" | value {e['value']}" if e.get("value") is not None else "")
                + (f" | text: {e['text']}" if e.get("text") else "") for e in evidence]
    sec_lines = [f"[{s['id']}] {s['measure']} {s['name']}: {s['text']}" for s in sections if s["text"]]
    rules = project_rules(result) if rules is None else rules
    rule_lines = [f"[{r['id']}] {r['label']}: {r['text']}" for r in rules]
    return "\n".join([
        "You explain quality-measure results to a clinic care team. Synthetic data, not a real patient.",
        "Write 3 to 5 short sentences in plain language.",
        "Rules:",
        "- Use only the facts below. Do not add anything from memory.",
        "- End every sentence that states a fact with its sources in brackets.",
        "- Tags: [E#] for this patient's facts, [R#] for this project's rules and next steps,",
        "  [S#] only for what the official spec text below actually says. No other bracketed labels.",
        "- Copy dates and numbers exactly as written below. Do not calculate new numbers.",
        "- Do not say the measure is 'met'. Say whether the patient has a gap or passes.",
        "- Do not compare values to thresholds yourself or give medical advice.",
        "",
        "Project rules:", *rule_lines,
        "",
        "Evidence:", *ev_lines,
        "",
        "Spec sections:", *(sec_lines or ["(not available; rely on the reason above)"]),
    ])


_BRACKET = re.compile(r"\[([^\[\]]{1,60})\]")
_ID = re.compile(r"^[ESR]\d+$")
_CITE = _BRACKET  # kept for number stripping
_MET = re.compile(r"\b(is|was|are) met\b", re.IGNORECASE)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text):
    """Number tokens, normalized so 07 == 7 and 6.10 == 6.1."""
    out = set()
    for tok in _NUM.findall(text):
        out.add(str(float(tok)) if "." in tok else str(int(tok)))
    return out


def check_grounding(answer, result, evidence, sections, rules=None):
    """Problems with a model answer, or [] if it passes. Strict on purpose: a rejected answer
    falls back to the deterministic explanation, which is always correct."""
    problems = []
    rules = project_rules(result) if rules is None else rules
    known = {e["id"] for e in evidence} | {s["id"] for s in sections if s["text"]} | {r["id"] for r in rules}
    tags = [t.strip() for group in _BRACKET.findall(answer) for t in re.split(r"[,;]", group) if t.strip()]
    cites = {t for t in tags if _ID.match(t)}
    bad_tags = sorted({t for t in tags if not _ID.match(t)})
    if bad_tags:
        problems.append("uses labels that aren't evidence or spec ids: " + ", ".join(bad_tags))
    if not cites:
        problems.append("no citations")
    unknown = sorted(cites - known)
    if unknown:
        problems.append("cites sources that don't exist: " + ", ".join(unknown))
    if result.get("gap") and _MET.search(answer):
        problems.append("says the measure is 'met', which reads as passing; this patient has a gap")
    allowed = (_numbers(json.dumps(evidence)) | _numbers(result["reason"]) | _numbers(result["measure"])
               | _numbers(json.dumps(rules)))
    for s in sections:
        if s["text"]:
            allowed |= _numbers(s["text"])
    stray = sorted(_numbers(_CITE.sub("", answer)) - allowed, key=float)
    if stray:
        problems.append("numbers not in the evidence: " + ", ".join(stray))
    return problems


def ollama_generate(model, prompt, host=None, timeout=120):
    """One non-streaming completion from a local Ollama server."""
    host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0}}).encode()
    request = urllib.request.Request(host + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: local Ollama host
        return json.loads(response.read())["response"].strip()


def explain(result, specs=None, model=None, public=False, generate=ollama_generate):
    """Explanation for one result. Never raises for model trouble: it falls back and says why."""
    evidence = result.get("evidence", [])
    sections = cited_sections(result, specs, public)
    rules = project_rules(result)
    out = {"measure": result["measure"], "status": result["status"], "kind": result.get("kind", ""),
           "mode": "fallback", "text": fallback_text(result), "problems": [],
           "evidence": evidence, "sections": sections, "rules": rules}
    if not model:
        out["problems"].append("no model selected")
        return out
    try:
        answer = generate(model, build_prompt(result, evidence, sections, rules))
    except Exception as exc:  # network, model missing, bad JSON: all mean "use the fallback"
        out["problems"].append(f"model unavailable: {exc}")
        return out
    problems = check_grounding(answer, result, evidence, sections, rules)
    if problems:
        out["problems"] = problems
        out["rejected_answer"] = answer
        return out
    out.update(mode="model", text=answer, model=model)
    return out


_LOAD = object()


def page_data(snapshot, pid, measure, model=None, public=False, generate=ollama_generate, specs=_LOAD):
    """Everything the explain page shows, for one patient and measure, or None if not found."""
    patient = next((p for p in snapshot.get("patients", []) if p["id"] == pid), None)
    if patient is None:
        return None
    result = next((r for r in patient["results"] if measure_key(r["measure"]) == measure), None)
    if result is None:
        return None
    out = explain(result, load_specs() if specs is _LOAD else specs, model, public, generate)
    out.update(patient_id=patient["id"], patient_name=patient["name"], scenario=patient.get("scenario", ""),
               outcome=outcome(result), builtin=fallback_text(result), gap=result.get("gap", False),
               kind_label=KIND_LABELS.get(result.get("kind", ""), ""), public=public)
    return out


def find_result(snapshot, patient, measure):
    patient = patient.lower()
    for p in snapshot["patients"]:
        if patient in (p["id"].lower(), ) or patient in p["name"].lower():
            for r in p["results"]:
                if measure_key(r["measure"]) == measure.upper():
                    return p, r
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--patient", required=True, help="patient id, or part of the name")
    parser.add_argument("--measure", required=True, choices=["CMS122", "CMS131", "CMS951"])
    parser.add_argument("--model", default="", help="Ollama model, e.g. qwen2.5:7b; omit for the built-in explanation")
    parser.add_argument("--public", action="store_true", help="apply public-site text rules")
    args = parser.parse_args()

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    patient, result = find_result(snapshot, args.patient, args.measure)
    if result is None:
        sys.exit(f"No {args.measure} result for a patient matching '{args.patient}'")
    out = explain(result, load_specs(), args.model or None, args.public)

    print(f"{patient['name']} | {out['measure']} | {out['status']}" + (f" | {KIND_LABELS[out['kind']]}" if out["kind"] else ""))
    print(f"\nWhat the measure logic found:\n  {fallback_text(result)}")
    if out["mode"] == "model":
        print(f"\nPlain-language summary by {out['model']} (passed the grounding check):\n  {out['text']}")
    elif args.model:
        print(f"\nNo summary shown. The {args.model} answer was not used: {'; '.join(out['problems'])}")
        if out.get("rejected_answer"):
            print(f"Rejected answer:\n  {out['rejected_answer']}")
    print()
    print("Project rules:")
    for r in out["rules"]:
        print(f"  [{r['id']}] {r['label']}: {r['text']}")
    print("Evidence:")
    for e in out["evidence"]:
        detail = " | ".join(str(x) for x in (e["date"], e["value"], e["text"]) if x not in (None, ""))
        print(f"  [{e['id']}] {e['label']}" + (f": {detail}" if detail else ""))
    print("Spec sections:")
    for s in out["sections"]:
        print(f"  [{s['id']}] {s['measure']} {s['name']}" + ("" if s["text"] else "  (not shown; see source)"))
        if s["text"]:
            print("      " + s["text"].replace("\n", "\n      "))
        if s["url"]:
            print(f"      Source: {s['url']}")
    notices = {s["notice"] for s in out["sections"] if s["text"] and s["notice"]}
    for notice in notices:
        print(f"\n{notice}")


if __name__ == "__main__":
    main()
