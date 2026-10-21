"""Measure the duplicate matcher on Synthea patients with injected duplicates.

Usage: python scripts/evaluate_matching.py [path/to/patients.csv] [dup_rate] [seed]
Prints counts only. No patient details are printed or written.
"""

import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from him_workbench import matching
from him_workbench.matching import NICKNAMES, Patient, find_duplicates

INVERSE = defaultdict(list)
for nick, full in NICKNAMES.items():
    INVERSE[full].append(nick)


def load(path: Path) -> list[Patient]:
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("DEATHDATE"):
                continue
            out.append(Patient(
                id=row["Id"],
                first=row["FIRST"],
                last=row["LAST"],
                dob=row["BIRTHDATE"],
                sex=(row.get("GENDER") or "")[:1],
                zip=row.get("ZIP", ""),
            ))
    return out


def typo(s: str, rng: random.Random) -> str:
    if len(s) < 4:
        return s
    i = rng.randrange(1, len(s) - 1)
    kind = rng.choice(["swap", "drop", "sub"])
    if kind == "swap":
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if kind == "drop":
        return s[:i] + s[i + 1:]
    return s[:i] + rng.choice("aeiourstln") + s[i + 1:]


def c_first_typo(p, rng):
    return Patient(p.id, typo(p.first, rng), p.last, p.dob, p.sex, p.zip)

def c_last_typo(p, rng):
    return Patient(p.id, p.first, typo(p.last, rng), p.dob, p.sex, p.zip)

def c_nickname(p, rng):
    nicks = INVERSE.get(p.first.lower())
    if not nicks:
        return None
    return Patient(p.id, rng.choice(nicks).title(), p.last, p.dob, p.sex, p.zip)

def c_dob_swap(p, rng):
    y, m, d = p.dob.split("-")
    if int(d) > 12 or d == m:
        return None
    return Patient(p.id, p.first, p.last, f"{y}-{d}-{m}", p.sex, p.zip)

def c_dob_year(p, rng):
    y, m, d = p.dob.split("-")
    return Patient(p.id, p.first, p.last, f"{int(y) + rng.choice([-1, 1])}-{m}-{d}", p.sex, p.zip)

def c_missing(p, rng):
    return Patient(p.id, p.first, p.last, p.dob, "", "")


CORRUPTIONS = {
    "first_typo": c_first_typo,
    "last_typo": c_last_typo,
    "nickname": c_nickname,
    "dob_month_day_swap": c_dob_swap,
    "dob_year_off_by_one": c_dob_year,
    "missing_sex_zip": c_missing,
}


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/synthea/csv/patients.csv")
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 0.10
    rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 7)
    originals = load(path)
    records = list(originals)
    truth: dict[frozenset, str] = {}
    for p in rng.sample(originals, int(len(originals) * rate)):
        name = rng.choice(list(CORRUPTIONS))
        dup = CORRUPTIONS[name](p, rng)
        if dup is None:
            continue
        dup = Patient(f"dup-{p.id}", dup.first, dup.last, dup.dob, dup.sex, dup.zip)
        records.append(dup)
        truth[frozenset((p.id, dup.id))] = name

    results = find_duplicates(records)
    found = {frozenset((r.a, r.b)): r.label for r in results}

    by_type = defaultdict(Counter)
    for pair, kind in truth.items():
        by_type[kind][found.get(pair, "missed")] += 1

    false_match = sum(1 for pair, lab in found.items() if pair not in truth and lab == "match")
    false_review = sum(1 for pair, lab in found.items() if pair not in truth and lab == "review")

    print(f"Records: {len(originals)} originals + {len(truth)} injected duplicates")
    print(f"\n{'corruption':22} {'match':>6} {'review':>7} {'missed':>7}")
    tot = Counter()
    for kind, c in sorted(by_type.items()):
        print(f"{kind:22} {c['match']:>6} {c['review']:>7} {c['missed']:>7}")
        tot.update(c)
    n = sum(tot.values()) or 1
    print(f"\nTrue duplicates auto-matched : {tot['match']}/{n} ({tot['match']/n:.0%})")
    print(f"True duplicates sent to review: {tot['review']}/{n} ({tot['review']/n:.0%})")
    print(f"True duplicates missed        : {tot['missed']}/{n} ({tot['missed']/n:.0%})")
    print(f"\nFalse auto-matches (different people): {false_match}")
    print(f"False review flags (different people): {false_review}")


if __name__ == "__main__":
    main()
