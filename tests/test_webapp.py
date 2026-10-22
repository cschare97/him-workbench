import re
from pathlib import Path

import pytest

pytest.importorskip("flask")

from him_workbench import audit
from him_workbench.icd10 import ICD10Index
from him_workbench.matching import Patient
from him_workbench.webapp import create_app

PTS = [Patient("1", "Robert", "Alvarado", "1980-03-14", "M", "90210"),
       Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210"),
       Patient("3", "Maria", "Okafor", "1991-11-02", "F", "60614")]
HDR = {"Host": "127.0.0.1:5050"}


def scripted(*replies):
    it = iter(replies)
    return lambda messages, tools: next(it)


def make(tmp_path, replies=(), **kw):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("A000    Test condition alpha\nA001    Test condition beta\n")
    icd = ICD10Index.from_file(p)
    return create_app(icd=icd, patients=PTS, model="fake",
                      chat_factory=lambda m: scripted(*replies),
                      texture_dir=tmp_path / "tex", models_lister=lambda: ["fake"], **kw)


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def client(tmp_path):
    return make(tmp_path).test_client()


@pytest.mark.parametrize("path", ["/patients", "/duplicates", "/quality", "/codes?q=alpha",
                                  "/audit", "/models", "/sources", "/about"])
def test_pages_load_with_banner(client, path):
    r = client.get(path, headers=HDR)
    assert r.status_code == 200 and b"Synthetic data only" in r.data
    assert r.headers["Cache-Control"] == "no-store" and "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_duplicates_page_shows_the_pair(client):
    assert b"Robert Alvarado and Bob Alvarado" in client.get("/duplicates", headers=HDR).data


def test_codes_page_finds_code(client):
    assert b"A00.0" in client.get("/codes?q=alpha", headers=HDR).data


def test_foreign_host_is_refused(client):
    assert client.get("/patients", headers={"Host": "evil.example"}).status_code == 403


def test_post_must_be_json_and_same_origin(client):
    assert client.post("/api/ask", data="question=x", headers=HDR).status_code == 415
    assert client.post("/api/ask", json={"question": "x"},
                       headers={**HDR, "Origin": "http://evil.example"}).status_code == 403


def test_ask_returns_answer_and_raw_tool_lines(tmp_path):
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "search_icd10", "arguments": {"query": "alpha"}}}]}
    c = make(tmp_path, [call, {"role": "assistant", "content": "A00.0 is alpha."}]).test_client()
    r = c.post("/api/ask", json={"question": "alpha?"}, headers=HDR)
    j = r.get_json()
    assert r.status_code == 200 and j["answer"] == "A00.0 is alpha."
    assert j["calls"][0]["tool"] == "search_icd10" and any("A00.0" in l for l in j["calls"][0]["lines"])


def test_ask_rejects_empty_and_long(client):
    assert client.post("/api/ask", json={"question": " "}, headers=HDR).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 501}, headers=HDR).status_code == 400


def test_local_texture_is_used_only_when_present(tmp_path):
    app = make(tmp_path)
    assert b"/local-textures/" not in app.test_client().get("/patients", headers=HDR).data
    (tmp_path / "tex").mkdir()
    (tmp_path / "tex" / "patients.jpg").write_bytes(b"\xff\xd8\xff")
    html = app.test_client().get("/patients", headers=HDR).data
    assert b"/local-textures/patients.jpg" in html
    assert app.test_client().get("/local-textures/secret.jpg", headers=HDR).status_code == 404


def test_no_external_urls_in_ui_files():
    root = Path(__file__).parent.parent / "src" / "him_workbench"
    for f in list((root / "templates").glob("*.html")) + list((root / "static").glob("*")):
        text = f.read_text(encoding="utf-8")
        text = text.replace("http://www.w3.org/2000/svg", "")
        assert not re.search(r"https?://", text), f.name
