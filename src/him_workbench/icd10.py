"""ICD-10-CM code lookup from the public CDC/CMS code descriptions file.

Reads the 'codes' text file (one code per line: code, whitespace, long description).
The code set is NOT bundled: download it with scripts/fetch_icd10.sh. Code sets change
every October 1 (and sometimes April 1), so always note which file a result came from.

Reference lookup only. This is not coding advice and not validated for billing.
"""

from __future__ import annotations

import re
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

    def search(self, query: str, limit: int = 10) -> list[Entry]:
        tokens = _words(query)
        if not tokens:
            return []
        scored = []
        for entry, words in zip(self.entries, self._words):
            if all(any(w.startswith(t) for w in words) for t in tokens):
                exact = sum(1 for t in tokens if t in words)
                scored.append((-exact, len(entry.description), entry.code, entry))
        scored.sort(key=lambda x: x[:3])
        results = [s[3] for s in scored[:limit]]
        audit.log_call("icd10_search", {"query_chars": len(query)}, f"{len(results)} results")
        return results
