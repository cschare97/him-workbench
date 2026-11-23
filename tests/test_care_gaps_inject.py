"""Tests for inject_gaps.py on a small hand-built bundle."""
import random
from datetime import date

from him_workbench.care_gaps.fhir_load import facts_from_bundle
from him_workbench.care_gaps.inject_gaps import apply, eligible, trim
from him_workbench.care_gaps.measures import cms122, cms131


def bundle():
    def e(r):
        return {"fullUrl": "urn:uuid:" + r["id"], "resource": r}
    obs = lambda i, day, v: e({"resourceType": "Observation", "id": f"o{i}", "effectiveDateTime": day,
                               "code": {"coding": [{"code": "4548-4"}]}, "valueQuantity": {"value": v}})
    return {"resourceType": "Bundle", "entry": [
        e({"resourceType": "Patient", "id": "p1", "birthDate": "1970-05-01",
           "name": [{"given": ["Ann12"], "family": "Lee34"}]}),
        e({"resourceType": "Condition", "id": "c1", "onsetDateTime": "2015-01-01",
           "code": {"coding": [{"code": "44054006", "display": "Diabetes mellitus type 2"}]}}),
        e({"resourceType": "Encounter", "id": "e1", "class": {"code": "AMB"},
           "period": {"start": "2026-03-10T09:00:00Z"}, "type": [{"coding": [{"display": "Checkup"}]}]}),
        obs(1, "2026-03-10T09:00:00Z", 7.1), obs(2, "2026-08-10T09:00:00Z", 6.8),
        obs(3, "2025-08-10T09:00:00Z", 7.5),
        e({"resourceType": "Procedure", "id": "x1", "performedDateTime": "2026-04-01T09:00:00Z",
           "code": {"coding": [{"code": "722161008", "display": "Diabetic retinal eye exam"}]}}),
        e({"resourceType": "Claim", "id": "cl1"}),
    ]}


def setup_function():
    random.seed(1)


def test_starts_passing_both():
    f = facts_from_bundle(bundle())
    assert eligible("a1c_in_note", f) and eligible("outside_eye_exam", f)


def test_a1c_in_note_becomes_documentation_gap():
    b = bundle()
    change = apply("a1c_in_note", b)
    r = cms122(facts_from_bundle(b))
    assert r.status == "met" and r.kind == "documentation"
    assert "removed 2" in change and "6.8%" in change  # latest value carried into the note


def test_open_order_becomes_open_order_gap():
    b = bundle()
    apply("open_order", b)
    assert cms122(facts_from_bundle(b)).kind == "open order"


def test_uncontrolled_becomes_care_gap():
    b = bundle()
    apply("uncontrolled", b)
    r = cms122(facts_from_bundle(b))
    assert r.status == "met" and r.kind == "care" and "above 9%" in r.reason


def test_outside_eye_exam_becomes_documentation_gap():
    b = bundle()
    apply("outside_eye_exam", b)
    r = cms131(facts_from_bundle(b))
    assert r.status == "not met" and r.kind == "documentation"


def test_last_year_a1c_is_left_alone():
    b = bundle()
    apply("a1c_in_note", b)
    days = [x["resource"]["effectiveDateTime"][:4] for x in b["entry"]
            if x["resource"]["resourceType"] == "Observation"]
    assert days == ["2025"]


def test_trim_drops_billing_resources():
    b = trim(bundle())
    assert all(x["resource"]["resourceType"] != "Claim" for x in b["entry"])


def test_same_seed_same_result():
    random.seed(5); b1 = bundle(); apply("uncontrolled", b1)
    random.seed(5); b2 = bundle(); apply("uncontrolled", b2)
    assert b1 == b2
