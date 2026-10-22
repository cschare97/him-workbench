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
