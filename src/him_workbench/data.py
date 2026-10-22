"""Loaders for local synthetic data (Synthea patients, ICD-10-CM code file)."""

from __future__ import annotations

import csv
from pathlib import Path

from .matching import Patient


def find_icd_file(root: str | Path = "data/icd10cm") -> Path | None:
    """The CDC 'codes' file, not the addenda or order files."""
    found = sorted(p for p in Path(root).rglob("*.txt")
                   if "codes" in p.name.lower() and "addenda" not in p.name.lower()
                   and "order" not in p.name.lower())
    return found[-1] if found else None


def load_synthea_patients(path: str | Path, include_deceased: bool = False) -> list[Patient]:
    out = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("DEATHDATE") and not include_deceased:
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
