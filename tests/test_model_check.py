import json

import pytest

from him_workbench import audit
from him_workbench.icd10 import ICD10Index
from him_workbench.matching import Patient
from him_workbench.model_check import CASES, run_all, run_case


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def icd(tmp_path):
    lines = ["E119    Type 2 diabetes mellitus without complications",
             "E1122   Type 2 diabetes mellitus with diabetic chronic kidney disease",
             "E1121   Type 2 diabetes mellitus with diabetic nephropathy",
             "I10     Essential (primary) hypertension"]
    lines += [f"J{n:02d}0    Test hypertension item {n}" for n in range(12)]
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("\n".join(lines) + "\n")
    return ICD10Index.from_file(p)


PATIENTS = [Patient("1", "Robert", "Alvarado", "1980-03-14", "M", "90210"),
            Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210"),
            Patient("3", "Maria", "Okafor", "1991-11-02", "F", "60614")]


def good_model(messages, tools):
    """A fake model that does what we want: calls the right tools, then repeats their results."""
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    q = messages[last_user]["content"].lower()
    results = [(m["tool_name"], json.loads(m["content"])) for m in messages[last_user + 1:] if m["role"] == "tool"]
    if not results:
        calls = []
        if "cpt" in q:
            return {"role": "assistant", "content": "I can't look up CPT codes. I only have ICD-10-CM."}
        if "e11.9" in q:
            calls.append(("lookup_icd10", {"code": "E11.9"}))
        if "q99.99" in q:
            calls.append(("lookup_icd10", {"code": "Q99.99"}))
        if "scan" in q:
            calls.append(("scan_duplicates", {}))
        if "type 2 diabetes" in q:
            calls.append(("search_icd10", {"query": "type 2 diabetes kidney complications"}))
        if "high blood pressure" in q:
            calls.append(("search_icd10", {"query": "high blood pressure"}))
        if "99999" in q:
            calls.append(("compare_patients", {"id_a": "99999", "id_b": "11111"}))
        if "clean" in q:
            calls.append(("data_quality_report", {}))
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]}
    parts = []
    for name, r in results:
        if "error" in r:
            parts.append(f"Not found: {r['error']}.")
        elif name == "lookup_icd10":
            parts.append(f"{r['code']}: {r.get('description') or 'not a valid code in this file'}.")
        elif name == "search_icd10":
            parts.append("Codes: " + "; ".join(f"{x['code']} {x['description']}" for x in r["results"]) + ". "
                         + r.get("note", ""))
        else:
            parts.append(r["summary"])
    return {"role": "assistant", "content": " ".join(parts)}


def test_a_well_behaved_model_passes_everything(icd):
    results = run_all(good_model, icd, PATIENTS)
    failed = [(r.name, r.reason) for r in results if not r.passed]
    assert failed == []


def test_case_names_are_unique():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


def test_a_model_that_never_calls_tools_fails(icd):
    lazy = lambda messages, tools: {"role": "assistant", "content": "E11.22, and there are 5 duplicate pairs."}
    results = {r.name: r for r in run_all(lazy, icd, PATIENTS)}
    assert not results["icd_lookup"].passed and "did not call" in results["icd_lookup"].reason
    assert not results["scan_counts"].passed
    assert not results["no_such_tool"].passed  # no refusal, no invented number allowed


def test_wrong_count_is_caught(icd):
    def wrong(messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "scan_duplicates", "arguments": {}}}]}
        return {"role": "assistant", "content": "I found 99 possible duplicate pairs."}
    case = next(c for c in CASES if c.name == "scan_counts")
    r = run_case(wrong, icd, PATIENTS, case)
    assert not r.passed and "pairs found" in r.reason


def test_partial_list_shown_as_complete_is_caught(icd):
    def sloppy(messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_icd10", "arguments": {"query": "high blood pressure"}}}]}
        return {"role": "assistant", "content": "The code is I10."}
    case = next(c for c in CASES if c.name == "icd_partial_list")
    r = run_case(sloppy, icd, PATIENTS, case)
    assert not r.passed and "partial" in r.reason


def test_invented_code_is_caught_by_grounding(icd):
    def inventor(messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup_icd10", "arguments": {"code": "E11.9"}}}]}
        return {"role": "assistant", "content": "E11.9 is Type 2 diabetes mellitus without complications. Also try Z88.88."}
    case = next(c for c in CASES if c.name == "icd_lookup")
    r = run_case(inventor, icd, PATIENTS, case)
    assert not r.passed and "grounding" in r.reason


def test_model_errors_are_reported_not_raised(icd):
    def broken(messages, tools):
        raise RuntimeError("model not found")
    r = run_case(broken, icd, PATIENTS, CASES[0])
    assert not r.passed and r.reason.startswith("ERROR")
