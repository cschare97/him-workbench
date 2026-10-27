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


def test_sources_page_says_where_patients_came_from(tmp_path):
    app = make(tmp_path, patient_source="FHIR files from data/synthea/fhir (3 patients from 5 files)")
    assert b"Read from: FHIR files from data/synthea/fhir" in app.test_client().get("/sources", headers=HDR).data


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


def test_codes_page_finds_a_code_inside_a_sentence(client):
    html = client.get("/codes?q=what+disease+is+code+a00.0", headers=HDR).data
    assert b"A00.0" in html and b"Test condition alpha" in html


def test_about_page_carries_nlm_attribution_only_when_drugs_are_on(tmp_path):
    from him_workbench.rxnorm import RxNormClient
    assert b"National Library of Medicine" not in make(tmp_path).test_client().get("/about", headers=HDR).data
    app = make(tmp_path, drugs=RxNormClient(fetch=lambda u: {}, cache_path=tmp_path / "rx.json"))
    html = app.test_client().get("/about", headers=HDR).data
    assert b"National Library of Medicine" in html and b"does not endorse" in html


def test_about_page_carries_medlineplus_attribution_only_when_education_is_on(tmp_path):
    from him_workbench.medlineplus import MedlinePlusClient
    assert b"MedlinePlus.gov" not in make(tmp_path).test_client().get("/about", headers=HDR).data
    app = make(tmp_path, education=MedlinePlusClient(fetch=lambda u: {}, cache_path=tmp_path / "m.json"))
    assert b"MedlinePlus.gov" in app.test_client().get("/about", headers=HDR).data


def _bundle(pid, name="Asthma (disorder)"):
    return {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Patient", "id": pid}},
        {"resource": {"resourceType": "Condition", "clinicalStatus": {"coding": [{"code": "active"}]},
                      "code": {"coding": [{"code": "195967001", "display": name}]}, "onsetDateTime": "2020-01-05T00:00:00Z"}}]}


def test_chart_page_shows_the_patient_chart_and_patients_link_to_it(tmp_path):
    import json
    from him_workbench.chart import chart_files_from
    d = tmp_path / "fhir"
    d.mkdir()
    (d / "Maria_Okafor_3.json").write_text(json.dumps(_bundle("3")))
    app = make(tmp_path, chart_files=chart_files_from(d))
    c = app.test_client()
    page = c.get("/patients/3/chart", headers=HDR)
    assert page.status_code == 200 and b"Asthma" in page.data and b"(disorder)" not in page.data
    assert b'href="/patients/3/chart"' in c.get("/patients", headers=HDR).data
    assert b'href="/patients/1/chart"' not in c.get("/patients", headers=HDR).data


def test_chart_page_unknown_patient_is_404_and_no_file_is_explained(tmp_path):
    c = make(tmp_path).test_client()
    assert c.get("/patients/nope/chart", headers=HDR).status_code == 404
    assert b"start the page with --fhir" in c.get("/patients/1/chart", headers=HDR).data


def test_chart_page_refuses_a_file_holding_a_different_patient(tmp_path):
    import json
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(_bundle("999")))
    c = make(tmp_path, chart_files={"3": f}).test_client()
    page = c.get("/patients/3/chart", headers=HDR)
    assert b"different patient" in page.data and b"Asthma" not in page.data


def test_chart_page_rejects_a_bad_host(tmp_path):
    assert make(tmp_path).test_client().get("/patients/1/chart", headers={"Host": "evil.example"}).status_code == 403


def _edu_client(tmp_path, calls):
    from him_workbench.medlineplus import MedlinePlusClient
    feed = {"feed": {"entry": [{"title": {"_value": "Sinusitis"}, "link": [{"href": "https://medlineplus.gov/sinusitis.html"}],
                                "summary": {"_value": "<p>Sinus swelling.</p>"}}]}}
    def fetch(url):
        calls.append(url)
        return feed
    return MedlinePlusClient(fetch=fetch, cache_path=tmp_path / "m.json")


