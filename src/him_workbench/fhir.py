"""Read FHIR Patient resources from local files into the workbench's Patient records.

Local only, no network. Accepts what FHIR tools commonly write:
  - one Patient resource (.json)
  - a Bundle of resources (.json), such as Synthea's FHIR export (one bundle per patient)
  - NDJSON (.ndjson), one resource per line, as in the FHIR Bulk Data export
Anything that is not a Patient is skipped. Nothing is skipped silently: the second return value
counts what was read, used and skipped.

Field mapping (US Core Patient): id; name (official, then usual, then first listed) -> first = given[0],
last = family; birthDate (partial dates kept as written); gender male/female -> M/F, other/unknown -> blank
(so it never counts as a conflict); address[].postalCode -> zip; telecom (system=phone) -> phone.
Deceased patients are left out unless include_deceased=True, same as the CSV loader.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import audit
from .matching import Patient

_SEX = {"male": "M", "female": "F"}
_NAME_RANK = {"official": 0, "usual": 1}
_SKIP_USE = {"old", "maiden", "anonymous"}


def _pick_name(names: list) -> dict | None:
    usable = [n for n in names if isinstance(n, dict) and n.get("family") and n.get("given")
              and n.get("use") not in _SKIP_USE]
    return min(usable, key=lambda n: _NAME_RANK.get(n.get("use"), 2)) if usable else None


def _first(items, key: str, where=None) -> str:
    for it in items or []:
        if isinstance(it, dict) and it.get(key) and (where is None or where(it)):
            return str(it[key])
    return ""


def patient_from_fhir(res: dict, include_deceased: bool = False) -> tuple[Patient | None, str]:
    """Return (Patient, "") or (None, reason) where reason is 'not_patient', 'deceased' or 'no_name'."""
    if not isinstance(res, dict) or res.get("resourceType") != "Patient":
        return None, "not_patient"
    if not include_deceased and (res.get("deceasedBoolean") is True or res.get("deceasedDateTime")):
        return None, "deceased"
    name = _pick_name(res.get("name") or [])
    if not name or not res.get("id"):
        return None, "no_name"
    given = name["given"]
    first = given[0] if isinstance(given, list) and given else str(given)
    return Patient(
        id=str(res["id"]),
        first=first,
        last=name["family"],
        dob=str(res.get("birthDate") or ""),
        sex=_SEX.get(str(res.get("gender", "")).lower(), ""),
        zip=_first(res.get("address"), "postalCode"),
        phone=_first(res.get("telecom"), "value", where=lambda t: t.get("system") == "phone"),
    ), ""


def _resources(path: Path):
    """Yield every resource in one file. Raises ValueError/OSError on a file that cannot be read."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".ndjson":
        for line in text.splitlines():
            if line.strip():
                yield json.loads(line)
        return
    data = json.loads(text)
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        for entry in data.get("entry") or []:
            if isinstance(entry, dict) and isinstance(entry.get("resource"), dict):
                yield entry["resource"]
    else:
        yield data


def load_fhir_patients(path: str | Path, include_deceased: bool = False) -> tuple[list[Patient], dict]:
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.suffix.lower() in (".json", ".ndjson") and p.is_file())
    elif path.is_file():
        files = [path]
    else:
        raise FileNotFoundError(f"{path} not found")
    stats = {"files": len(files), "patients": 0, "deceased_left_out": 0, "no_name_or_id": 0, "unreadable_files": 0}
    out: list[Patient] = []
    seen: set[str] = set()
    for f in files:
        try:
            for res in _resources(f):
                p, why = patient_from_fhir(res, include_deceased)
                if p:
                    if p.id not in seen:  # the same patient can appear in more than one file
                        seen.add(p.id)
                        out.append(p)
                elif why == "deceased":
                    stats["deceased_left_out"] += 1
                elif why == "no_name":
                    stats["no_name_or_id"] += 1
        except (ValueError, OSError):
            stats["unreadable_files"] += 1
    stats["patients"] = len(out)
    audit.log_call("fhir_load", {"files": stats["files"]}, f"{stats['patients']} patients")
    return out, stats


def describe(stats: dict) -> str:
    """One plain line for the terminal."""
    s = (f"FHIR: {stats['patients']} patients from {stats['files']} file(s)")
    extras = [f"{stats[k]} {label}" for k, label in (
        ("deceased_left_out", "deceased left out"), ("no_name_or_id", "without a usable name or id"),
        ("unreadable_files", "unreadable files")) if stats[k]]
    return s + (f" ({', '.join(extras)})" if extras else "")
