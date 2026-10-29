import pytest

from him_workbench import smart_fhir
from him_workbench.smart_fhir import SmartError


def test_new_pkce_gives_a_matching_s256_pair():
    verifier, challenge = smart_fhir.new_pkce()
    import base64, hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect
    # two calls never collide
    v2, c2 = smart_fhir.new_pkce()
    assert v2 != verifier and c2 != challenge


def test_authorize_url_carries_pkce_and_state_and_aud():
    url = smart_fhir.authorize_url("https://sandbox.example/auth", server="https://sandbox.example/fhir",
                                   cid="him-workbench", redirect_uri="http://127.0.0.1:5050/smart/callback",
                                   state="abc123", code_challenge="xyz789")
    assert url.startswith("https://sandbox.example/auth?")
    assert "state=abc123" in url and "code_challenge=xyz789" in url and "code_challenge_method=S256" in url
    assert "aud=https%3A%2F%2Fsandbox.example%2Ffhir" in url
    assert "client_id=him-workbench" in url


def test_discover_reads_the_servers_own_smart_configuration(monkeypatch):
    seen = []
    def fake_get(url, token=None, timeout=15):
        seen.append(url)
        return {"authorization_endpoint": "https://sandbox.example/auth", "token_endpoint": "https://sandbox.example/token"}
    monkeypatch.setattr(smart_fhir, "_get_json", fake_get)
    out = smart_fhir.discover("https://sandbox.example/fhir/")
    assert out == {"authorization_endpoint": "https://sandbox.example/auth", "token_endpoint": "https://sandbox.example/token"}
    assert seen == ["https://sandbox.example/fhir/.well-known/smart-configuration"]


def test_discover_raises_a_plain_error_when_the_config_is_incomplete(monkeypatch):
    monkeypatch.setattr(smart_fhir, "_get_json", lambda url, token=None, timeout=15: {"authorization_endpoint": "x"})
    with pytest.raises(SmartError):
        smart_fhir.discover("https://sandbox.example/fhir")


def test_exchange_code_posts_the_right_fields(monkeypatch):
    seen = {}
    def fake_post(url, fields, timeout=15):
        seen["url"], seen["fields"] = url, fields
        return {"access_token": "tok123", "patient": "p1"}
    monkeypatch.setattr(smart_fhir, "_post_form", fake_post)
    out = smart_fhir.exchange_code("https://sandbox.example/token", code="c1",
                                   redirect_uri="http://127.0.0.1:5050/smart/callback", cid="him-workbench",
                                   code_verifier="v1")
    assert out == {"access_token": "tok123", "patient": "p1"}
    assert seen["url"] == "https://sandbox.example/token"
    assert seen["fields"] == {"grant_type": "authorization_code", "code": "c1",
                              "redirect_uri": "http://127.0.0.1:5050/smart/callback",
                              "client_id": "him-workbench", "code_verifier": "v1"}


def test_fetch_chart_builds_a_chart_from_live_resources(monkeypatch):
    patient_res = {"resourceType": "Patient", "id": "p1", "name": [{"family": "Lee", "given": ["Ann"]}],
                   "birthDate": "1990-01-01", "gender": "female"}
    condition_bundle = {"entry": [{"resource": {"resourceType": "Condition", "id": "c1",
                                                "code": {"coding": [{"display": "Hypertension (disorder)"}]},
                                                "clinicalStatus": {"coding": [{"code": "active"}]},
                                                "onsetDateTime": "2020-01-01"}}]}
    empty_bundle = {"entry": []}

    def fake_get(url, token=None, timeout=15):
        assert token == "tok123"
        if url.endswith("/Patient/p1"):
            return patient_res
        if url.startswith("https://sandbox.example/fhir/Condition"):
            return condition_bundle
        return empty_bundle
    monkeypatch.setattr(smart_fhir, "_get_json", fake_get)

    patient, chart = smart_fhir.fetch_chart("https://sandbox.example/fhir", "p1", "tok123")
    assert patient.first == "Ann" and patient.last == "Lee" and patient.dob == "1990-01-01"
    assert chart.patient_id == "p1"
    assert len(chart.problems) == 1 and chart.problems[0].what.display == "Hypertension (disorder)"


def test_fetch_chart_raises_smart_error_on_a_failed_read(monkeypatch):
    def fake_get(url, token=None, timeout=15):
        raise SmartError("HTTP 401")
    monkeypatch.setattr(smart_fhir, "_get_json", fake_get)
    with pytest.raises(SmartError):
        smart_fhir.fetch_chart("https://sandbox.example/fhir", "p1", "bad-token")


def test_iss_and_client_id_read_env_overrides(monkeypatch):
    monkeypatch.delenv("HIM_SMART_ISS", raising=False)
    monkeypatch.delenv("HIM_SMART_CLIENT_ID", raising=False)
    assert smart_fhir.iss() == smart_fhir.DEFAULT_ISS
    assert smart_fhir.client_id() == smart_fhir.DEFAULT_CLIENT_ID
    monkeypatch.setenv("HIM_SMART_ISS", "https://other.example/fhir")
    monkeypatch.setenv("HIM_SMART_CLIENT_ID", "another-client")
    assert smart_fhir.iss() == "https://other.example/fhir"
    assert smart_fhir.client_id() == "another-client"
