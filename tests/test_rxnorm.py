import json

import pytest

from him_workbench import audit
from him_workbench.assistant import Assistant
from him_workbench.rxnorm import RxNormClient, RxNormError


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


DRUGS = {"drugGroup": {"name": "x", "conceptGroup": [
    {"tty": "SCD", "conceptProperties": [{"rxcui": "2", "name": "Test drug 10 MG Oral Tablet", "tty": "SCD"}]},
    {"tty": "IN", "conceptProperties": [{"rxcui": "1", "name": "testdrug", "tty": "IN"}]}]}}


def client(tmp_path, responses, calls=None, now=lambda: 1000.0):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        for key, val in responses.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError("unexpected url " + url)
    return RxNormClient(fetch=fetch, cache_path=tmp_path / "rx.json", now=now)


ID_HIT = {"idGroup": {"rxnormId": ["1"]}}
ID_PROPS = {"properties": {"rxcui": "1", "name": "testdrug", "tty": "IN"}}


def test_name_search_puts_ingredient_first(tmp_path):
    r = client(tmp_path, {"/rxcui.json": ID_HIT, "/rxcui/1/properties.json": ID_PROPS, "/drugs.json": DRUGS}).search("testdrug")
    assert [e["rxcui"] for e in r["results"]] == ["1", "2"] and r["match"] == "name"
    assert r["results"][0]["type_label"] == "ingredient" and r["source"] == "RxNorm API (NLM)"


def test_cache_avoids_second_call_until_expired(tmp_path):
    calls, t = [], [1000.0]
    c = client(tmp_path, {"/rxcui.json": {"idGroup": {}}, "/drugs.json": DRUGS}, calls, now=lambda: t[0])
    c.search("testdrug"); first = len(calls); c.search("testdrug")
    assert len(calls) == first
    t[0] += 25 * 3600
    c.search("testdrug")
    assert len(calls) == 2 * first


def test_approximate_fallback_is_labeled(tmp_path):
    c = client(tmp_path, {"/rxcui.json": {"idGroup": {}}, "/drugs.json": {"drugGroup": {"name": "x"}},
                          "/approximateTerm.json": {"approximateGroup": {"candidate": [{"rxcui": "1"}, {"rxcui": "1"}]}},
                          "/rxcui/1/properties.json": {"properties": {"rxcui": "1", "name": "testdrug", "tty": "IN"}}})
    r = c.search("tesdrug")
    assert r["match"] == "approximate" and len(r["results"]) == 1 and "closest matches" in r["note"]


def test_nothing_found_says_so(tmp_path):
    c = client(tmp_path, {"/rxcui.json": {"idGroup": {}}, "/drugs.json": {"drugGroup": {}}, "/approximateTerm.json": {"approximateGroup": {}}})
    r = c.search("zzzz")
    assert r["results"] == [] and "No drug concept found" in r["note"]


def test_partial_list_is_labeled(tmp_path):
    many = {"drugGroup": {"conceptGroup": [{"tty": "SCD", "conceptProperties": [
        {"rxcui": str(i), "name": f"d{i}", "tty": "SCD"} for i in range(12)]}]}}
    r = client(tmp_path, {"/rxcui.json": {"idGroup": {}}, "/drugs.json": many}).search("d")
    assert len(r["results"]) == 10 and r["total_matches"] == 12 and "10 of 12" in r["note"]


def test_audit_log_never_has_the_drug_name(tmp_path):
    client(tmp_path, {"/rxcui.json": {"idGroup": {}}, "/drugs.json": DRUGS}).search("secretdrugname")
    assert "secretdrugname" not in (tmp_path / "audit.jsonl").read_text()


def scripted(*replies):
    it = iter(replies)
    return lambda m, t: next(it)


def test_assistant_tool_only_offered_when_enabled_and_errors_are_reported(tmp_path):
    call = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_drug", "arguments": {"name": "x"}}}]}
    seen = {}
    def chat(m, tools):
        seen.setdefault("n", []).append([t["function"]["name"] for t in tools])
        return call if len(seen["n"]) == 1 else {"role": "assistant", "content": "offline"}
    c = client(tmp_path, {"/rxcui.json": RxNormError("Could not get an answer from NLM")})
    bot = Assistant(chat, drugs=c)
    bot.ask("x")
    assert "search_drug" in seen["n"][0]
    tool_msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert "Could not get an answer" in tool_msg["error"]
    off = Assistant(scripted({"role": "assistant", "content": "hi"}))
    assert all(t["function"]["name"] != "search_drug" for t in off.tools)
