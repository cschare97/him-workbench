"""Invented demo data for the dashboard. Synthetic: every name, provider, date and number here is made up by a seeded
random generator, so the dashboard has something to show. It is never mixed into your real trackers: it is written to
its own folder (data/demo by default) and the web page labels it DEMO when it is loaded.

What is real and what is not:
  - The referral and ROI rows go through the same store code as real entries (same validation, same dated event history).
  - The data-quality snapshots are real outputs of analyze(), run on invented patients with defects injected at a rate that
    falls from week to week. The improving trend is something this generator puts there on purpose.
  - Patient ids are placeholders (demo-0001 ...). Nothing links to Synthea or any real record.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .analytics import QualityHistory
from .matching import Patient
from .quality import analyze
from .referrals import ReferralStore
from .roi import REQUESTER_TYPES, RoiStore

# specialty -> (fastest, slowest) days from sent to scheduled. Invented.
SPECIALTIES = {"Cardiology": (3, 11), "Dermatology": (14, 42), "Orthopedics": (7, 22), "Gastroenterology": (10, 30),
               "Neurology": (12, 38), "Ophthalmology": (8, 25), "Endocrinology": (9, 27), "Physical therapy": (2, 9)}
REFERRING = ("Dr. A. Rivera", "Dr. B. Chen", "Dr. C. Okafor", "Dr. D. Novak")
COORDS = ("Jordan Reyes", "Casey Lin", "Morgan Patel", "Jordan Reyes", "Casey Lin", "Morgan Patel", "Unassigned")
FIRST = ("Alex", "Sam", "Jamie", "Taylor", "Riley", "Jordan", "Casey", "Morgan", "Quinn", "Avery", "Rowan", "Skyler")
LAST = ("Rivera", "Chen", "Okafor", "Novak", "Haddad", "Lindqvist", "Moreau", "Tanaka", "Dube", "Silva", "Kowalski", "Mwangi")


def fake_patients(n: int, rng: random.Random) -> list[Patient]:
    out = []
    for i in range(n):
        y, m, d = rng.randint(1935, 2020), rng.randint(1, 12), rng.randint(1, 28)
        out.append(Patient(f"demo-{i + 1:04d}", rng.choice(FIRST), rng.choice(LAST), f"{y:04d}-{m:02d}-{d:02d}",
                           rng.choice("MF"), f"{rng.randint(90000, 92999):05d}", f"{rng.randint(2000000000, 9899999999)}"))
    return out


def _corrupt(p: Patient, rng: random.Random) -> Patient:
    kind = rng.choice(("space", "case", "zip", "phone", "sex", "dob", "blank_dob", "placeholder"))
    return {"space": lambda: replace(p, first=" " + p.first), "case": lambda: replace(p, last=p.last.upper()),
            "zip": lambda: replace(p, zip=p.zip[:4]), "phone": lambda: replace(p, phone=p.phone[:7]),
            "sex": lambda: replace(p, sex="Q"), "dob": lambda: replace(p, dob="1980-13-45"),
            "blank_dob": lambda: replace(p, dob=""), "placeholder": lambda: replace(p, first="Baby")}[kind]()


def seed_quality(history: QualityHistory, today: date, weeks: int = 12, n_patients: int = 400, seed: int = 2026) -> None:
    base = fake_patients(n_patients, random.Random(seed))
    for w in range(weeks):
        rng = random.Random(seed * 100 + w)
        rate = max(0.01, 0.17 - 0.13 * w / max(weeks - 1, 1) + rng.uniform(-0.01, 0.01))
        pts = [_corrupt(p, rng) if rng.random() < rate else p for p in base]
        day = today - timedelta(days=7 * (weeks - 1 - w))
        history.record(analyze(pts, today=day), day, source="demo")


def seed_referrals(store: ReferralStore, today: date, n: int = 160, seed: int = 2026) -> None:
    rng = random.Random(seed + 1)
    for i in range(n):
        spec = rng.choice(list(SPECIALTIES))
        lo, hi = SPECIALTIES[spec]
        urgent = rng.random() < 0.2
        created = today - timedelta(days=rng.randint(1, 300))
        rid = store.create(f"demo-{rng.randint(1, 400):04d}", spec, "urgent" if urgent else "routine",
                           f"Demo referral to {spec.lower()}", referring_provider=rng.choice(REFERRING),
                           receiving_provider=f"Example {spec} Group", coordinator=rng.choice(COORDS),
                           created=created, today=created)
        roll = rng.random()
        if roll < 0.06:                                        # declined early
            day = created + timedelta(days=rng.randint(3, 20))
            if day <= today:
                store.set_status(rid, "declined", note="Demo: patient declined care", today=day)
            continue
        if roll < 0.14:                                        # stalled: never scheduled
            continue
        lag = rng.randint(lo, hi)
        lag = max(1, lag // 2) if urgent else lag
        sched = created + timedelta(days=lag)
        if sched > today:
            continue
        appt = sched + timedelta(days=rng.randint(3, 21))
        store.set_status(rid, "scheduled", appointment_date=appt, today=sched)
        if appt <= today and rng.random() < 0.9:
            store.set_status(rid, "completed", today=appt)


def seed_roi(store: RoiStore, today: date, n: int = 90, seed: int = 2026) -> None:
    rng = random.Random(seed + 2)
    kinds = ["patient"] * 5 + ["attorney"] * 2 + ["insurer"] * 2 + ["another provider", "patient's representative", "other"]
    kinds = [k for k in kinds if k in REQUESTER_TYPES]
    for i in range(n):
        got = today - timedelta(days=rng.randint(1, 300))
        rid = store.create(f"demo-{rng.randint(1, 400):04d}", rng.choice(kinds), "Demo records request", got, today=got)
        verify = got + timedelta(days=rng.randint(1, 5))
        progress = verify + timedelta(days=rng.randint(1, 3))
        late = rng.random() < 0.15
        close = got + timedelta(days=rng.randint(31, 45) if late else min(30, max(8, int(rng.gauss(18, 6)))))
        close = max(close, progress + timedelta(days=1))
        steps = [(verify, "verified"), (progress, "in_progress")]
        if rng.random() < 0.05:
            steps.append((close, "denied"))
        else:
            steps.append((close, "fulfilled"))
        if rng.random() < 0.10 and not late:                   # one extension, before the original due date
            ext = got + timedelta(days=rng.randint(20, 28))
            if verify < ext < close:
                steps.insert(2, (ext, "extend"))
        for day, what in sorted(steps, key=lambda s: s[0]):
            if day > today:
                break
            if what == "extend":
                store.extend(rid, "Demo: more records needed", today=day)
            else:
                store.set_status(rid, what, note="Demo: not authorized" if what == "denied" else "", today=day)


def seed_demo(folder: str | Path = "data/demo", today: date | None = None, force: bool = False) -> dict[str, str]:
    """Create referrals.db, roi.db and quality.db in `folder`. Refuses to overwrite unless force=True."""
    today = today or date.today()
    folder = Path(folder)
    paths = {k: folder / f"{k}.db" for k in ("referrals", "roi", "quality")}
    existing = [p for p in paths.values() if p.exists()]
    if existing and not force:
        raise FileExistsError(f"{existing[0]} already exists. Use force to replace the demo files.")
    folder.mkdir(parents=True, exist_ok=True)
    for p in existing:
        p.unlink()
    seed_referrals(ReferralStore(paths["referrals"]), today)
    seed_roi(RoiStore(paths["roi"]), today)
    seed_quality(QualityHistory(paths["quality"]), today)
    return {k: str(v) for k, v in paths.items()}