def test_learn_page_shows_medlineplus_result_with_attribution_and_what_was_sent(tmp_path):
    calls = []
    app = make(tmp_path, education=_edu_client(tmp_path, calls))
    page = app.test_client().get("/patients/3/learn?kind=problem&code=444814009&name=Viral+sinusitis", headers=HDR)
    assert page.status_code == 200 and b"Sinusitis" in page.data and b"MedlinePlus.gov" in page.data
    assert b"No patient information was sent" in page.data and len(calls) == 1


def test_learn_page_is_off_without_education_and_makes_no_request(tmp_path):
    page = make(tmp_path).test_client().get("/patients/3/learn?kind=problem&code=444814009", headers=HDR)
    assert b"--education" in page.data


def test_learn_page_rejects_bad_input_and_unknown_patients_without_calling_nlm(tmp_path):
    calls = []
    c = make(tmp_path, education=_edu_client(tmp_path, calls)).test_client()
    assert c.get("/patients/nope/learn?kind=problem&code=444814009", headers=HDR).status_code == 404
    assert b"not a problem or drug code" in c.get("/patients/3/learn?kind=problem&code=abc;drop", headers=HDR).data
    assert b"not a problem or drug code" in c.get("/patients/3/learn?kind=lab&code=12345", headers=HDR).data
    assert calls == []


def test_chart_shows_about_links_only_when_education_is_on(tmp_path):
    import json
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(_bundle("3")))
    on = make(tmp_path, chart_files={"3": f}, education=_edu_client(tmp_path, [])).test_client()
    off = make(tmp_path, chart_files={"3": f}).test_client()
    assert b"/patients/3/learn?" in on.get("/patients/3/chart", headers=HDR).data
    assert b"/learn?" not in off.get("/patients/3/chart", headers=HDR).data


def test_ask_with_a_patient_passes_context_and_unknown_patient_is_refused(tmp_path):
    import json
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(_bundle("3")))
    seen = []
    def chat(messages, tools):
        seen.append(([m["content"] for m in messages if m["role"] == "user"], [t["function"]["name"] for t in tools]))
        return {"role": "assistant", "content": "ok"}
    app = create_app(patients=PTS, model="fake", chat_factory=lambda m: chat, texture_dir=tmp_path / "tex",
                     models_lister=lambda: ["fake"], chart_files={"3": f})
    c = app.test_client()
    ok = c.post("/api/ask", json={"question": "what problems?", "patient_id": "3"}, headers=HDR)
    assert ok.status_code == 200 and "Maria Okafor" in seen[0][0][0] and "patient_chart" in seen[0][1]
    assert c.post("/api/ask", json={"question": "x", "patient_id": "nope"}, headers=HDR).status_code == 400
    assert c.post("/api/ask", json={"question": "x", "patient_id": 7}, headers=HDR).status_code == 400


def test_chart_page_points_the_prompt_bar_at_that_patient(tmp_path):
    import json
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(_bundle("3")))
    c = make(tmp_path, chart_files={"3": f}).test_client()
    page = c.get("/patients/3/chart", headers=HDR).data
    assert b'data-patient="3"' in page and b"Ask about Maria&#39;s chart" in page
    assert b"data-patient" not in c.get("/patients", headers=HDR).data


def test_ask_about_a_patient_shows_the_raw_chart_next_to_the_answer(tmp_path):
    import json
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(_bundle("3")))
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "patient_chart", "arguments": {}}}]},
                    {"role": "assistant", "content": "One active problem: asthma."}])
    app = create_app(patients=PTS, model="fake", chat_factory=lambda m: (lambda messages, tools: next(replies)),
                     texture_dir=tmp_path / "tex", models_lister=lambda: ["fake"], chart_files={"3": f})
    r = app.test_client().post("/api/ask", json={"question": "problems?", "patient_id": "3"}, headers=HDR).get_json()
    call = r["calls"][0]
    assert call["tool"] == "patient_chart" and "Chart for Maria Okafor" in call["headline"]
    assert any("active: Asthma [195967001]" in line for line in call["lines"])
    assert r["answer"] == "One active problem: asthma."


