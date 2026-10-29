"""HCPCS Level II code lookup from the public CMS alpha-numeric HCPCS file.

HCPCS Level II covers products, supplies and services not in CPT (ambulance, DME, drugs
administered other than by mouth, etc.) — a letter (A-V) followed by 4 digits, e.g. E0100.
It is maintained by CMS and is NOT the same thing as CPT/HCPCS Level I, which is AMA-licensed
and deliberately not included here (see scripts/fetch_hcpcs.sh).

The code set is NOT bundled: download it with scripts/fetch_hcpcs.sh. CMS distributes the
alpha-numeric file as a quarterly update; the exact internal column layout has changed between
releases (fixed-width in older years, delimited in newer ones), so from_file() below detects the
layout rather than assuming one. If a real CMS file fails to load, the error message says why —
that is a parser gap to fix, not a sign the file is wrong.

Reference lookup only. This is not coding advice and not validated for billing.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import audit
from .icd10 import FILLER, _hit, _singular_alts, _words

CODE_RE = re.compile(r"^[A-Va-v][0-9]{4}$")

# A small hand-written lay-term table, same spirit as icd10.py's: illustrative, not exhaustive.
GROUPS = [
    ["wheelchair", "chair"],
    ["walker", "cane", "crutch", "crutches"],
    ["oxygen", "o2"],
    ["ambulance", "transport"],
    ["hearing", "audio", "audiologic"],
    ["diabetic", "diabetes"],
    ["catheter", "catheterization"],
    ["ostomy", "colostomy"],
]
_SYN = {w: g for g in GROUPS for w in g}


@dataclass(frozen=True)
class Entry:
    code: str  # normalized, uppercase, 5 chars, no dot
    description: str

    @property
    def display(self) -> str:
        return self.code


def normalize_code(code: str) -> str:
    return re.sub(r"\s", "", str(code or "")).upper()


def looks_like_code(text: str) -> bool:
    return bool(CODE_RE.match(normalize_code(text)))


def find_code_in_text(text: str) -> str | None:
    """A code-shaped token inside a sentence, e.g. 'what is e0100' -> 'E0100'. Only the shape;
    the caller checks it against the loaded file."""
    for tok in re.findall(r"[A-Va-v]\d{4}\b", text):
        if looks_like_code(tok):
            return normalize_code(tok)
    return None


def _expand_query(query: str) -> list[list[str]]:
    q = " ".join(_words(query))
    required: list[list[str]] = []
    for tok in q.split():
        group = _SYN.get(tok, [])
        required.append([tok] + [a for a in group if a != tok] + ([] if tok in FILLER else _singular_alts(tok)))
    return required


_CODE_AT_START = re.compile(r"^[A-Za-z]\d{4}")


def _fixed_width_rows(raw: str) -> list[tuple[str, str]]:
    """CMS's actual public ANWEB.TXT layout: fixed-width, no delimiter, no header.
    Per the file's own record layout doc (HCPC<year>_recordlayout.txt): byte 1-5 = HCPCS code,
    byte 6-10 = a sequence number used *by 100s* to link together the physical lines of one
    code's long description when it's too long for one line (100 = the first/primary line,
    which also carries the short description and pricing/coverage columns starting at byte 92;
    200, 300, ... = pure continuation text with nothing else on the line), byte 11 = one filler
    byte, byte 12-91 (80 chars) = (a chunk of) the long description.

    A handful of non-conforming lines sit at the very top of the real CMS export (observed:
    'A1001'..'A5001', offset by 3 leading spaces from where the layout says the code should be,
    with placeholder descriptions like 'Dressing for one wound') — these aren't real codes, just
    some kind of example/filler rows CMS ships ahead of the real data. Requiring the code to sit
    at byte 1-5 exactly (no leading whitespace) is what excludes them, which is deliberate."""
    current_code = None
    current_parts: list[str] = []
    rows: list[tuple[str, str]] = []

    def flush():
        if current_code is not None:
            rows.append((current_code, " ".join(p for p in current_parts if p)))

    for line in raw.splitlines():
        if len(line) < 10:
            continue
        code, seq_txt = line[0:5], line[5:10]
        if not _CODE_AT_START.match(code) or not seq_txt.strip().isdigit():
            continue
        seq = int(seq_txt)
        frag = line[11:91].rstrip() if len(line) > 11 else ""
        if seq == 100:
            flush()
            current_code = code
            current_parts = [frag] if frag else []
        elif code == current_code:
            if frag:
                current_parts.append(frag)
        # a continuation line (seq != 100) whose code doesn't match an open record is junk/noise
        # from a layout we don't understand yet — skip it rather than guess.
    flush()
    return rows


def _detect_rows(path: Path) -> list[tuple[str, str]]:
    """(code, description) pairs from whatever layout the file actually uses. Tries, in order:
    a delimited file with a header row naming the code/description columns (CMS's recent CSV-style
    alpha-numeric files); CMS's actual public ANWEB.TXT fixed-width layout (see _fixed_width_rows);
    and last, a simple 'CODE<whitespace>description' file (same shape as the ICD-10-CM codes file,
    for a hand-trimmed or simplified HCPCS list). Raises ValueError, not a silent empty result, if
    nothing recognizes the file — see the module docstring."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    sample = raw[:4096]
    delim = None
    for cand in (",", "\t", "|"):
        if sample.count(cand) > sample.count("\n"):
            delim = cand
            break
    if delim:
        reader = csv.reader(io.StringIO(raw), delimiter=delim)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        header_l = [h.strip().lower() for h in header]
        code_i = next((i for i, h in enumerate(header_l) if "hcpc" in h or h == "code"), None)
        desc_i = next((i for i, h in enumerate(header_l) if "long" in h and "desc" in h), None)
        if desc_i is None:
            desc_i = next((i for i, h in enumerate(header_l) if "desc" in h), None)
        if code_i is not None and desc_i is not None:
            out = []
            for row in reader:
                if len(row) > max(code_i, desc_i):
                    out.append((row[code_i].strip(), row[desc_i].strip()))
            return out
    fixed = _fixed_width_rows(raw)
    if fixed:
        return fixed
    # Fallback: 'CODE<ws>description', one per line (no header assumed) — a hand-trimmed list.
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.rstrip("\r\n").split(None, 1)
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


