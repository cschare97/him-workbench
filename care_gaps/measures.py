"""Simplified versions of three public diabetes eCQMs (2026 versions).

Sources (ecqi.healthit.gov):
  CMS122v14  Diabetes: Glycemic Status Assessment Greater Than 9%  (inverse measure)
  CMS131v14  Diabetes: Eye Exam
  CMS951v4   Kidney Health Evaluation

These are teaching versions, not certified measure logic. Simplifications
are listed in SIMPLIFICATIONS so nobody mistakes this for the official spec.
"""
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
    "Exclusions cover hospice and palliative care only. The frailty and "
    "advanced-illness exclusion for ages 66+ is not implemented.",
    "Patients who died before the end of the period are left out.",
    "Codes are matched on a few common Synthea codes, not full VSAC value sets.",
    "Glucose management indicator (GMI) is not used for CMS122, only HbA1c.",
]


@dataclass
class MeasureResult:
    measure: str
    status: str   # "met", "not met", "excluded" or "not in denominator"
    reason: str


def _in_period(day):
    return day is not None and PERIOD_START <= day <= PERIOD_END


def has_diabetes(facts):
    for c in facts.conditions:
        if c.code not in DIABETES_CODES:
            continue
        started = c.onset is None or c.onset <= PERIOD_END
        still_active = c.abated is None or c.abated >= PERIOD_START
        if started and still_active:
            return True
    return False


def _has_outpatient_visit(facts):
    return any(cls in OUTPATIENT_CLASSES and _in_period(day)
               for cls, _display, day in facts.encounters)


def _in_hospice_or_palliative(facts):
    texts = [d for _c, d, day in facts.procedures if _in_period(day)]
    texts += [d for _cls, d, day in facts.encounters if _in_period(day)]
    return any(w in t.lower() for t in texts for w in HOSPICE_PALLIATIVE_WORDS)


def denominator_check(facts):
    """Shared denominator for all three measures. Returns (in_denominator, reason)."""
    if facts.deceased and facts.deceased <= PERIOD_END:
        return False, "died before the end of the measurement period"
    age = facts.age_on(PERIOD_END)
    if not 18 <= age <= 75:
        return False, f"age {age} at end of period is outside 18-75"
    if not has_diabetes(facts):
        return False, "no active type 1 or type 2 diabetes diagnosis"
    if not _has_outpatient_visit(facts):
        return False, "no outpatient visit in the measurement period"
    return True, "diabetic, age 18-75, seen in the period"


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
        return MeasureResult(name, "met",
                             "no structured A1c result in the period (a missing result counts as >9%)")
    latest_day = max(o.day for o in a1cs)
    # Spec: if several results share the latest date, use the lowest.
    latest = min(o.value for o in a1cs if o.day == latest_day)
    if latest > 9.0:
        return MeasureResult(name, "met", f"most recent A1c {latest}% on {latest_day} is above 9%")
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
    return MeasureResult(name, "not met", "no structured retinal exam on file for the period")


def cms951(facts):
    name = "CMS951 Kidney health"
    ok, why = denominator_check(facts)
    if not ok:
        return MeasureResult(name, "not in denominator", why)
    if _in_hospice_or_palliative(facts):
        return MeasureResult(name, "excluded", "hospice or palliative care in the period")
    has_egfr = bool(_results_in_period(facts, EGFR_LOINC))
    has_uacr = bool(_results_in_period(facts, UACR_LOINC))
    if has_egfr and has_uacr:
        return MeasureResult(name, "met", "eGFR and urine albumin/creatinine ratio both in the period")
    missing = [label for label, present in (("eGFR", has_egfr), ("uACR", has_uacr)) if not present]
    return MeasureResult(name, "not met", "missing in the period: " + ", ".join(missing))


MEASURES = (cms122, cms131, cms951)


def evaluate(facts):
    return [measure(facts) for measure in MEASURES]
