"""Synthetic clinical notes for the document-search feature.

Not a separate corpus and not real clinical documentation: each note is generated deterministically
from a patient's own loaded FHIR chart (problems, medications, allergies, vitals, most recent visit) —
the same data already shown on that patient's chart page, just written as prose instead of a table.
One note per patient. Local only, no network, no model involved in generating the text itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chart import Chart, active, clean


@dataclass
class Note:
    patient_id: str
    text: str


# Keeps notes short enough to stay well under local embedding models' context windows (and
# readable). A patient with many years of visits can rack up dozens of near-duplicate problem
# entries (e.g. "Chronic kidney disease stage 1/2/3/4" recorded separately at each stage) — dedupe
# handles that; the cap handles the rest (cancer staging, long problem lists, etc).
MAX_LISTED = 20
MAX_NOTE_CHARS = 4000


def _joined(items, fmt, limit: int = MAX_LISTED) -> str:
    seen: list[str] = []
    for i in items:
        v = fmt(i)
        if v and v not in seen:
            seen.append(v)
    if not seen:
        return "none on record"
    shown = seen[:limit]
    extra = len(seen) - len(shown)
    text = ", ".join(shown)
    return f"{text} (+{extra} more)" if extra else text


def generate_note(patient_id: str, chart: Chart, name: str = "") -> Note:
    who = name or patient_id
    problems = _joined(active(chart.problems), lambda p: clean(p.what.display))
    meds = _joined(chart.medications, lambda m: clean(m.what.display))
    allergies = _joined(chart.allergies, lambda a: clean(a.what.display))
    vitals = _joined(chart.vitals[:6], lambda r: f"{clean(r.what.display)} {r.value}".strip())
    visit = chart.visits[0] if chart.visits else None
    visit_line = (f"Most recently seen {visit.start} for {clean(visit.kind) or 'a visit'}."
                  if visit and visit.start else "No visits on record.")
    text = (f"{who}. Active problems: {problems}. Current medications: {meds}. "
            f"Allergies: {allergies}. Recent vitals: {vitals}. {visit_line}")
    # Belt-and-suspenders: even after deduping and capping each list, a handful of very long
    # individual display strings could still add up. Cut the whole note rather than risk an
    # embedding failure over it — a truncated note is still searchable, a dropped one isn't.
    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS].rstrip() + "... [truncated]"
    return Note(patient_id, text)
