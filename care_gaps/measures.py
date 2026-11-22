"""Simplified versions of three public diabetes eCQMs (2026 versions).

Sources (ecqi.healthit.gov):
  CMS122v14  Diabetes: Glycemic Status Assessment Greater Than 9%  (inverse measure)
  CMS131v14  Diabetes: Eye Exam
  CMS951v4   Kidney Health Evaluation

These are teaching versions, not certified measure logic. Simplifications
are listed in SIMPLIFICATIONS so nobody mistakes this for the official spec.
"""
import re
from dataclasses import dataclass
from datetime import date

from fhir_load import (
    A1C_LOINC, DIABETES_CODES, EGFR_LOINC, HOSPICE_PALLIATIVE_WORDS,
    RETINAL_EXAM_SNOMED, UACR_LOINC,
)

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 12, 31)
OUTPATIENT_CLASSES = {"AMB", "VR"}  # ambulatory and virtual visits

SIMPLIFICATIONS = [
    "A qualifying visit is any ambulatory or virtual encounter in the period, "
    "not the exact visit value sets in the spec.",
    "Exclusions implemented: hospice and palliative care (all three), plus ESRD and CKD "
    "stage 5 for CMS951. Not implemented: the ages 66+ frailty/advanced-illness and "
    "long-term nursing home exclusions (CMS122, CMS131) and bilateral absence of eyes (CMS131).",
    "Patients who died before the end of the period are left out.",
    "Codes are matched on a few common Synthea codes, not full VSAC value sets.",
    "Glucose management indicator (GMI) is not used for CMS122, only HbA1c.",
    "CMS131 counts retinal or dilated eye exams only. The spec also accepts autonomous AI "
    "eye exams and retinal exam findings, which are not checked.",
    "CMS951 counts a uACR result only. The spec also accepts separate urine albumin and "
    "urine creatinine results within four days of each other, which Synthea does not produce.",
    "Gap types (care, documentation, open order) are this project's own triage labels, "
    "not part of any eCQM. A documentation gap comes from a keyword match in a note, "
    "so it means 'worth a chart review', not proof the care happened.",
]

# Gap types. Only set when the result is a care gap.
CARE = "care"                    # the care didn't happen, or the result is out of range
DOCUMENTATION = "documentation"  # a note suggests it happened, but nothing structured is on file
OPEN_ORDER = "open order"        # it was ordered in the period but no result came back

A1C_NOTE = re.compile(r"\b(hb)?a1c\b|hemoglobin a1c", re.IGNORECASE)
EYE_NOTE = re.compile(r"retinal (eye )?exam|dilated eye exam|eye exam", re.IGNORECASE)


@dataclass
class MeasureResult:
    measure: str
    status: str   # "met", "not met", "excluded" or "not in denominator"
    reason: str
    kind: str = ""  # gap type when this result is a care gap, else ""


def _note_mention(facts, pattern):
    """Most recent date in the period on which a note matches the pattern, or None."""
    days = [day for day, text in facts.notes if _in_period(day) and pattern.search(text)]
    return max(days) if days else None


def _open_order(facts, code):
    """Most recent date in the period on which this code was ordered, or None."""
    days = [day for c, day, _status in facts.orders if c == code and _in_period(day)]
    return max(days) if days else None


def _in_period(day):
    return day is not None and PERIOD_START <= day <= PERIOD_END


def has_diabetes(facts, by=PERIOD_END):
    """Active type 1 or type 2 diabetes diagnosed on or before `by` and not resolved before the period."""
    for c in facts.conditions:
        if c.code not in DIABETES_CODES:
            continue
        started = c.onset is None or c.onset <= by
        still_active = c.abated is None or c.abated >= PERIOD_START
        if started and still_active:
            return True
    return False


# CMS951 exclusions. Matched by SNOMED code, or by name for other codings of the same disorder.
ESRD_CODES = {"46177005"}      # End-stage renal disease
CKD5_CODES = {"433146000"}     # Chronic kidney disease stage 5
KIDNEY_EXCLUSION_WORDS = ("end-stage renal disease", "end stage renal disease", "chronic kidney disease stage 5")


def _kidney_exclusion(facts):
    """Name of an ESRD or CKD stage 5 diagnosis active during the period, or None."""
    for c in facts.conditions:
        named = any(w in c.display.lower() for w in KIDNEY_EXCLUSION_WORDS)
        if c.code not in ESRD_CODES | CKD5_CODES and not named:
            continue
        started = c.onset is None or c.onset <= PERIOD_END
        still_active = c.abated is None or c.abated >= PERIOD_START
        if started and still_active:
            return c.display or c.code
    return None


def _has_outpatient_visit(facts):
    return any(cls in OUTPATIENT_CLASSES and _in_period(day)
               for cls, _display, day in facts.encounters)


def _in_hospice_or_palliative(facts):
    texts = [d for _c, d, day in facts.procedures if _in_period(day)]
    texts += [d for _cls, d, day in facts.encounters if _in_period(day)]
    return any(w in t.lower() for t in texts for w in HOSPICE_PALLIATIVE_WORDS)


