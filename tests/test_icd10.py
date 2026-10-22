import pytest

from him_workbench import audit
from him_workbench.icd10 import ICD10Index, format_code, looks_like_code, normalize_code


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


# Invented stand-in file: real-looking code shapes, fake descriptions.
FIXTURE = """\
A000    Test condition alpha, first type
A001    Test condition alpha, second type
B12     Test condition bravo
C3401   Test condition charlie of left side
C3402   Test condition charlie of right side
C341    Test condition charlie, unspecified
"""


@pytest.fixture
def idx(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text(FIXTURE)
    return ICD10Index.from_file(p)


def test_normalize_and_format():
    assert normalize_code("c34.01") == "C3401"
    assert format_code("C3401") == "C34.01"
    assert format_code("A00") == "A00"
    assert looks_like_code("a00.0") and not looks_like_code("diabetes")


def test_loads_and_reads_year(idx):
    assert len(idx) == 6 and idx.year == "2099"


def test_lookup_with_and_without_dot(idx):
    assert idx.lookup("C34.01").description.startswith("Test condition charlie of left")
    assert idx.lookup("c3401") is not None
    assert idx.lookup("Z999") is None


def test_describe_statuses(idx):
    assert idx.describe("A00.0")["status"] == "valid"
    cat = idx.describe("C34")
    assert cat["status"] == "category" and cat["more_specific_count"] == 3
    assert idx.describe("Z99")["status"] == "unknown"


def test_search_all_tokens_and_ranking(idx):
    res = idx.search("charlie left")
    assert [e.code for e in res] == ["C3401"]
    assert {e.code for e in idx.search("alpha")} == {"A000", "A001"}
    assert idx.search("") == []


def test_wrong_file_is_rejected(tmp_path):
    p = tmp_path / "order.txt"
    p.write_text("00001 A00 0 Short text   Long text here\n" * 5)
    with pytest.raises(ValueError):
        ICD10Index.from_file(p)


def test_audit_never_logs_query_text(idx, tmp_path):
    idx.search("charlie left")
    log = (tmp_path / "audit.jsonl").read_text()
    assert "charlie" not in log and "query_chars" in log


SYN_FIXTURE = """\
A100    Test chronic renal condition
A101    Test kidney condition direct
A102    Test hypertension condition
A103    Test myocardial infarction condition
A104    Test unrelated condition
"""


@pytest.fixture
def syn_idx(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text(SYN_FIXTURE)
    return ICD10Index.from_file(p)


def test_synonym_kidney_finds_renal_and_direct_ranks_first(syn_idx):
    codes = [e.code for e in syn_idx.search("kidney")]
    assert codes == ["A101", "A100"]


def test_synonym_off_is_literal(syn_idx):
    assert [e.code for e in syn_idx.search("kidney", synonyms=False)] == ["A101"]


def test_phrase_synonyms(syn_idx):
    assert [e.code for e in syn_idx.search("high blood pressure")] == ["A102"]
    assert [e.code for e in syn_idx.search("heart attack")] == ["A103"]


def test_no_false_synonym_hits(syn_idx):
    assert syn_idx.search("liver") == []


def test_typed_word_is_the_direct_term(syn_idx):
    # typing "renal" should rank the renal entry above the kidney-only entry
    assert [e.code for e in syn_idx.search("renal")] == ["A100", "A101"]


DIAB_FIXTURE = """\
E110    Test diabetes type 2 with hyperosmolarity
E1121   Test diabetes type 2 with diabetic nephropathy kidney
E1122   Test diabetes type 2 with diabetic chronic kidney disease
E1129   Test diabetes type 2 with other diabetic kidney complication
E1165   Test diabetes type 2 with hyperglycemia
"""


@pytest.fixture
def diab_idx(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text(DIAB_FIXTURE)
    return ICD10Index.from_file(p)


def test_strict_search_misses_natural_phrase(diab_idx):
    assert diab_idx.search("type 2 diabetes kidney complications") == []


def test_relaxed_drops_filler_word_and_says_so(diab_idx):
    res, ignored = diab_idx.search_relaxed("type 2 diabetes kidney complications")
    assert {e.code for e in res} == {"E1121", "E1122", "E1129"}
    assert ignored == ["complications"]


def test_relaxed_drops_word_that_matches_nothing(diab_idx):
    res, ignored = diab_idx.search_relaxed("diabetes kidney xyzzy")
    assert {e.code for e in res} == {"E1121", "E1122", "E1129"} and ignored == ["xyzzy"]


def test_relaxed_leaves_good_searches_alone(diab_idx):
    res, ignored = diab_idx.search_relaxed("diabetes kidney")
    assert len(res) == 3 and ignored == []


def test_relaxed_gives_up_cleanly(diab_idx):
    assert diab_idx.search_relaxed("xyzzy plugh") == ([], [])
    assert diab_idx.search_relaxed("") == ([], [])


def test_relaxed_never_drops_a_real_word(diab_idx):
    # real words that never appear together: say nothing found, do not answer for part of the question
    assert diab_idx.search_relaxed("hyperglycemia nephropathy") == ([], [])
    assert diab_idx.search_relaxed("diabetes kidney hyperglycemia") == ([], [])


HBP_FIXTURE = """\
K766    Test portal hypertension
P292    Test neonatal hypertension
I150    Test renovascular hypertension
I10     Test essential (primary) hypertension
"""


def test_high_blood_pressure_puts_the_essential_code_first(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text(HBP_FIXTURE)
    idx = ICD10Index.from_file(p)
    assert idx.search("high blood pressure")[0].code == "I10"
    assert idx.search_relaxed("high blood pressure")[0][0].code == "I10"


def test_search_detail_reports_total_beyond_the_limit(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text(HBP_FIXTURE)
    idx = ICD10Index.from_file(p)
    results, ignored, total = idx.search_detail("hypertension", limit=2)
    assert len(results) == 2 and total == 4 and ignored == []


def test_question_wrapper_words_are_dropped_and_reported(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("N300    Test acute cystitis\nN301    Test interstitial cystitis\nZ001    Test code for diagnosis follow up\n")
    idx = ICD10Index.from_file(p)
    res, ignored, total = idx.search_detail("diagnosis code for cystitis")
    assert {e.code for e in res} == {"N300", "N301"} and total == 2
    assert set(ignored) == {"diagnosis", "code", "for"}
    # a query that already matches is left alone, even with these words in it
    assert [e.code for e in idx.search_detail("diagnosis code")[0]] == ["Z001"]


def test_biggest_matching_category_ranks_first(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("A0681  Test amebic cystitis\nN3000  Test acute cystitis without hematuria\n"
                 "N3001  Test acute cystitis with hematuria\nN3090  Test cystitis, unspecified\n")
    idx = ICD10Index.from_file(p)
    assert [e.code for e in idx.search("cystitis")][-1] == "A0681"