def test_chart_page_shows_social_history_and_questionnaires(tmp_path):
    import json
    b = _bundle("3")
    b["entry"] += [
        {"resource": {"resourceType": "Observation", "category": [{"coding": [{"code": "social-history"}]}],
                      "code": {"coding": [{"code": "72166-2", "display": "Tobacco smoking status"}]},
                      "valueCodeableConcept": {"coding": [{"display": "Never smoker"}]}, "effectiveDateTime": "2026-02-03T00:00:00Z"}},
        {"resource": {"resourceType": "Observation", "category": [{"coding": [{"code": "survey"}]}],
                      "code": {"coding": [{"code": "44261-6", "display": "PHQ-9 total score"}]},
                      "valueQuantity": {"value": 3, "unit": "{score}"}, "effectiveDateTime": "2026-02-03T00:00:00Z"}}]
    f = tmp_path / "Maria_Okafor_3.json"
    f.write_text(json.dumps(b))
    page = make(tmp_path, chart_files={"3": f}).test_client().get("/patients/3/chart", headers=HDR).data
    assert b"Social history" in page and b"Never smoker" in page
    assert b"Questionnaires and survey answers" in page and b"PHQ-9 total score" in page and b"3 {score}" in page


def _roi_app(tmp_path):
    from datetime import date
    from him_workbench.roi import RoiStore
    return make(tmp_path, roi_store=RoiStore(":memory:", days=30), today=lambda: date(2026, 10, 24))


def test_roi_page_log_verify_and_refuse_out_of_order(tmp_path):
    c = _roi_app(tmp_path).test_client()
    page = c.get("/roi", headers=HDR).data
    assert b"ROI requests" in page and b"Log a request" in page and b"Maria Okafor" in page
    r = c.post("/roi/new", json={"patient_id": "3", "requester_type": "attorney", "summary": "visit notes",
                                 "received": "2026-10-01"}, headers=HDR)
    assert r.status_code == 200 and r.get_json()["id"] == 1
    page = c.get("/roi", headers=HDR).data
    assert b"2026-10-31" in page and b"Maria Okafor" in page and b"days left" in page
    bad = c.post("/roi/1/status", json={"status": "fulfilled"}, headers=HDR)
    assert bad.status_code == 400 and "verified" in bad.get_json()["error"]
    assert c.post("/roi/1/status", json={"status": "verified", "note": "ID checked"}, headers=HDR).status_code == 200
    detail = c.get("/roi/1", headers=HDR).data
    assert b"ID checked" in detail and b"Verified" in detail


def test_roi_rejects_unknown_patient_bad_input_and_non_json(tmp_path):
    c = _roi_app(tmp_path).test_client()
    r = c.post("/roi/new", json={"patient_id": "nope", "requester_type": "attorney", "summary": "x"}, headers=HDR)
    assert r.status_code == 400 and "Pick a patient" in r.get_json()["error"]
    r = c.post("/roi/new", json={"patient_id": "3", "requester_type": "attorney", "summary": ""}, headers=HDR)
    assert r.status_code == 400
    assert c.post("/roi/new", data="x=1", headers=HDR).status_code == 415
    assert c.get("/roi/99", headers=HDR).status_code == 404


def test_roi_overdue_is_shown_and_extension_works(tmp_path):
    c = _roi_app(tmp_path).test_client()
    c.post("/roi/new", json={"patient_id": "3", "requester_type": "insurer", "summary": "labs",
                             "received": "2026-09-01"}, headers=HDR)
    assert b"overdue by 23 days" in c.get("/roi", headers=HDR).data
    assert c.post("/roi/1/extend", json={"reason": "late"}, headers=HDR).status_code == 400
    c.post("/roi/new", json={"patient_id": "3", "requester_type": "insurer", "summary": "labs",
                             "received": "2026-10-10"}, headers=HDR)
    assert c.post("/roi/2/extend", json={"reason": "large record"}, headers=HDR).status_code == 200
    assert b"extended" in c.get("/roi/2", headers=HDR).data


