"""ICD-10-CM code lookup from the public CDC/CMS code descriptions file.

Reads the 'codes' text file (one code per line: code, whitespace, long description).
The code set is NOT bundled: download it with scripts/fetch_icd10.sh. Code sets change
every October 1 (and sometimes April 1), so always note which file a result came from.

Reference lookup only. This is not coding advice and not validated for billing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import audit

CODE_RE = re.compile(r"^[A-Z][0-9][0-9A-Z]{1,5}$")


@dataclass(frozen=True)
class Entry:
    code: str  # normalized, no dot
    description: str

    @property
    def display(self) -> str:
        return format_code(self.code)


def normalize_code(code: str) -> str:
    return re.sub(r"[\s.]", "", code).upper()


def format_code(code: str) -> str:
    code = normalize_code(code)
    return f"{code[:3]}.{code[3:]}" if len(code) > 3 else code


def looks_like_code(text: str) -> bool:
    return bool(CODE_RE.match(normalize_code(text)))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


# Small hand-written lay-term table so searches like "kidney" also find "renal".
# Illustrative, not a clinical thesaurus. Each group is interchangeable for search.
PHRASES = {
    "high blood pressure": ["hypertension"],
    "low blood pressure": ["hypotension"],
    "heart attack": ["myocardial", "infarction"],
    "blood clot": ["thrombosis"],
}
GROUPS = [
    ["kidney", "renal", "nephr"],
    ["heart", "cardiac", "cardio", "myocard"],
    ["liver", "hepat"],
    ["lung", "pulmon"],
    ["stomach", "gastr"],
    ["cancer", "carcinoma", "malignant", "neoplasm"],
    ["broken", "fracture"],
    ["swelling", "edema"],
    ["fever", "pyrexia", "febrile"],
    ["bleeding", "hemorrhage"],
    ["stroke", "cerebrovascular"],
    ["baby", "newborn", "neonatal", "infant"],
    ["pernicious", "intrinsic"],  # pernicious anemia is indexed to B12 deficiency due to intrinsic factor
]
_SYN = {w: g for g in GROUPS for w in g}

# For a plain-English phrase, descriptions containing these words rank first. Hand-written:
# the ICD-10-CM index sends "high blood pressure" to the essential (primary) hypertension code.
PREFER = {
    "high blood pressure": ["essential"],
}


def _prefer_words(query: str) -> list[str]:
    q = " ".join(_words(query))
    return [w for phrase, ws in PREFER.items() if phrase in q for w in ws]

# Words people add that codes rarely contain. Only dropped when nothing matches every word.
FILLER = {"complication", "complications", "problem", "problems", "issue", "issues",
          # question wrapper words: "what is the diagnosis code for cystitis"
          "diagnosis", "diagnoses", "code", "codes", "icd", "icd10", "for", "the", "a", "an", "of",
          "what", "whats", "is", "find", "show", "me", "give", "search", "lookup", "to", "i", "need"}


def real_words(ignored: list[str]) -> list[str]:
    """Ignored words that are not plain filler. These may be real medical terms the code
    descriptions simply do not use (index terms, eponyms), so results may not fit them."""
    return [w for w in ignored if w not in FILLER]


def _expand_query(query: str) -> list[list[str]]:
    """Return, per required term, the list of alternative word-prefixes that satisfy it."""
    q = " ".join(_words(query))
    required: list[list[str]] = []
    for phrase, alts in PHRASES.items():
        if phrase in q:
            q = q.replace(phrase, " ")
            required.extend([[a] for a in alts])
    for tok in q.split():
        group = _SYN.get(tok, [])
        required.append([tok] + [a for a in group if a != tok])  # typed word first
    return required


class ICD10Index:
    def __init__(self, entries: list[Entry], source: str = "", year: str = ""):
        self.entries = entries
        self.source = source
        self.year = year
        self._by_code = {e.code: e for e in entries}
        self._words = [set(_words(e.description)) for e in entries]

    @classmethod
    def from_file(cls, path: str | Path) -> "ICD10Index":
        path = Path(path)
        entries: list[Entry] = []
        skipped = 0
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                parts = line.rstrip("\r\n").split(None, 1)
                code = parts[0].upper() if parts else ""
                if len(parts) < 2 or not CODE_RE.match(code):
                    skipped += 1
                    continue
                entries.append(Entry(code, parts[1].strip()))
        total = len(entries) + skipped
        if not entries or skipped / total > 0.01:
            raise ValueError(
                f"{path.name} does not look like the ICD-10-CM codes file "
                f"({len(entries)} valid lines, {skipped} unrecognized). "
                "Expected one 'CODE  description' per line."
            )
        year = next(iter(re.findall(r"20\d\d", path.name)), "")
        return cls(entries, source=path.name, year=year)

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, code: str) -> Entry | None:
        return self._by_code.get(normalize_code(code))

    def children(self, prefix: str) -> list[Entry]:
        p = normalize_code(prefix)
        return [e for e in self.entries if e.code.startswith(p) and e.code != p]

    def describe(self, code: str) -> dict:
        """Status is relative to the loaded file: 'valid' (a full code in the file),
        'category' (a parent that has more specific codes), or 'unknown'."""
        norm = normalize_code(code)
        entry = self.lookup(norm)
        if entry:
            result = {"status": "valid", "code": entry.display, "description": entry.description}
        else:
            kids = self.children(norm) if len(norm) >= 3 else []
            if kids:
                result = {
                    "status": "category",
                    "code": format_code(norm),
                    "note": "Not a complete code on its own. Use a more specific code.",
                    "more_specific_count": len(kids),
                    "examples": [(k.display, k.description) for k in kids[:5]],
                }
            else:
                result = {"status": "unknown", "code": format_code(norm)}
        result["source_file"] = self.source
        audit.log_call("icd10_describe", {"op": "describe"}, result["status"])
        return result

    def _ranked(self, terms: list[list[str]], prefer: list[str] | None = None) -> list[Entry]:
        """All entries that satisfy every term, best first."""
        prefer = prefer or []
        originals = [alts[0] for alts in terms]
        scored = []
        for entry, words in zip(self.entries, self._words):
            if all(any(w.startswith(a) for a in alts for w in words) for alts in terms):
                direct = sum(1 for t in originals if any(w.startswith(t) for w in words))
                exact = sum(1 for t in originals if t in words)
                pref = sum(1 for t in prefer if any(w.startswith(t) for w in words))
                scored.append((-direct, -exact, -pref, len(entry.description), entry.code, entry))
        # Codes from the category that matched most often come first: for "cystitis" that is the
        # everyday N30 group, ahead of one-off infection codes like A06.81. A plain heuristic.
        size = Counter(s[4][:3] for s in scored)
        scored.sort(key=lambda x: (x[0], x[1], x[2], -size[x[4][:3]], x[3], x[4]))
        return [s[5] for s in scored]

    def search(self, query: str, limit: int = 10, synonyms: bool = True) -> list[Entry]:
        """Strict search: every word must match."""
        terms = _expand_query(query) if synonyms else [[t] for t in _words(query)]
        if not terms:
            return []
        prefer = _prefer_words(query) if synonyms else []
        results = self._ranked(terms, prefer)[:limit]
        audit.log_call("icd10_search", {"query_chars": len(query)}, f"{len(results)} results")
        return results

    def search_detail(self, query: str, limit: int = 10, synonyms: bool = True) -> tuple[list[Entry], list[str], int]:
        """Like search, but if nothing matches every word, retry without the words that get in
        the way. Returns (results, ignored_words, total_matches) so the caller can say what was
        dropped and whether more matches exist than were returned.
        Only two things are ever dropped: filler words like 'complications', and words that match
        no code at all (typos, junk). It never drops a real word to force an answer."""
        terms = _expand_query(query) if synonyms else [[t] for t in _words(query)]
        if not terms:
            return [], [], 0
        prefer = _prefer_words(query) if synonyms else []
        results = self._ranked(terms, prefer)
        ignored: list[str] = []
        if not results and len(terms) > 1:
            drop = [t for t in terms if t[0] in FILLER]
            base = [t for t in terms if t not in drop]
            dead = [t for t in base if not self._ranked([t])]
            base = [t for t in base if t not in dead]
            ignored = [t[0] for t in terms if t in drop or t in dead]
            if base and ignored:
                results = self._ranked(base, prefer)
            if not results:
                ignored = []
        total = len(results)
        results = results[:limit]
        audit.log_call("icd10_search", {"query_chars": len(query)},
                       f"{len(results)} of {total} results, {len(ignored)} words ignored")
        return results, ignored, total

    def search_relaxed(self, query: str, limit: int = 10, synonyms: bool = True) -> tuple[list[Entry], list[str]]:
        results, ignored, _ = self.search_detail(query, limit, synonyms)
        return results, ignored
