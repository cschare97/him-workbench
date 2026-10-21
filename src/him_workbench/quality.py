"""Data-quality report for patient demographic records.

Checks format and completeness only: a field can be well-formed and still wrong, and this
cannot tell. Output is counts, never patient values. Public, textbook checks; not validated
for clinical use.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from . import audit
from .matching import Patient, normalize_name

FIELDS = ("first", "last", "dob", "sex", "zip", "phone")

CHECK_LABELS = {
    "dob_malformed": "birth date not a real YYYY-MM-DD date",
    "dob_in_future": "birth date in the future",
    "dob_over_120": "birth date more than 120 years ago",
    "sex_unexpected": "sex not one of M, F, U, X",
    "zip_malformed": "zip not 5 digits or 5+4",
    "phone_malformed": "phone not 10 digits (or 11 starting with 1)",
    "name_whitespace": "name has leading, trailing, or doubled spaces",
    "name_case": "name is all upper-case or all lower-case",
    "name_odd_characters": "name has characters other than letters, space, ' - .",
    "name_placeholder": "name looks like a placeholder (unknown, test, baby...)",
}

_PLACEHOLDERS = {"unknown", "unk", "test", "none", "na", "baby", "babygirl", "babyboy",
                 "newborn", "anonymous"}
_NAME_PUNCT = set(" .'\u2019-")  # space, period, apostrophes, hyphen
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class QualityReport:
    records: int
    missing: dict[str, int] = field(default_factory=dict)
    problems: dict[str, int] = field(default_factory=dict)  # check -> records affected
    same_name_dob_groups: int = 0
    same_name_dob_records: int = 0
    empty_fields: list[str] = field(default_factory=list)  # blank in every record: likely not in the source

    def to_dict(self) -> dict:
        return {"records": self.records, "missing": self.missing, "problems": self.problems,
                "same_name_dob_groups": self.same_name_dob_groups,
                "same_name_dob_records": self.same_name_dob_records, "empty_fields": self.empty_fields}

    def summary(self) -> str:
        flagged = {k: v for k, v in self.problems.items() if v}
        gaps = {k: v for k, v in self.missing.items() if v and k not in self.empty_fields}
        parts = [f"{self.records} records."]
        parts.append("Missing values: " + (", ".join(f"{k} {v}" for k, v in gaps.items()) or "none") + ".")
        if self.empty_fields:
            parts.append("Empty in every record (probably not in the source): " + ", ".join(self.empty_fields) + ".")
        parts.append("Format problems: " + (", ".join(f"{k} {v}" for k, v in flagged.items()) or "none") + ".")
        parts.append(f"{self.same_name_dob_groups} group(s) share the same name and birth date "
                     f"({self.same_name_dob_records} records).")
        return " ".join(parts)

    def text(self) -> str:
        n = max(self.records, 1)
        lines = [f"Data quality report: {self.records} records", "", "Missing values"]
        for k in FIELDS:
            v = self.missing.get(k, 0)
            note = "  empty in every record; probably not in the source" if k in self.empty_fields else ""
            lines.append(f"  {k:<7}{v:>6}  ({v / n:.1%}){note}")
        lines += ["", "Format problems (records affected)"]
        for k, label in CHECK_LABELS.items():
            v = self.problems.get(k, 0)
            lines.append(f"  {k:<20}{v:>6}  ({v / n:.1%})  {label}")
        lines += ["", f"Same name and birth date: {self.same_name_dob_groups} group(s), "
                      f"{self.same_name_dob_records} records (exact matches only; "
                      "use the duplicate scan for fuzzy ones)"]
        return "\n".join(lines)


def _valid_date(s: str) -> date | None:
    if not _DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _name_problems(name: str) -> set[str]:
    out: set[str] = set()
    if name != name.strip() or "  " in name:
        out.add("name_whitespace")
    s = name.strip()
    if not s:
        return out
    letters = "".join(ch for ch in s if ch.isalpha())  # any alphabet, so José and Nuñez are fine
    if len(letters) > 1 and (letters.isupper() or letters.islower()):
        out.add("name_case")
    if not s[0].isalpha() or not all(ch.isalpha() or ch in _NAME_PUNCT for ch in s):
        out.add("name_odd_characters")
    if letters.lower() in _PLACEHOLDERS:
        out.add("name_placeholder")
    return out


def analyze(patients: Iterable[Patient], today: date | None = None) -> QualityReport:
    today = today or date.today()
    pts = list(patients)
    missing = Counter()
    problems = Counter()
    keys: Counter = Counter()
    for p in pts:
        raw = {"first": p.first, "last": p.last, "dob": p.dob, "sex": p.sex, "zip": p.zip, "phone": p.phone}
        for k, v in raw.items():
            if not (v or "").strip():
                missing[k] += 1
        found: set[str] = set()
        found |= _name_problems(p.first or "") | _name_problems(p.last or "")
        dob = (p.dob or "").strip()
        if dob:
            d = _valid_date(dob)
            if d is None:
                found.add("dob_malformed")
            elif d > today:
                found.add("dob_in_future")
            elif (today - d).days / 365.25 > 120:
                found.add("dob_over_120")
        sex = (p.sex or "").strip()
        if sex and sex.upper() not in {"M", "F", "U", "X"}:
            found.add("sex_unexpected")
        z = (p.zip or "").strip()
        if z and not re.fullmatch(r"\d{5}(-?\d{4})?", z):
            found.add("zip_malformed")
        ph = re.sub(r"\D", "", p.phone or "")
        if (p.phone or "").strip() and not (len(ph) == 10 or (len(ph) == 11 and ph[0] == "1")):
            found.add("phone_malformed")
        for k in found:
            problems[k] += 1
        if p.first.strip() and p.last.strip() and dob:
            keys[(normalize_name(p.first), normalize_name(p.last), dob)] += 1
    groups = [c for c in keys.values() if c > 1]
    report = QualityReport(
        records=len(pts),
        missing={k: missing.get(k, 0) for k in FIELDS},
        problems={k: problems.get(k, 0) for k in CHECK_LABELS},
        same_name_dob_groups=len(groups),
        same_name_dob_records=sum(groups),
        empty_fields=[k for k in FIELDS if pts and missing.get(k, 0) == len(pts)],
    )
    audit.log_call("quality_report", {"records": len(pts)},
                   f"{sum(report.problems.values())} problem flags, {sum(report.missing.values())} missing values")
    return report
