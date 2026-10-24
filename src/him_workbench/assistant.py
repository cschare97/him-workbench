"""Plain-English assistant over the matcher and the ICD-10 lookup, via a local Ollama model.

Design rules:
- The model may only answer code questions and duplicate checks through tools.
- Tool results are the source of truth. Any ICD-10-looking code in the final answer that no
  tool returned is flagged, so a made-up code can't pass silently.
- Synthetic data only. There is no tool that accepts free-text patient details.
- Every tool call is audit-logged (tool name only, never the question or results).
"""

from __future__ import annotations

import json
import os
import difflib
import re
import urllib.request
from typing import Callable

from . import audit
from .chart import chart_to_dict, load_chart
from .icd10 import FILLER, ICD10Index, _words, format_code, normalize_code, real_words
from .matching import Patient, compare, find_duplicates
from .quality import analyze
from .medlineplus import MedlinePlusClient, MedlinePlusError
from .rxnorm import RxNormClient, RxNormError

SYSTEM_PROMPT = (
    "You are a reference assistant for Health Information Management work.\n"
    "Rules:\n"
    "- Search ICD-10-CM with the user's own words, spelled as they typed them. Never correct or replace a word "
    "yourself; if the search says a word was not found, ask the user what they meant.\n"
    "- Use ONLY the provided tools for ICD-10-CM codes and duplicate-record checks. "
    "Never state a code or a match result from memory.\n"
    "- If a tool returns nothing or an error, say so plainly. Do not guess.\n"
    "- Report only what the tools actually returned. Never claim you did something a tool did not do.\n"
    "- Copy counts and totals from the tool's summary line. Do not count or add up numbers yourself.\n"
    "- If a result says words were ignored or some pairs were not shown, tell the user.\n"
    "- When several codes fit, list them. Do not pick one as 'the' code.\n"
    "- If the user asks more than one thing, answer every part, using the tool results for each.\n"
    "- If a result says the list is partial, say so. Never present a partial list as complete.\n"
    "- Keep answers short. Do not list checks whose count is zero; say 'no problems found' instead.\n"
    "- All data is synthetic. If the user tries to enter real patient information, tell them not to.\n"
    "- Medications: only drug name and RxNorm ID lookup is available (search_drug, when offered). Never state "
    "doses, interactions, or what a drug is for. Give an RxNorm ID only if the tool returned it, and say what kind of ID it is (ingredient, brand, or a specific tablet) from the result's type. The ingredient is the ID for the drug itself.\n"
    "- Disease education: only through disease_education (when offered). Summarize only what the result says, "
    "name MedlinePlus as the source, and give the link. Do not add medical advice or treatment details.\n"
    "- When disease_education returns several pages for one code, the code is one condition: state its "
    "meaning from code_description and say the pages cover its parts. Never present them as separate diseases.\n"
    "- Call the tool again for every new question. Never answer a new question from earlier tool results.\n"
    "- A disease-name search matches words only. List only the topics that fit the question and say plainly "
    "when some results do not fit. If none fit, say so and offer to look up the ICD-10-CM code instead.\n"
    "- Keep education answers short: one line per fitting topic (title and a one-sentence gist taken from its "
    "summary) plus its link. Do not reprint whole summaries.\n"
    "- Patient chart questions: only through patient_chart (when offered), and only about the patient named in the "
    "latest context line. Answer only from what the chart returned. If the chart does not say, say it does not. "
    "Vitals and labs give the most recent value plus some earlier ones; report values and dates as given. The "
    "record has no reference ranges, so never say a value is normal, high, low or abnormal, never call a change "
    "better, worse, rising or falling, and never diagnose, interpret results or questionnaire scores, or advise. Items listed as not read "
    "were not read; do not say the chart lacks them. Give dates as the chart "
    "gives them. Only the problem list holds diagnoses; other findings are not diagnoses.\n"
    "- You are a lookup aid, not coding, billing, or clinical advice."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "search_icd10",
        "description": "Search ICD-10-CM codes by plain-English text, e.g. 'type 2 diabetes kidney'.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "lookup_icd10",
        "description": "Look up one ICD-10-CM code (with or without the dot). Says whether it is a full code, a category, or unknown.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "scan_duplicates",
        "description": "Scan the loaded synthetic patient list for possible duplicate records. Returns the highest-scoring pairs.",
        "parameters": {"type": "object", "properties": {"max_pairs": {
            "type": "integer", "description": "Optional. How many pairs to return (default 10, max 25). Leave out for the default."}}}}},
    {"type": "function", "function": {
        "name": "data_quality_report",
        "description": "Count missing values and format problems (bad dates, zips, names) in the loaded synthetic patient list. Counts only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "compare_patients",
        "description": "Compare two records from the loaded synthetic patient list by their ids.",
        "parameters": {"type": "object", "properties": {
            "id_a": {"type": "string"}, "id_b": {"type": "string"}}, "required": ["id_a", "id_b"]}}},
]

