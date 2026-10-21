import pytest

from him_workbench import audit, matching
from him_workbench.matching import Patient, compare, find_duplicates, jaro_winkler, soundex


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


# All names below are invented test data.
BASE = Patient("1", "Robert", "Alvarado", "1980-03-14", "M", "90210", "555-010-1234")


def test_known_string_metrics():
    assert soundex("Robert") == "R163"
    assert soundex("Rupert") == "R163"
    assert jaro_winkler("martha", "marhta") == pytest.approx(0.9611, abs=1e-3)


def test_nickname_same_person_is_match():
    other = Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210", "5550101234")
    assert compare(BASE, other).label == "match"


def test_typo_in_last_name_still_match():
    other = Patient("2", "Robert", "Alvarodo", "1980-03-14", "M", "90210", "")
    assert compare(BASE, other).label == "match"


def test_dob_month_day_swap_goes_to_review_or_match():
    other = Patient("2", "Robert", "Alvarado", "1980-14-03", "M", "90210", "")
    assert compare(BASE, other).label in ("review", "match")


def test_different_people_non_match():
    other = Patient("2", "Maria", "Okafor", "1991-11-02", "F", "60614", "5550999999")
    assert compare(BASE, other).label == "non-match"


def test_twins_not_auto_matched():
    a = Patient("1", "Jordan", "Lee", "2005-06-01", "M", "30301", "")
    b = Patient("2", "Jaden", "Lee", "2005-06-01", "M", "30301", "")
    assert compare(a, b).label != "match"


def test_missing_fields_add_no_evidence():
    a = Patient("1", "Robert", "Alvarado", "1980-03-14")
    b = Patient("2", "Robert", "Alvarado", "1980-03-14")
    r = compare(a, b)
    assert r.detail["sex"] == 0 and r.detail["zip"] == 0 and r.detail["phone"] == 0


def test_find_duplicates_blocking_and_audit(tmp_path):
    pts = [
        BASE,
        Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210", ""),
        Patient("3", "Maria", "Okafor", "1991-11-02", "F", "60614", ""),
    ]
    res = find_duplicates(pts)
    assert [(r.a, r.b) for r in res] == [("1", "2")]
    assert (tmp_path / "audit.jsonl").exists()


def test_year_typo_is_found_by_blocking():
    pts = [
        BASE,
        Patient("2", "Robert", "Alvarado", "1981-03-14", "M", "90210", ""),
    ]
    res = find_duplicates(pts)
    assert [(r.a, r.b) for r in res] == [("1", "2")]


def test_parent_child_same_name_never_auto_match():
    parent = Patient("1", "Robert", "Alvarado", "1955-03-14", "M", "90210", "")
    child = Patient("2", "Robert", "Alvarado", "1985-03-14", "M", "90210", "")
    assert compare(parent, child).label != "match"


def test_two_errors_still_paired():
    pts = [
        BASE,
        Patient("2", "Robert", "Alvarodo", "1981-03-14", "M", "90210", ""),
    ]
    assert len(find_duplicates(pts)) == 1


def test_twins_with_similar_names_not_auto_matched():
    a = Patient("1", "Jaden", "Lee", "2005-06-01", "M", "30301", "")
    b = Patient("2", "Jayden", "Lee", "2005-06-01", "M", "30301", "")
    assert compare(a, b).label != "match"


def test_review_label_always_has_a_reason():
    twin_a = Patient("1", "Jaden", "Lee", "2005-06-01", "M", "30301", "")
    twin_b = Patient("2", "Jayden", "Lee", "2005-06-01", "M", "30301", "")
    r = compare(twin_a, twin_b)
    assert r.label == "review" and any("first names" in w for w in r.reasons)
    parent = Patient("1", "Robert", "Alvarado", "1955-03-14", "M", "90210", "")
    child = Patient("2", "Robert", "Alvarado", "1985-03-14", "M", "90210", "")
    assert any("birth" in w for w in compare(parent, child).reasons)
    assert compare(BASE, Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210", "")).reasons == []
