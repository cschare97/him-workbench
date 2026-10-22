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
import re
import urllib.request
from typing import Callable

from . import audit
from .icd10 import ICD10Index, format_code, normalize_code, real_words
from .matching import Patient, compare, find_duplicates
from .quality import analyze
from .medlineplus import MedlinePlusClient, MedlinePlusError
from .rxnorm import RxNormClient, RxNormError

SYSTEM_PROMPT = (
    "You are a reference assistant for Health Information Management work.\n"
    "Rules:\n"
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

_CODE_IN_TEXT = re.compile(r"\b[A-Z]\d[0-9A-Z](?:\.[0-9A-Z]{1,4})?\b")

ChatFn = Callable[[list, list], dict]


class Assistant:
    def __init__(self, chat_fn: ChatFn, icd: ICD10Index | None = None,
                 patients: list[Patient] | None = None, max_steps: int = 5,
                 on_tool: Callable[[str, dict], None] | None = None,
                 on_result: Callable[[str, dict], None] | None = None,
                 drugs: RxNormClient | None = None, education: MedlinePlusClient | None = None):
        self.chat = chat_fn
        self.drugs = drugs
        self.education = education
        self.tools = TOOLS + ([DRUG_TOOL] if drugs else []) + ([EDU_TOOL] if education else [])
        self.icd = icd
        self.patients = patients or []
        self._by_id = {p.id: p for p in self.patients}
        self.max_steps = max_steps
        self.on_tool = on_tool
        self.on_result = on_result
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.seen_codes: set[str] = set()

    # ---- tools ----
    def run_tool(self, name: str, args: dict) -> dict:
        audit.log_call("assistant_tool", {"tool": name}, "called")
        if self.on_tool:
            self.on_tool(name, args)
        try:
            if name == "search_icd10":
                if not self.icd:
                    return {"error": "ICD-10 file not loaded"}
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

    def _note_codes(self, result: dict) -> None:
        for m in _CODE_IN_TEXT.findall(json.dumps(result)):
            self.seen_codes.add(normalize_code(m))

    # ---- conversation ----
    def ask(self, question: str) -> str:
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
                self._note_codes(result)
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


def ollama_chat(model: str, host: str | None = None, timeout: int = 300) -> ChatFn:
    url = _host(host) + "/api/chat"

    def chat(messages: list, tools: list) -> dict:
        body = json.dumps({"model": model, "messages": messages, "tools": tools,
                           "stream": False, "options": {"temperature": 0}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["message"]

    return chat
