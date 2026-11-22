"""Read-only public mode (HIM_PUBLIC=1) for the hosted demo."""
import importlib

import pytest

from him_workbench import webapp
from him_workbench.webapp import create_app

HOST = "demo.example.test"


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("HIM_PUBLIC", "1")
    monkeypatch.setenv("HIM_ALLOWED_HOSTS", f"{HOST}, other.example.test")
    def no_models(*a, **k):
        raise AssertionError("public mode must never ask Ollama for models")
    monkeypatch.setattr(webapp, "list_models", no_models)
    return create_app().test_client()


def get(client, path, host=HOST, **kw):
    return client.get(path, base_url=f"http://{host}", **kw)


@pytest.mark.parametrize("path", ["/care-gaps", "/about", "/sources"])
def test_public_pages_load(public, path):
    assert get(public, path).status_code == 200


@pytest.mark.parametrize("path", ["/patients", "/duplicates", "/roi", "/referrals", "/search", "/smart",
                                  "/audit", "/models", "/dashboard", "/ccd", "/sync", "/codes"])
def test_everything_else_is_not_found(public, path):
    assert get(public, path).status_code == 404


def test_home_goes_to_care_gaps(public):
    r = get(public, "/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/care-gaps")


def test_no_writes_at_all(public):
    # Without the page token the existing guard refuses it; with a valid token, public mode still does.
    assert public.post("/api/model", base_url=f"http://{HOST}", json={"model": "x"}).status_code == 403
    token = public.application.config["HIM_TOKEN"]
    r = public.post("/api/model", base_url=f"http://{HOST}", json={"model": "x"}, headers={"X-HIM-Token": token})
    assert r.status_code == 405


def test_unknown_host_is_still_refused(public):
    assert get(public, "/care-gaps", host="evil.example.test").status_code == 403


def test_second_allowed_host_and_localhost_work(public):
    assert get(public, "/care-gaps", host="other.example.test").status_code == 200
    assert get(public, "/care-gaps", host="127.0.0.1").status_code == 200


def test_layout_hides_private_pages_and_the_assistant(public):
    html = get(public, "/care-gaps").get_data(as_text=True)
    for hidden in ('href="/patients"', 'href="/roi"', 'id="ask-form"', "app.js", 'id="model-name"'):
        assert hidden not in html, hidden
    assert "Read-only demo" in html and 'href="/care-gaps"' in html


def test_explain_page_never_calls_a_model(public):
    html = get(public, "/care-gaps").get_data(as_text=True)
    link = html.split('href="/care-gaps/explain?')[1].split('"')[0].replace("&amp;", "&")
    r = get(public, f"/care-gaps/explain?{link}&model=qwen2.5:7b")
    assert r.status_code == 200
    assert "Write summary" not in r.get_data(as_text=True)


def test_allowed_hosts_are_ignored_outside_public_mode(monkeypatch):
    monkeypatch.delenv("HIM_PUBLIC", raising=False)
    monkeypatch.setenv("HIM_ALLOWED_HOSTS", HOST)
    client = create_app().test_client()
    assert get(client, "/care-gaps").status_code == 403


def test_public_entry_point_is_always_public(monkeypatch):
    monkeypatch.setenv("HIM_PUBLIC", "0")
    monkeypatch.setenv("HIM_ALLOWED_HOSTS", HOST)
    import him_workbench.public as entry
    entry = importlib.reload(entry)
    assert get(entry.app.test_client(), "/patients").status_code == 404