class HCPCSIndex:
    def __init__(self, entries: list[Entry], source: str = "", year: str = ""):
        self.entries = entries
        self.source = source
        self.year = year
        self._by_code = {e.code: e for e in entries}
        self._words = [set(_words(e.description)) for e in entries]

    @classmethod
    def from_file(cls, path: str | Path) -> "HCPCSIndex":
        path = Path(path)
        rows = _detect_rows(path)
        entries: list[Entry] = []
        skipped = 0
        for code, desc in rows:
            code = normalize_code(code)
            if not desc or not CODE_RE.match(code):
                skipped += 1
                continue
            entries.append(Entry(code, desc))
        total = len(entries) + skipped
        if not entries or (total and skipped / total > 0.05):
            raise ValueError(
                f"{path.name} does not look like a HCPCS Level II code file "
                f"({len(entries)} valid rows, {skipped} unrecognized). CMS has changed this file's "
                "internal layout between releases before; if this is a genuine CMS download, the "
                "column-detection in hcpcs.py needs updating for this release's layout."
            )
        year = next(iter(re.findall(r"20\d\d", path.name)), "")
        return cls(entries, source=path.name, year=year)

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, code: str) -> Entry | None:
        return self._by_code.get(normalize_code(code))

    def describe(self, code: str) -> dict:
        norm = normalize_code(code)
        entry = self.lookup(norm)
        result = ({"status": "valid", "code": entry.display, "description": entry.description} if entry
                  else {"status": "unknown", "code": norm})
        result["source_file"] = self.source
        audit.log_call("hcpcs_describe", {"op": "describe"}, result["status"])
        return result

    def _ranked(self, terms: list[list[str]]) -> list[Entry]:
        originals = [alts[0] for alts in terms]
        scored = []
        for entry, words in zip(self.entries, self._words):
            if all(any(_hit(w, a) for a in alts for w in words) for alts in terms):
                direct = sum(1 for t in originals if any(w.startswith(t) for w in words))
                exact = sum(1 for t in originals if t in words)
                scored.append((-direct, -exact, len(entry.description), entry.code, entry))
        size = Counter(s[3][0] for s in scored)  # leading letter, same "biggest group first" idea as icd10
        scored.sort(key=lambda x: (x[0], x[1], -size[x[3][0]], x[2], x[3]))
        return [s[4] for s in scored]

    def search_detail(self, query: str, limit: int = 10) -> tuple[list[Entry], list[str], int]:
        """Like icd10.ICD10Index.search_detail: every word must match; if nothing does, retry
        after dropping only filler words and words that match no description at all."""
        terms = _expand_query(query)
        if not terms:
            return [], [], 0
        results = self._ranked(terms)
        ignored: list[str] = []
        if not results and len(terms) > 1:
            drop = [t for t in terms if t[0] in FILLER]
            base = [t for t in terms if t not in drop]
            dead = [t for t in base if not self._ranked([t])]
            base = [t for t in base if t not in dead]
            ignored = [t[0] for t in terms if t in drop or t in dead]
            if base and ignored:
                results = self._ranked(base)
            if not results:
                ignored = []
        total = len(results)
        results = results[:limit]
        audit.log_call("hcpcs_search", {"query_chars": len(query)},
                       f"{len(results)} of {total} results, {len(ignored)} words ignored")
        return results, ignored, total

    def search(self, query: str, limit: int = 10) -> list[Entry]:
        return self.search_detail(query, limit)[0]

    def did_you_mean(self, query: str, per_word: int = 3) -> dict[str, list[str]]:
        if not hasattr(self, "_vocab"):
            self._vocab = sorted({w for ws in self._words for w in ws if len(w) >= 4 and not w.isdigit()})
        out: dict[str, list[str]] = {}
        for t in _expand_query(query):
            word = t[0]
            if word in FILLER or len(word) < 4 or word.isdigit() or self._ranked([t]):
                continue
            close = difflib.get_close_matches(word, self._vocab, n=per_word, cutoff=0.75)
            if close:
                out[word] = close
        return out
