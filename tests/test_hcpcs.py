import pytest

from him_workbench import audit
from him_workbench.hcpcs import HCPCSIndex, looks_like_code, normalize_code


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


# Invented stand-in file, same spirit as test_icd10.py's fixture: real-looking code shapes,
# fake descriptions, simple "CODE<whitespace>description" layout.
FIXTURE = """\
E0100   Test cane, with tip
E0105   Test cane, quad or three-prong
E0130   Test walker, rigid, adjustable or fixed height
K0001   Test wheelchair, standard
K0002   Test wheelchair, standard hemi
A4253   Test blood glucose test strips, diabetic
"""


@pytest.fixture
def idx(tmp_path):
    p = tmp_path / "hcpc2099_anweb.txt"
    p.write_text(FIXTURE)
    return HCPCSIndex.from_file(p)


def test_normalize_and_shape():
    assert normalize_code("e0100") == "E0100"
    assert looks_like_code("e0100") and not looks_like_code("wheelchair") and not looks_like_code("E11.9")


def test_loads_plain_text_layout_and_reads_year(idx):
    assert len(idx) == 6 and idx.year == "2099"


def test_lookup(idx):
    assert idx.lookup("e0100").description.startswith("Test cane, with tip")
    assert idx.lookup("Z9999") is None


def test_describe_statuses(idx):
    assert idx.describe("E0100")["status"] == "valid"
    assert idx.describe("Z9999")["status"] == "unknown"


def test_search_all_tokens_and_synonym_group(idx):
    assert [e.code for e in idx.search("standard hemi")] == ["K0002"]
    # "chair" is grouped with "wheelchair" in hcpcs.py's synonym table
    assert {e.code for e in idx.search("chair")} == {"K0001", "K0002"}
    assert idx.search("") == []


def test_search_detail_drops_filler_not_real_words(idx):
    results, ignored, total = idx.search_detail("wheelchair standard please")
    assert ignored == ["please"] and total == 2 and {e.code for e in results} == {"K0001", "K0002"}


def test_did_you_mean_suggests_close_real_words(idx):
    maybe = idx.did_you_mean("wheelchare standard")
    assert "wheelchare" in maybe and "wheelchair" in maybe["wheelchare"]


def test_from_file_rejects_a_file_that_does_not_look_like_hcpcs(tmp_path):
    p = tmp_path / "not_hcpcs.txt"
    p.write_text("this is just some prose, not a code file at all\nnothing here looks like a code\n")
    with pytest.raises(ValueError):
        HCPCSIndex.from_file(p)


# Real lines from a CMS HCPC<year>_..._ANWEB_<date>.txt file (public, not patient data), captured
# byte-for-byte via repr() against the actual October 2026 file. Byte 1-5 = code, byte 6-10 = a
# sequence number by 100s linking a code's continuation lines (100 = primary line, carrying the
# short description/pricing columns after byte 91; 200, 300... = pure description overflow with
# nothing else on the line), byte 11 = one filler byte, byte 12-91 = (a chunk of) the long
# description. G0412/G0413/G0414 below are real multi-line examples; G0412's continuation line is
# included but not its primary line, the way it actually appears mid-file, to make sure a
# continuation line with no open record for its code is safely ignored rather than guessed at.
REAL_ANWEB_LINES = (
    "G0412003004not disrupt the pelvic ring includes internal fixation, when performed\n"
    "G0413001003Percutaneous skeletal fixation of posterior pelvic bone fracture and/or         "
    "Pelvic ring fx tx                                                                                                           "
    "C                                      2002070120020701        N\n"
    "G0413002004dislocation, for fracture patterns which disrupt the pelvic ring, unilateral or\n"
    "G0413003004bilateral, (includes ilium, sacroiliac joint and/or sacrum)\n"
    "G0414001003Open treatment of anterior pelvic bone fracture and/or dislocation for fracture "
    "Pelv ant fx/disloc tx                                                                                                       "
    "C                                      2002070120020701        N\n"
)


def test_from_file_parses_the_real_cms_anweb_fixed_width_layout(tmp_path):
    p = tmp_path / "HCPC2026_OCT_ANWEB_09232026.txt"
    p.write_text(REAL_ANWEB_LINES)
    idx2 = HCPCSIndex.from_file(p)
    assert idx2.year == "2026"
    # G0412's orphan continuation line (no primary line for it in this sample) must not appear.
    assert idx2.lookup("G0412") is None
    assert idx2.lookup("G0413").description == (
        "Percutaneous skeletal fixation of posterior pelvic bone fracture and/or "
        "dislocation, for fracture patterns which disrupt the pelvic ring, unilateral or "
        "bilateral, (includes ilium, sacroiliac joint and/or sacrum)"
    )
    assert idx2.lookup("G0414").description.startswith(
        "Open treatment of anterior pelvic bone fracture and/or dislocation for fracture")


def test_from_file_skips_the_non_conforming_example_rows_at_the_top_of_the_real_file(tmp_path):
    # These sit ahead of the real data in the actual CMS export, offset by 3 leading spaces from
    # where the layout says the code belongs, with placeholder text ("Dressing for N wounds") —
    # not real codes. They must be excluded, not crash the parser or get treated as real entries.
    bogus = "   A1001007Dressing for one wound                                                          \n"
    real = REAL_ANWEB_LINES
    p = tmp_path / "HCPC2026_OCT_ANWEB_09232026.txt"
    p.write_text(bogus * 12 + real)  # pad so the fixed-width path still wins over the >=10 floor
    idx2 = HCPCSIndex.from_file(p)
    assert idx2.lookup("A1001") is None
    assert idx2.lookup("G0413") is not None


def test_from_file_detects_a_csv_layout_with_header(tmp_path):
    p = tmp_path / "hcpc2099_csv.csv"
    p.write_text("HCPC,SEQNUM,LONG DESCRIPTION,SHORT DESCRIPTION\n"
                 "E0100,0,Test cane with tip detailed,Test cane\n"
                 "K0001,0,Test wheelchair standard detailed,Test wheelchair\n")
    idx2 = HCPCSIndex.from_file(p)
    assert len(idx2) == 2 and idx2.lookup("E0100").description == "Test cane with tip detailed"
