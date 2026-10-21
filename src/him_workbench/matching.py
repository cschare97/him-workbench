"""Duplicate patient record detection.

Technique: blocking + Fellegi-Sunter style weighted field comparison
(log2 likelihood-ratio weights), with Jaro-Winkler name similarity.
Public, textbook methods only. The m/u probabilities below are illustrative
defaults for synthetic data, not tuned or validated for clinical use.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from itertools import combinations

from . import audit

# Small hand-written nickname table (common English given names).
NICKNAMES = {
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "bill": "william", "will": "william", "billy": "william",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "jim": "james", "jimmy": "james",
    "mike": "michael", "mick": "michael",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "tom": "thomas", "tommy": "thomas",
    "dick": "richard", "rick": "richard", "rich": "richard",
    "peggy": "margaret", "maggie": "margaret",
    "sue": "susan", "susie": "susan",
    "chris": "christopher",
    "joe": "joseph", "joey": "joseph",
    "tony": "anthony",
    "dave": "david",
}


@dataclass(frozen=True)
class Patient:
    id: str
    first: str
    last: str
    dob: str  # YYYY-MM-DD
    sex: str = ""
    zip: str = ""
    phone: str = ""


@dataclass
class PairResult:
    a: str
    b: str
    score: float
    label: str  # "match" | "review" | "non-match"
    detail: dict[str, float]
    reasons: list[str] = field(default_factory=list)  # why a high score was held back for review


def normalize_name(s: str) -> str:
    s = re.sub(r"[^a-z\s'-]", "", s.lower().strip())
    s = re.sub(r"[\s'-]+", " ", s).strip()
    return s


def canonical_first(s: str) -> str:
    n = normalize_name(s)
    return NICKNAMES.get(n, n)


def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    window = max(max(len(s1), len(s2)) // 2 - 1, 0)
    m1 = [False] * len(s1)
    m2 = [False] * len(s2)
    matches = 0
    for i, c in enumerate(s1):
        lo, hi = max(0, i - window), min(len(s2), i + window + 1)
        for j in range(lo, hi):
            if not m2[j] and s2[j] == c:
                m1[i] = m2[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t, k = 0, 0
    for i in range(len(s1)):
        if m1[i]:
            while not m2[k]:
                k += 1
            if s1[i] != s2[k]:
                t += 1
            k += 1
    t //= 2
    return (matches / len(s1) + matches / len(s2) + (matches - t) / matches) / 3


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    j = jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1[:4], s2[:4]):
        if a != b:
            break
        prefix += 1
    return j + prefix * p * (1 - j)


def soundex(s: str) -> str:
    s = re.sub(r"[^a-z]", "", s.lower())
    if not s:
        return ""
    codes = {c: d for d, letters in {
        "1": "bfpv", "2": "cgjkqsxz", "3": "dt", "4": "l", "5": "mn", "6": "r",
    }.items() for c in letters}
    out, last = s[0].upper(), codes.get(s[0], "")
    for c in s[1:]:
        d = codes.get(c, "")
        if d and d != last:
            out += d
        if c not in "hw":
            last = d
    return (out + "000")[:4]


# Illustrative m (P agree | true match) and u (P agree | non-match) values.
FIELD_PARAMS = {
    "first": (0.95, 0.01),
    "last": (0.95, 0.005),
    "dob": (0.97, 0.001),
    "sex": (0.99, 0.5),
    "zip": (0.85, 0.01),
    "phone": (0.80, 0.0005),
}
MATCH_THRESHOLD = 12.0
REVIEW_THRESHOLD = 5.0


def _weights(field: str) -> tuple[float, float]:
    m, u = FIELD_PARAMS[field]
    return math.log2(m / u), math.log2((1 - m) / (1 - u))


def _graded(field: str, sim: float) -> float:
    agree, disagree = _weights(field)
    if sim >= 0.92:
        return agree
    if sim >= 0.80:
        return (agree + disagree) / 2  # partial agreement
    return disagree


def _dob_weight(a: str, b: str) -> float:
    agree, disagree = _weights("dob")
    if not a or not b:
        return 0.0
    if a == b:
        return agree
    ya, ma, da = a.split("-") if a.count("-") == 2 else ("", "", "")
    yb, mb, db = b.split("-") if b.count("-") == 2 else ("", "", "")
    if ya == yb and ma == db and da == mb:  # month/day swapped
        return (agree + disagree) / 2
    if ma == mb and da == db and abs(int(ya or 0) - int(yb or 0)) in (1, 10):
        return (agree + disagree) / 2  # one-digit year typo
    return disagree


def _exact(field: str, a: str, b: str) -> float:
    if not a or not b:
        return 0.0  # missing data adds no evidence
    agree, disagree = _weights(field)
    return agree if a == b else disagree


def compare(a: Patient, b: Patient) -> PairResult:
    detail = {
        "first": _graded("first", jaro_winkler(canonical_first(a.first), canonical_first(b.first))),
        "last": _graded("last", jaro_winkler(normalize_name(a.last), normalize_name(b.last))),
        "dob": _dob_weight(a.dob, b.dob),
        "sex": _exact("sex", a.sex.lower(), b.sex.lower()),
        "zip": _exact("zip", a.zip[:5], b.zip[:5]),
        "phone": _exact("phone", re.sub(r"\D", "", a.phone)[-10:], re.sub(r"\D", "", b.phone)[-10:]),
    }
    score = sum(detail.values())
    label = "match" if score >= MATCH_THRESHOLD else "review" if score >= REVIEW_THRESHOLD else "non-match"
    # Safety rules: an automatic match needs the first names to be equal after nickname normalization
    # (similar-but-different names like twins, or typos, go to a human) and no clear DOB conflict (parent/child, same name).
    reasons: list[str] = []
    if label == "match":
        first_full = canonical_first(a.first) == canonical_first(b.first)
        dob_conflict = detail["dob"] <= _weights("dob")[1] + 1e-9
        if not first_full:
            reasons.append("first names not identical (typo or look-alike name)")
        if dob_conflict:
            reasons.append("dates of birth clearly conflict")
        if reasons:
            label = "review"
    return PairResult(a.id, b.id, round(score, 2), label,
                      {k: round(v, 2) for k, v in detail.items()}, reasons)


def _block_keys(p: Patient) -> set[str]:
    keys = set()
    last = soundex(p.last)
    first = canonical_first(p.first)
    if first and len(p.dob) == 10:
        keys.add(f"fn:{first}|md:{p.dob[5:]}")
        keys.add(f"fn:{first}|y:{p.dob[:4]}")
    if last and p.dob:
        keys.add(f"sx:{last}|y:{p.dob[:4]}")
    if last and len(p.dob) == 10:
        keys.add(f"sx:{last}|md:{p.dob[5:]}")  # catches year typos
    if p.dob:
        keys.add(f"dob:{p.dob}")
    return keys


def find_duplicates(patients: list[Patient], include_non_matches: bool = False) -> list[PairResult]:
    """Compare only records sharing a blocking key; return pairs sorted by score."""
    blocks: dict[str, list[int]] = {}
    for i, p in enumerate(patients):
        for k in _block_keys(p):
            blocks.setdefault(k, []).append(i)
    seen: set[tuple[int, int]] = set()
    results: list[PairResult] = []
    for idxs in blocks.values():
        for i, j in combinations(sorted(set(idxs)), 2):
            if (i, j) in seen:
                continue
            seen.add((i, j))
            r = compare(patients[i], patients[j])
            if include_non_matches or r.label != "non-match":
                results.append(r)
    results.sort(key=lambda r: r.score, reverse=True)
    audit.log_call(
        "find_duplicates",
        {"records": len(patients)},
        f"{len(results)} candidate pairs returned",
    )
    return results
