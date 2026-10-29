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


def find_hcpcs_file(root: str | Path = "data/hcpcs") -> Path | None:
    """The CMS alpha-numeric HCPCS data file itself (ANWEB), extracted by scripts/fetch_hcpcs.sh.
    The same CMS zip also ships a record-layout doc, correction/transaction reports, and a
    not-otherwise-classified-codes list; those are excluded by name, not just by extension."""
    found = sorted(p for p in Path(root).rglob("*")
                   if p.suffix.lower() in (".txt", ".csv")
                   and "anweb" in p.name.lower()
                   and "layout" not in p.name.lower()
                   and "correction" not in p.name.lower()
                   and "transaction" not in p.name.lower())
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


def load_patients(args: list[str], default_csv: str | Path = "data/synthea/csv/patients.csv") -> tuple[list[Patient], str]:
    """Patients for the command-line tools plus a plain-words label of where they came from.
    '--fhir PATH' reads FHIR files, otherwise Synthea's CSV."""
    if "--fhir" in args:
        from .fhir import describe, load_fhir_patients
        i = args.index("--fhir")
        if i + 1 >= len(args):
            raise SystemExit("--fhir needs a file or folder: --fhir data/synthea/fhir")
        try:
            patients, stats = load_fhir_patients(args[i + 1])
        except FileNotFoundError as exc:
            raise SystemExit(str(exc))
        print(describe(stats))
        left_out = f", {stats['deceased_left_out']} deceased left out" if stats["deceased_left_out"] else ""
        return patients, f"FHIR files from {args[i + 1]} ({stats['patients']} patients from {stats['files']} files{left_out})"
    csv_path = Path(default_csv)
    if csv_path.exists():
        return load_synthea_patients(csv_path), f"Synthea CSV file {csv_path}"
    return [], ""


def patients_from_args(args: list[str], default_csv: str | Path = "data/synthea/csv/patients.csv") -> list[Patient]:
    return load_patients(args, default_csv)[0]
