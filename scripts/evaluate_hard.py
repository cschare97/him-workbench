"""Harder matcher test on Synthea patients.

1) Duplicates with TWO errors at once.
2) Look-alike DIFFERENT people (twins, parent/child with same name, siblings,
   same-name strangers). These must never be auto-matched.

Usage: python scripts/evaluate_hard.py [path/to/patients.csv] [seed]
Prints counts only.
"""

import random
import sys
from collections import Counter
from pathlib import Path

from evaluate_matching import CORRUPTIONS, load
from him_workbench.matching import Patient, find_duplicates


def two_errors(p, rng):
    a, b = rng.sample(list(CORRUPTIONS), 2)
    first = CORRUPTIONS[a](p, rng)
    if first is None:
        return None, None
    second = CORRUPTIONS[b](first, rng)
    if second is None:
        return None, None
    return second, f"{a}+{b}"


def twin(p, pool, rng):
    names = [q.first for q in pool if q.sex == p.sex and q.first.lower() != p.first.lower()]
    return Patient("", rng.choice(names), p.last, p.dob, p.sex, p.zip)


def parent_child(p, pool, rng):
    y, m, d = p.dob.split("-")
    delta = rng.choice([-1, 1]) * rng.randint(22, 38)
    return Patient("", p.first, p.last, f"{int(y) + delta}-{m}-{d}", p.sex, p.zip)


def sibling(p, pool, rng):
    names = [q.first for q in pool if q.sex == p.sex and q.first.lower() != p.first.lower()]
    y, m, d = p.dob.split("-")
    return Patient("", rng.choice(names), p.last, f"{int(y) + rng.choice([-1, 1]) * rng.randint(1, 6)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}", p.sex, p.zip)


def same_name_stranger(p, pool, rng):
    other = rng.choice(pool)
    while other.id == p.id or other.dob == p.dob:  # must be a different person
        other = rng.choice(pool)
    return Patient("", p.first, p.last, other.dob, p.sex, other.zip)


LOOKALIKES = {
    "twins": twin,
    "parent_child_same_name": parent_child,
    "siblings": sibling,
    "same_name_stranger": same_name_stranger,
}


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/synthea/csv/patients.csv")
    rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 11)
    originals = load(path)
    records = list(originals)
    true_pairs: dict[frozenset, str] = {}
    look_pairs: dict[frozenset, str] = {}

    for p in rng.sample(originals, len(originals) // 3):
        dup, combo = two_errors(p, rng)
        if dup is None:
            continue
        dup = Patient(f"dup-{p.id}", dup.first, dup.last, dup.dob, dup.sex, dup.zip)
        records.append(dup)
        true_pairs[frozenset((p.id, dup.id))] = combo

    for kind, fn in LOOKALIKES.items():
        for p in rng.sample(originals, 60):
            q = fn(p, originals, rng)
            q = Patient(f"{kind}-{p.id}", q.first, q.last, q.dob, q.sex, q.zip)
            records.append(q)
            look_pairs[frozenset((p.id, q.id))] = kind

    results = {frozenset((r.a, r.b)): r for r in find_duplicates(records)}
    found = {k: r.label for k, r in results.items()}

    t = Counter(found.get(k, "missed") for k in true_pairs)
    n = len(true_pairs) or 1
    print(f"TWO-ERROR DUPLICATES ({n})")
    print(f"  auto-matched {t['match']} | review {t['review']} | missed {t['missed']}")
    bad = Counter(c for k, c in true_pairs.items() if found.get(k, "missed") == "missed")
    if bad:
        print("  missed combos:", dict(bad))

    print("\nLOOK-ALIKE DIFFERENT PEOPLE (auto-match = WRONG)")
    print(f"{'type':26} {'auto-match':>10} {'review':>7} {'ok':>5}")
    wrong = 0
    for kind in LOOKALIKES:
        c = Counter(found.get(k, "ok") if found.get(k) != "non-match" else "ok"
                    for k, v in look_pairs.items() if v == kind)
        wrong += c["match"]
        print(f"{kind:26} {c['match']:>10} {c['review']:>7} {c['ok']:>5}")
    print(f"\nWRONG AUTO-MATCHES TOTAL: {wrong}")
    for k, kind in look_pairs.items():
        r = results.get(k)
        if r and r.label == "match":
            print(f"  wrong match [{kind}] score={r.score} fields={r.detail}")


if __name__ == "__main__":
    main()