def denominator_check(facts, max_age=75, diabetes_at_start=False):
    """Denominator shared by the measures, with each spec's own age range and diabetes timing.

    CMS122 and CMS131: ages 18-75 by the end of the period, diabetes during the period.
    CMS951: ages 18-85, diabetes already diagnosed at the start of the period.
    Returns (in_denominator, reason).
    """
    if facts.deceased and facts.deceased <= PERIOD_END:
        return False, "died before the end of the measurement period"
    age = facts.age_on(PERIOD_END)
    if not 18 <= age <= max_age:
        return False, f"age {age} at end of period is outside 18-{max_age}"
    if not has_diabetes(facts, by=PERIOD_START if diabetes_at_start else PERIOD_END):
        if diabetes_at_start and has_diabetes(facts):
            return False, "diabetes was diagnosed during the period, not before its start"
        return False, "no active type 1 or type 2 diabetes diagnosis"
    if not _has_outpatient_visit(facts):
        return False, "no outpatient visit in the measurement period"
    return True, f"diabetic, age 18-{max_age}, seen in the period"


def _results_in_period(facts, code):
    return [o for o in facts.observations if o.code == code and _in_period(o.day)]


def cms122(facts):
    """Inverse measure: 'met' means the patient FAILS (A1c > 9% or missing)."""
    name = "CMS122 A1c >9%"
    ok, why = denominator_check(facts)
    if not ok:
        return MeasureResult(name, "not in denominator", why)
    if _in_hospice_or_palliative(facts):
        return MeasureResult(name, "excluded", "hospice or palliative care in the period")
    a1cs = [o for o in _results_in_period(facts, A1C_LOINC) if o.value is not None]
    if not a1cs:
        missing = "no structured A1c result in the period (a missing result counts as >9%)"
        noted = _note_mention(facts, A1C_NOTE)
        if noted:
            return MeasureResult(name, "met", f"{missing}, but a note on {noted} mentions an A1c; "
                                 "review the chart and enter it as a structured result", DOCUMENTATION)
        ordered = _open_order(facts, A1C_LOINC)
        if ordered:
            return MeasureResult(name, "met", f"{missing}; an A1c was ordered on {ordered} "
                                 "but no result came back", OPEN_ORDER)
        return MeasureResult(name, "met", missing, CARE)
    latest_day = max(o.day for o in a1cs)
    # Spec: if several results share the latest date, use the lowest.
    latest = min(o.value for o in a1cs if o.day == latest_day)
    if latest > 9.0:
        return MeasureResult(name, "met", f"most recent A1c {latest}% on {latest_day} is above 9%", CARE)
    return MeasureResult(name, "not met", f"most recent A1c {latest}% on {latest_day}")


def _has_retinopathy(facts):
    return any("retinopathy" in c.display.lower() for c in facts.conditions
               if c.onset is None or c.onset <= PERIOD_END)


def cms131(facts):
    name = "CMS131 Eye exam"
    ok, why = denominator_check(facts)
    if not ok:
        return MeasureResult(name, "not in denominator", why)
    if _in_hospice_or_palliative(facts):
        return MeasureResult(name, "excluded", "hospice or palliative care in the period")
    exams = sorted(day for code, _d, day in facts.procedures
                   if code == RETINAL_EXAM_SNOMED and day is not None)
    in_period = [d for d in exams if _in_period(d)]
    if in_period:
        return MeasureResult(name, "met", f"retinal exam on {in_period[-1]}")
    # Without retinopathy, an exam in the year before the period also counts.
    prior_year = [d for d in exams if PERIOD_START.replace(year=PERIOD_START.year - 1) <= d < PERIOD_START]
    if prior_year and not _has_retinopathy(facts):
        return MeasureResult(name, "met",
                             f"no retinopathy and a retinal exam last year on {prior_year[-1]}")
    missing = "no structured retinal exam on file for the period"
    noted = _note_mention(facts, EYE_NOTE)
    if noted:
        return MeasureResult(name, "not met", f"{missing}, but a note on {noted} mentions an eye exam; "
                             "request the outside report and enter it", DOCUMENTATION)
    return MeasureResult(name, "not met", missing, CARE)


def cms951(facts):
    name = "CMS951 Kidney health"
    ok, why = denominator_check(facts, max_age=85, diabetes_at_start=True)
    if not ok:
        return MeasureResult(name, "not in denominator", why)
    if _in_hospice_or_palliative(facts):
        return MeasureResult(name, "excluded", "hospice or palliative care in the period")
    kidney = _kidney_exclusion(facts)
    if kidney:
        return MeasureResult(name, "excluded", f"{kidney} active during the period")
    has_egfr = bool(_results_in_period(facts, EGFR_LOINC))
    has_uacr = bool(_results_in_period(facts, UACR_LOINC))
    if has_egfr and has_uacr:
        return MeasureResult(name, "met", "eGFR and urine albumin/creatinine ratio both in the period")
    missing = [label for label, present in (("eGFR", has_egfr), ("uACR", has_uacr)) if not present]
    return MeasureResult(name, "not met", "missing in the period: " + ", ".join(missing), CARE)


MEASURES = (cms122, cms131, cms951)


def evaluate(facts):
    return [measure(facts) for measure in MEASURES]