# ---- security edge cases: these assert the app behaves correctly under adversarial-shaped input,
# not that it fails. Written as ordinary pytest assertions, same as the rest of this file. ----

def test_post_with_no_origin_header_still_requires_correct_host(client):
    # The before_request guard only checks Origin when the header is present (webapp.py: "if origin and ...").
    # A request with Host correct but Origin header absent entirely should pass the host/origin guard and
    # reach the route handler (here failing later for an unrelated reason: the fake chat backend errors,
    # which is a 502 from inside api_ask, proving the guard itself let the request through).
    assert client.post("/api/ask", json={"question": "x"}, headers=HDR).status_code != 403
    # Host wrong must still be refused regardless of Origin being present or absent.
    assert client.post("/api/ask", json={"question": "x"},
                       headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("host", [
    "127.0.0.1:5050@evil.example",   # userinfo-style prefix
    "127.0.0.1.evil.example",         # subdomain-style suffix
    "evil.example#127.0.0.1",
    "evil.example\t127.0.0.1",
    " 127.0.0.1",
])
def test_host_header_lookalikes_are_refused(client, host):
    # _host_ok() must not be tricked into reading "127.0.0.1" out of a Host value that doesn't
    # actually mean localhost. Every one of these should be refused, same as a plain wrong host.
    assert client.get("/patients", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("name", [
    "..%2f..%2f..%2fetc%2fpasswd",
    "../../../etc/passwd",
    "....//....//etc/passwd",
    "patients.jpg/../../../etc/passwd",
])
def test_local_texture_rejects_path_traversal_attempts(client, name):
    r = client.get(f"/local-textures/{name}", headers=HDR)
    assert r.status_code == 404


def test_patient_name_with_markup_is_escaped_on_the_page(tmp_path):
    # A patient name containing script-like or template-like text must render as inert text,
    # not execute or interpolate. Confirms Jinja's autoescaping actually holds for this field.
    tricky = Patient("9", "<script>alert(1)</script>", "{{7*7}}", "1990-01-01", "F", "00000")
    app = create_app(patients=PTS + [tricky], model=None, texture_dir=tmp_path / "tex")
    page = app.test_client().get("/patients", headers=HDR).data
    assert b"<script>alert(1)</script>" not in page   # not rendered as live markup
    assert b"49" not in page                            # not evaluated as a template expression
    assert b"&lt;script&gt;" in page or b"&amp;lt;" in page or b"alert(1)" in page  # escaped, still visible as text


def test_roi_summary_over_300_chars_is_refused_even_posted_directly(tmp_path):
    # The 300-char limit in roi.py must be enforced server-side, not just via the form's maxlength
    # attribute (which a direct POST bypasses entirely).
    from datetime import date
    from him_workbench.roi import RoiStore
    app = make(tmp_path, roi_store=RoiStore(":memory:", days=30), today=lambda: date(2026, 10, 24))
    c = app.test_client()
    r = c.post("/roi/new", json={"patient_id": "3", "requester_type": "attorney",
                                 "summary": "x" * 301, "received": "2026-10-01"}, headers=HDR)
    assert r.status_code == 400


def test_ask_question_over_500_chars_is_refused_even_posted_directly(client):
    r = client.post("/api/ask", json={"question": "x" * 501}, headers=HDR)
    assert r.status_code == 400


def test_app_does_not_run_in_debug_mode():
    # Werkzeug's interactive debugger (and its PIN-bypass history) must never be reachable.
    # Read as text rather than imported: ui_cli.py is meant to run with scripts/ itself on
    # sys.path (not as an importable package), so importing it here would fail for reasons
    # unrelated to what this test checks.
    src = (Path(__file__).parent.parent / "scripts" / "ui_cli.py").read_text()
    assert "debug=True" not in src
