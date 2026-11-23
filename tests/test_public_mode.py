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


def test_explain_page_wording_on_a_public_site(public):
    # Same state as the hosted demo: no NCQA spec in the cache (CI never fetches specs either).
    html = get(public, "/care-gaps").get_data(as_text=True)
    link = html.split('href="/care-gaps/explain?')[1].split('"')[0].replace("&amp;", "&")
    if "measure=CMS951" in link:
        pytest.skip("first gap is CMS951; this test needs an NCQA measure")
    page = get(public, f"/care-gaps/explain?{link}").get_data(as_text=True)
    assert "spec_loader.py fetch" not in page and "No local model is reachable" not in page
    assert "public site" in page and "ecqi.healthit.gov/sites/default/files/ecqm/measures/" in page


# ---- favicon, filtered sort, print layout ----

def test_favicon_redirects_to_svg(public):
    r = get(public, "/favicon.ico")
    assert r.status_code == 302 and r.headers["Location"].endswith("/static/favicon.svg")
    icon = get(public, "/static/favicon.svg")
    assert icon.status_code == 200 and icon.mimetype == "image/svg+xml"


def test_pages_link_the_icon(public):
    assert 'rel="icon"' in get(public, "/care-gaps").get_data(as_text=True)


def test_filtered_view_sorts_by_matching_gaps():
    d = {"patients": [
        {"id": "a", "name": "Alpha", "results": [
            {"measure": "CMS131 Eye exam", "gap": True, "kind": "care"},
            {"measure": "CMS951 Kidney health", "gap": True, "kind": "care"},
            {"measure": "CMS122 A1c >9%", "gap": True, "kind": "open order"}]},
        {"id": "b", "name": "Bravo", "results": [
            {"measure": "CMS122 A1c >9%", "gap": True, "kind": "open order"},
            {"measure": "CMS131 Eye exam", "gap": True, "kind": "open order"}]},
        {"id": "c", "name": "Charlie", "results": [
            {"measure": "CMS122 A1c >9%", "gap": False, "kind": None}]},
    ]}
    rows = webapp.care_gap_rows(d)
    assert [r["patient"]["id"] for r in rows] == ["a", "b"]          # unfiltered: total gaps
    rows = webapp.care_gap_rows(d, kind="open order")
    assert [r["patient"]["id"] for r in rows] == ["b", "a"]          # filtered: matching gaps only
    assert [len(r["gaps"]) for r in rows] == [2, 1]
    assert webapp.care_gap_rows(d, measure="CMS951", kind="open order") == []


def test_bad_filter_values_are_ignored(public):
    html = get(public, "/care-gaps?kind=<script>&measure=CMS999").get_data(as_text=True)
    assert "<script>" not in html and "(clear filters)" not in html


def test_print_styles_unpin_header_and_keep_notice():
    from pathlib import Path
    css = (Path(webapp.__file__).parent / "static" / "style.css").read_text()
    block = css[css.index("@media print"):]
    assert ".top, .strip { position: static" in block
    hidden = [line for line in block.splitlines() if "display: none" in line]
    assert hidden and not any(".strip" in line for line in hidden)   # the synthetic-data notice always prints


# ---- data-quality notes ----

def test_data_quality_view_counts_cms122_passes_on_low_values():
    dq = {"patients": [
        {"id": "a", "name": "A", "low_count": 3, "latest_in_period_low": True, "notes": ["n"]},
        {"id": "b", "name": "B", "low_count": 1, "latest_in_period_low": True, "notes": ["n"]},
        {"id": "c", "name": "C", "low_count": 0, "latest_in_period_low": False, "repeat_runs": 1, "notes": ["n"]},
    ]}
    d = {"patients": [
        {"id": "a", "results": [{"measure": "CMS122 A1c >9%", "status": "not met"}]},   # passing (inverse)
        {"id": "b", "results": [{"measure": "CMS122 A1c >9%", "status": "excluded"}]},
    ]}
    v = webapp.data_quality_view(dq, d)
    assert [p["id"] for p in v["passing_on_low"]] == ["a"]
    assert [p["id"] for p in v["low_list"]] == ["a", "b"]
    assert webapp.data_quality_view(None, d) is None


def test_care_gaps_page_shows_data_quality_notes(public):
    html = get(public, "/care-gaps").get_data(as_text=True)
    if webapp.load_data_quality(webapp.Path(webapp.__file__).resolve().parents[2]):
        assert "Data-quality notes" in html and "pass CMS122 on a most recent A1c under 4%" in html


# ---- rate limit ----

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_limiter_window_and_retry_after():
    clock = FakeClock()
    lim = webapp.RateLimiter(limit=2, window=60, clock=clock)
    assert lim.allow("x") == 0 and lim.allow("x") == 0
    wait = lim.allow("x")
    assert 1 <= wait <= 61
    assert lim.allow("y") == 0                       # other clients are not affected
    clock.t += 61
    assert lim.allow("x") == 0                       # the window slid past the old hits


def test_rate_limiter_memory_is_bounded():
    lim = webapp.RateLimiter(limit=5, max_clients=3, clock=FakeClock())
    for k in "abcde":
        lim.allow(k)
    assert list(lim.hits) == ["c", "d", "e"]


def test_public_mode_returns_429_per_client(monkeypatch):
    monkeypatch.setenv("HIM_PUBLIC", "1")
    monkeypatch.setenv("HIM_ALLOWED_HOSTS", HOST)
    monkeypatch.setenv("HIM_RATE_LIMIT", "3")
    client = create_app().test_client()
    alice = {"True-Client-IP": "203.0.113.5"}
    for _ in range(3):
        assert get(client, "/about", headers=alice).status_code == 200
    r = get(client, "/about", headers=alice)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert get(client, "/about", headers={"True-Client-IP": "203.0.113.9"}).status_code == 200
    assert get(client, "/static/style.css", headers=alice).status_code == 200   # static files are not counted


def test_x_forwarded_for_does_not_pick_the_client(monkeypatch):
    monkeypatch.setenv("HIM_PUBLIC", "1")
    monkeypatch.setenv("HIM_ALLOWED_HOSTS", HOST)
    monkeypatch.setenv("HIM_RATE_LIMIT", "2")
    client = create_app().test_client()
    for i in range(2):
        get(client, "/about", headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert get(client, "/about", headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 429