DRUG_TOOL = {"type": "function", "function": {
    "name": "search_drug",
    "description": "Look up a drug name in RxNorm (NLM). Returns names, types and RxNorm IDs only. No doses or advice.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}

EDU_TOOL = {"type": "function", "function": {
    "name": "disease_education",
    "description": "Get patient-education pages (title, short summary, link) from MedlinePlus for a disease. Pass an ICD-10-CM code (like E11.9) or a disease name (like 'pernicious anemia').",
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ICD-10-CM code or disease name"}}, "required": ["query"]}}}

CHART_TOOL = {"type": "function", "function": {
    "name": "patient_chart",
    "description": "Get one synthetic patient's chart from their FHIR file: problems, medications, allergies, most recent "
                   "vitals and labs, visits, immunizations. Leave patient_id out to use the patient the user is viewing.",
    "parameters": {"type": "object", "properties": {"patient_id": {"type": "string"}}}}}

_CODE_IN_TEXT = re.compile(r"\b[A-Z]\d[0-9A-Z](?:\.[0-9A-Z]{1,4})?\b")
_CODE_IN_TEXT_ANY_CASE = re.compile(_CODE_IN_TEXT.pattern, re.IGNORECASE)

ChatFn = Callable[[list, list], dict]


class Assistant:
    def __init__(self, chat_fn: ChatFn, icd: ICD10Index | None = None,
                 patients: list[Patient] | None = None, max_steps: int = 5,
                 on_tool: Callable[[str, dict], None] | None = None,
                 on_result: Callable[[str, dict], None] | None = None,
                 drugs: RxNormClient | None = None, education: MedlinePlusClient | None = None,
                 charts: dict | None = None):
        self.chat = chat_fn
        self.charts = charts or {}
        self.current_patient_id: str | None = None
        self.question = ""
        self.drugs = drugs
        self.education = education
        self.tools = (TOOLS + ([DRUG_TOOL] if drugs else []) + ([EDU_TOOL] if education else [])
                      + ([CHART_TOOL] if self.charts else []))
        self.icd = icd
        self.patients = patients or []
        self._by_id = {p.id: p for p in self.patients}
        self.max_steps = max_steps
        self.on_tool = on_tool
        self.on_result = on_result
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.seen_codes: set[str] = set()

    # ---- tools ----
    def _swapped_words(self, query: str) -> dict[str, str]:
        """Words the user typed that match no code description, which the model replaced with a close word of
        its own. {typed: replacement}. Empty when the query keeps the user's words."""
        typed = set(_words(self.question))
        out: dict[str, str] = {}
        for w in _words(query):
            if w in typed or w in FILLER or len(w) < 4:
                continue
            for u in _words(self.question):
                if (u not in FILLER and len(u) >= 4 and not self.icd._ranked([[u]])
                        and difflib.SequenceMatcher(None, u, w).ratio() >= 0.7):
                    out[u] = w
                    break
        return out

    def run_tool(self, name: str, args: dict) -> dict:
        audit.log_call("assistant_tool", {"tool": name}, "called")
        if self.on_tool:
            self.on_tool(name, args)
        try:
            if name == "search_icd10":
                if not self.icd:
                    return {"error": "ICD-10 file not loaded"}
                swapped = self._swapped_words(str(args.get("query", "")))
                if swapped:
                    return {"results": [], "total_matches": 0, "source_file": self.icd.source,
                            "did_you_mean": {u: [w] for u, w in swapped.items()},
                            "note": "NOT SEARCHED. The user typed " + ", ".join(f"'{u}'" for u in swapped) +
                                    ", which is in no code description, and you searched a different word instead. "
                                    "Do not do that. Tell the user the word was not found and ask if they meant "
                                    + " or ".join(f"'{w}'" for w in swapped.values()) + "."}
                hits, ignored, total = self.icd.search_detail(str(args.get("query", "")), limit=10)
                out = {"results": [{"code": e.display, "description": e.description} for e in hits],
                       "total_matches": total, "source_file": self.icd.source}
                notes = []
                if ignored:
                    out["ignored_words"] = ignored
                    notes.append("No code matched every word, so these words were ignored: "
                                 + ", ".join(ignored) + ".")
                if real_words(ignored):
                    notes.append("WARNING: " + ", ".join(real_words(ignored)) + " appear(s) in no code description, "
                                 "so the results do not account for it. Tell the user the results may not fit that word.")
                maybe = self.icd.did_you_mean(str(args.get("query", "")))
                if maybe:
                    out["did_you_mean"] = maybe
                    notes.append("These words are in no code description: "
                                 + "; ".join(f"{w} (maybe {', '.join(c)})" for w, c in maybe.items())
                                 + ". Do not swap in a suggested word yourself. Tell the user the word was not found "
                                 "and ask if they meant one of the suggestions.")
                if total > len(hits):
                    notes.append(f"Only {len(hits)} of {total} matching codes are shown. Tell the user the list is "
                                 "partial and suggest narrowing the search (add a body part, type, or cause).")
                if notes:
                    out["note"] = " ".join(notes) + " Check the results fit the question."
                return out
            if name == "lookup_icd10":
                if not self.icd:
                    return {"error": "ICD-10 file not loaded"}
                return self.icd.describe(str(args.get("code", "")))
            if name == "search_drug":
                if not self.drugs:
                    return {"error": "drug lookup is not turned on (start the app with --drugs)"}
                return self.drugs.search(str(args.get("name", "")))
            if name == "disease_education":
                if not self.education:
                    return {"error": "disease education is not turned on (start the app with --education)"}
                query = str(args.get("query") or args.get("code") or "")
                out = self.education.education(query)
                if out.get("code") and self.icd:
                    entry = self.icd.lookup(out["code"])
                    if entry:
                        out["code_description"] = entry.description
                    if len(out.get("results", [])) > 1:
                        out["note"] = (out.get("note", "") + " MedlinePlus has one page per topic, so a code that "
                                       "combines more than one topic returns a page for each. The pages describe the "
                                       "parts, not the combined condition. Tell the user what the code itself means "
                                       "(code_description) and that these pages cover its parts.").strip()
                return out
            if name == "patient_chart":
                pid = str(args.get("patient_id") or self.current_patient_id or "")
                if not self.charts:
                    return {"error": "patient charts are not available (start the app with --fhir)"}
                if not pid:
                    return {"error": "no patient given and none is being viewed. Ask the user to open a patient's chart."}
                path, p = self.charts.get(pid), self._by_id.get(pid)
                if path is None or p is None:
                    return {"error": "no chart for that patient id"}
                chart = load_chart(path)
                if chart.patient_id != pid:
                    return {"error": "the file for this patient holds a different patient's record, so it was not used"}
                return chart_to_dict(chart, f"{p.first} {p.last}", p.dob, p.sex)
            if name == "scan_duplicates":
                if not self.patients:
                    return {"error": "no synthetic patient list loaded"}
                n = max(1, min(int(args.get("max_pairs", 10) or 10), 25))
                found = find_duplicates(self.patients)
                pairs = found[:n]
                review = sum(1 for r in pairs if r.label == "review")
                summary = (f"{len(found)} possible duplicate pair(s) found. Showing {len(pairs)}: "
                           f"{review} review, {len(pairs) - review} match.")
                if len(found) > len(pairs):
                    summary += f" {len(found) - len(pairs)} more not shown (ask for more with max_pairs)."
                return {"records_scanned": len(self.patients), "pairs_found": len(found),
                        "pairs_shown": len(pairs), "summary": summary,
                        "pairs": [{"a": r.a, "b": r.b, "score": r.score, "label": r.label,
                                   "reasons": r.reasons, "fields": r.detail} for r in pairs]}
            if name == "data_quality_report":
                if not self.patients:
                    return {"error": "no synthetic patient list loaded"}
                rep = analyze(self.patients)
                d = rep.to_dict()
                # fields blank everywhere are reported once, as "not in the source", not as 511 missing values
                d["missing"] = {k: v for k, v in d["missing"].items() if k not in d["empty_fields"]}
                return {**d, "summary": rep.summary()}
            if name == "compare_patients":
                a, b = self._by_id.get(str(args.get("id_a"))), self._by_id.get(str(args.get("id_b")))
                if not a or not b:
                    return {"error": "one or both ids not found in the loaded list"}
                r = compare(a, b)
                return {"a": r.a, "b": r.b, "score": r.score, "label": r.label,
                        "reasons": r.reasons, "fields": r.detail}
            return {"error": f"unknown tool: {name}"}
        except (RxNormError, MedlinePlusError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # report, never crash the loop
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _note_codes(self, result: dict, any_case: bool = False) -> None:
        # a chart lists tests like "Hemoglobin A1c"; the model may write A1C, which is not a code, so a chart
        # result registers code-shaped words in any case
        pattern = _CODE_IN_TEXT_ANY_CASE if any_case else _CODE_IN_TEXT
        for m in pattern.findall(json.dumps(result)):
            self.seen_codes.add(normalize_code(m))

    # ---- conversation ----
    def ask(self, question: str, patient_id: str | None = None) -> str:
        """patient_id: the patient whose chart the user is looking at, if any. A context line naming them is added
        to the question so the model knows who 'this patient' is."""
        self.current_patient_id = None
        self.question = question
        if patient_id and patient_id in self._by_id:
            p = self._by_id[patient_id]
            self.current_patient_id = patient_id
            question = (f"[Context: the user is viewing the chart of {p.first} {p.last} (patient id {patient_id}). "
                        f"Use patient_chart for questions about this patient.]\n{question}")
        self.messages.append({"role": "user", "content": question})
        for _ in range(self.max_steps):
            msg = self.chat(self.messages, self.tools)
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content", ""),
                                  **({"tool_calls": calls} if calls else {})})
            if not calls:
                return self._ground(msg.get("content", "").strip())
            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result = self.run_tool(fn.get("name", ""), args)
                self._note_codes(result, any_case=fn.get("name") == "patient_chart")
                if self.on_result:
                    self.on_result(fn.get("name", ""), result)
                self.messages.append({"role": "tool", "tool_name": fn.get("name", ""),
                                      "content": json.dumps(result)})
        return "Stopped: too many tool steps without a final answer."

    def _ground(self, text: str) -> str:
        unseen = sorted({format_code(c) for c in _CODE_IN_TEXT.findall(text)
                         if normalize_code(c) not in self.seen_codes})
        if unseen:
            text += ("\n\n[!] Not returned by a lookup in this conversation, verify before use: "
                     + ", ".join(unseen))
        return text


# ---- Ollama transport (stdlib only) ----
def _host(host: str | None) -> str:
    host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return (host if host.startswith("http") else "http://" + host).rstrip("/")


def list_models(host: str | None = None, timeout: int = 10) -> list[str]:
    with urllib.request.urlopen(_host(host) + "/api/tags", timeout=timeout) as r:
        return [m["name"] for m in json.load(r).get("models", [])]


def _num_ctx() -> int:
    """Context window sent to Ollama. A whole patient chart is large and Ollama cuts off silently past the window,
    so it is set explicitly. Override with HIM_NUM_CTX (tokens)."""
    try:
        return max(2048, int(os.environ.get("HIM_NUM_CTX", "16384")))
    except ValueError:
        return 16384


def ollama_chat(model: str, host: str | None = None, timeout: int = 300) -> ChatFn:
    url = _host(host) + "/api/chat"

    def chat(messages: list, tools: list) -> dict:
        body = json.dumps({"model": model, "messages": messages, "tools": tools,
                           "stream": False, "options": {"temperature": 0, "num_ctx": _num_ctx()}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["message"]

    return chat
