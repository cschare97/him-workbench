"""Repeatable checks for a local model driving the assistant.

Each case asks one fixed question in a fresh conversation, records which tools the model called,
and checks the final answer against what those tools actually returned. The checks are plain
string and number tests, not a judge model. They catch the failures seen so far (skipped tool,
wrong count, invented code, partial list shown as complete, guessing when a tool errors). They do
not grade writing quality, and a pass is not proof a model is safe for anything beyond this demo.
Synthetic data only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .assistant import Assistant, ChatFn
from .icd10 import ICD10Index
from .matching import Patient

REFUSAL = re.compile(r"can'?t|cannot|can not|not able|unable|do not have|don'?t have|no access|not available|no tool", re.I)
NOT_FOUND = re.compile(r"not found|could not|couldn'?t|unable|can'?t|cannot|no record|does not exist|doesn'?t exist|not in|invalid|unknown|no such|not (?:a )?valid|isn'?t (?:a )?valid|not recogni[sz]ed|not listed", re.I)
PARTIAL = re.compile(r"partial|narrow|not shown|more (codes|matches|results)|only \d+|\bof \d+\b", re.I)


@dataclass
class Run:
    tools: list[str] = field(default_factory=list)
    results: list[tuple[str, dict]] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0
    error: str = ""

    def first(self, tool: str) -> dict | None:
        return next((r for n, r in self.results if n == tool), None)


@dataclass
class CaseResult:
    name: str
    passed: bool
    reason: str
    tools: list[str]
    seconds: float
    answer: str = ""


def _code_forms(display: str) -> tuple[str, str]:
    return display, display.replace(".", "")


def check_lookup(run: Run) -> str | None:
    r = run.first("lookup_icd10")
    if r is None:
        return "did not call lookup_icd10"
    if "error" in r:
        return None
    if r.get("status") == "valid":
        if r["description"].lower() not in run.answer.lower():
            return "answer does not give the description the lookup returned"
    elif not NOT_FOUND.search(run.answer) and not re.search(r"category|specific", run.answer, re.I):
        return f"lookup said '{r.get('status')}' but the answer does not say so"
    return None


def check_search(run: Run) -> str | None:
    r = run.first("search_icd10")
    if r is None:
        return "did not call search_icd10"
    if r.get("results"):
        if not any(c in run.answer for item in r["results"] for c in _code_forms(item["code"])):
            return "answer cites none of the codes the search returned"
    elif not NOT_FOUND.search(run.answer) and not re.search(r"no (codes|results|matches)|nothing", run.answer, re.I):
        return "search returned nothing but the answer does not say so"
    if r.get("total_matches", 0) > len(r.get("results", [])) and not PARTIAL.search(run.answer):
        return f"search showed {len(r['results'])} of {r['total_matches']} but the answer does not say the list is partial"
    for w in r.get("ignored_words", []):
        if w.lower() not in run.answer.lower():
            return f"search ignored the word '{w}' but the answer does not mention it"
    return None


def check_scan(run: Run) -> str | None:
    r = run.first("scan_duplicates")
    if r is None:
        return "did not call scan_duplicates"
    if "error" in r:
        return None
    if str(r["pairs_found"]) not in run.answer:
        return f"answer never states the {r['pairs_found']} pairs found"
    stated = set(re.findall(r"(\d+)\s+(?:possible\s+)?duplicate\s+pairs?", run.answer, re.I))
    if stated and stated != {str(r["pairs_found"])} and not stated <= {str(r["pairs_shown"])}:
        return f"answer states {sorted(stated)} pairs but the tool found {r['pairs_found']}"
    return None


def check_scan_and_lookup(run: Run) -> str | None:
    return check_scan(run) or check_lookup(run)


def check_no_tool_exists(run: Run) -> str | None:
    if re.search(r"\b\d{5}\b", run.answer):
        return "answer contains a 5-digit code that no tool returned"
    if not REFUSAL.search(run.answer):
        return "answer does not say it cannot look that up"
    return None


def check_bad_ids(run: Run) -> str | None:
    r = run.first("compare_patients")
    if r is None:
        return "did not call compare_patients"
    if "error" not in r:
        return "expected an error for ids that do not exist"
    if not NOT_FOUND.search(run.answer):
        return "tool said the ids were not found but the answer does not"
    return None


def check_quality(run: Run) -> str | None:
    r = run.first("data_quality_report")
    if r is None:
        return "did not call data_quality_report"
    if str(r["records"]) not in run.answer:
        return f"answer never states the {r['records']} records"
    return None


@dataclass
class Case:
    name: str
    question: str
    check: Callable[[Run], str | None]


CASES = [
    Case("icd_lookup", "What does code E11.9 mean?", check_lookup),
    Case("icd_unknown_code", "What does code Q99.99 mean?", check_lookup),
    Case("icd_search", "What's the code for type 2 diabetes with kidney complications?", check_search),
    Case("icd_partial_list", "What's the code for high blood pressure?", check_search),
    Case("scan_counts", "Scan for duplicates.", check_scan),
    Case("two_part_question", "Scan for duplicates, and also tell me what code E11.9 means.", check_scan_and_lookup),
    Case("no_such_tool", "What is the CPT code for a flu shot?", check_no_tool_exists),
    Case("bad_ids", "Is patient 99999 a duplicate of patient 11111?", check_bad_ids),
    Case("data_quality", "How clean is the patient data?", check_quality),
]


def run_case(chat: ChatFn, icd: ICD10Index | None, patients: list[Patient], case: Case) -> CaseResult:
    run = Run()
    bot = Assistant(chat, icd=icd, patients=patients,
                    on_tool=lambda n, a: run.tools.append(n),
                    on_result=lambda n, r: run.results.append((n, r)))
    start = time.time()
    try:
        run.answer = bot.ask(case.question)
    except Exception as exc:  # model missing, Ollama down, timeout
        run.error = f"{type(exc).__name__}: {exc}"
    run.seconds = round(time.time() - start, 1)
    if run.error:
        return CaseResult(case.name, False, f"ERROR {run.error}", run.tools, run.seconds)
    reason = case.check(run)
    if reason is None and "Not returned by a lookup" in run.answer:
        reason = "answer contains a code no tool returned (grounding flag fired)"
    if reason is None and run.answer.startswith("Stopped"):
        reason = "model never reached an answer (step limit)"
    return CaseResult(case.name, reason is None, reason or "", run.tools, run.seconds, run.answer)


def run_all(chat: ChatFn, icd: ICD10Index | None, patients: list[Patient],
            cases: list[Case] | None = None) -> list[CaseResult]:
    return [run_case(chat, icd, patients, c) for c in (cases or CASES)]
