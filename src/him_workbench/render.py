"""Plain-text rendering of tool results, shared by the terminal assistant and the web UI.

The raw tool result is the source of truth; these lines show it without the model's retelling.
"""

from __future__ import annotations


def _pair_line(x: dict) -> list[str]:
    f = " ".join(f"{k}={v}" for k, v in x["fields"].items())
    lines = [f"{x['a'][:12]} ~ {x['b'][:16]}  score={x['score']}  {x['label'].upper()}  [{f}]"]
    lines += [f"    held for review: {why}" for why in x.get("reasons", [])]
    return lines


def result_lines(name: str, r: dict) -> tuple[str, list[str]]:
    """Return (headline, detail_lines) for one tool result."""
    if "error" in r:
        return f"ERROR: {r['error']}", []
    if name == "search_icd10":
        shown, total = len(r["results"]), r.get("total_matches", len(r["results"]))
        count = f"{shown} of {total}" if total > shown else f"{shown}"
        details = []
        if r.get("ignored_words"):
            details.append(f"(no code matched every word; ignored: {', '.join(r['ignored_words'])})")
        details += [f"{x['code']:9} {x['description']}" for x in r["results"]]
        return f"{count} code(s) from {r.get('source_file')}", details
    if name == "search_hcpcs":
        shown, total = len(r["results"]), r.get("total_matches", len(r["results"]))
        count = f"{shown} of {total}" if total > shown else f"{shown}"
        details = []
        if r.get("ignored_words"):
            details.append(f"(no code matched every word; ignored: {', '.join(r['ignored_words'])})")
        details += [f"{x['code']:9} {x['description']}" for x in r["results"]]
        return f"{count} code(s) from {r.get('source_file')}", details
    if name == "lookup_hcpcs":
        return f"{r.get('status')}: {r.get('code')} {r.get('description', '')}".rstrip(), []
    if name == "search_drug":
        shown, total = len(r["results"]), r.get("total_matches", len(r["results"]))
        count = f"{shown} of {total}" if total > shown else f"{shown}"
        details = [f"(closest matches, not an exact name match)"] if r.get("match") == "approximate" else []
        details += [f"{x['rxcui']:9} {x.get('type_label', x['type']):22} {x['name']}" for x in r["results"]]
        return f"{count} drug concept(s) from {r.get('source')}", details
    if name == "disease_education":
        pages = r["results"]
        details = []
        for x in pages:
            details += [x["title"], f"    {x['summary']}", f"    {x['url']}"]
            if x.get("credit"):
                details.append(f"    ({x['credit']})")
        total = r.get("total_matches", len(pages))
        count = f"{len(pages)} of {total}" if total > len(pages) else f"{len(pages)}"
        if r.get("code_description"):
            details.insert(0, f"{r['code']} means: {r['code_description']}")
        return f"{count} MedlinePlus page(s) for {r.get('code') or r.get('term')}", details or [r.get("note", "")]
    if name == "patient_chart":
        who = r["patient"]["name"] or r["patient"]["id"]
        details = [f"active: {x['name']} [{x['code']}]" for x in r["active_problems"]]
        details += [f"past: {x['name']} [{x['code']}] {x['from']} to {x['to']}".rstrip() for x in r["past_problems"]]
        details += [f"medication: {x['name']} {x['status']} {x['date']}" for x in r["medications"]]
        details += [f"{x['name']}: {x['value']} ({x['date']})" + (f"  [+{len(x['earlier'])} earlier]" if x.get("earlier") else "")
                    for x in r["vitals_most_recent"] + r["labs_most_recent"]
                    + r.get("social_history_observations", []) + r.get("screening_questionnaires", [])]
        details += [f"visit {x['date']}: {x['type']}" for x in r["visits"][:10]]
        return f"Chart for {who}: {r['summary']}", details
    if name == "lookup_icd10":
        return f"{r.get('status')}: {r.get('code')} {r.get('description', r.get('note', ''))}", []
    if name == "data_quality_report":
        return r["summary"], []
    if name == "scan_duplicates":
        details = [line for x in r["pairs"] for line in _pair_line(x)]
        return f"{r['summary']} ({r['records_scanned']} records scanned)", details
    if name == "compare_patients":
        return "pair compared", _pair_line(r)
    return "(no display for this tool)", []
