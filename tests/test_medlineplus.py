import json

import pytest

from him_workbench import audit
from him_workbench.assistant import Assistant
from him_workbench.medlineplus import MedlinePlusClient, MedlinePlusError, parse


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


HTML = ('<div class="NLMalsoCalled"><p>Also called: Test</p></div><p>Alpha is a <b>test</b> condition.</p>'
        '<ul><li>point one</li></ul><div class="NLMattribution"><p>Source: Test Institute</p></div>'
        '<div class="NLMrelatedLinks"><ul><li>skip me</li></ul></div>')
FEED = {"feed": {"entry": [{"title": {"_type": "text", "_value": "Alpha"},
                            "link": [{"rel": "alternate", "href": "https://medlineplus.gov/alpha.html"}],
                            "summary": {"_type": "html", "_value": HTML}}]}}


def client(tmp_path, data, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if isinstance(data, Exception):
            raise data
        return data
    return MedlinePlusClient(fetch=fetch, cache_path=tmp_path / "m.json")


def test_parse_keeps_text_and_credit_and_drops_related_links():
    item = parse(FEED)[0]
    assert item["title"] == "Alpha" and item["url"] == "https://medlineplus.gov/alpha.html"
    assert "Alpha is a test condition." in item["summary"] and "skip me" not in item["summary"]
    assert item["credit"] == "Source: Test Institute"


def test_long_summary_is_shortened():
    feed = {"feed": {"entry": [{"title": "T", "link": {"href": "https://x"}, "summary": "<p>" + "word " * 400 + "</p>"}]}}
    assert len(parse(feed)[0]["summary"]) < 620 and parse(feed)[0]["summary"].endswith("...")


def test_unrecognized_format_is_refused_not_guessed():
    with pytest.raises(MedlinePlusError):
        parse({"something": "else"})
    with pytest.raises(MedlinePlusError):
        parse({"feed": {"entry": [{"summary": "<p>x</p>"}]}})


def test_education_builds_icd10cm_request_and_caches(tmp_path):
    calls = []
    c = client(tmp_path, FEED, calls)
    r = c.education("e11.9"); c.education("E11.9")
    assert r["code"] == "E11.9" and r["results"][0]["title"] == "Alpha" and len(calls) == 1
    assert "2.16.840.1.113883.6.90" in calls[0] and "E11.9" in calls[0] and "application%2Fjson" in calls[0]


def test_no_page_says_so(tmp_path):
    r = client(tmp_path, {"feed": {}}).education("Z99.89")
    assert r["results"] == [] and "no patient-education page" in r["note"]


def test_empty_query_is_rejected_before_any_request(tmp_path):
    calls = []
    assert "error" in client(tmp_path, FEED, calls).education("  ") and calls == []


def test_audit_log_has_no_code(tmp_path):
    client(tmp_path, FEED).education("E11.9")
    assert "E11.9" not in (tmp_path / "audit.jsonl").read_text()


def test_assistant_offers_tool_only_when_enabled_and_reports_errors(tmp_path):
    call = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "disease_education", "arguments": {"query": "E11.9"}}}]}
    seen = []
    def chat(m, tools):
        seen.append([t["function"]["name"] for t in tools])
        return call if len(seen) == 1 else {"role": "assistant", "content": "offline"}
    bot = Assistant(chat, education=client(tmp_path, MedlinePlusError("Could not get an answer from MedlinePlus Connect")))
    bot.ask("x")
    assert "disease_education" in seen[0]
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert "Could not get an answer" in msg["error"]
    assert all(t["function"]["name"] != "disease_education" for t in Assistant(lambda m, t: {}).tools)


XML = """<?xml version="1.0" encoding="UTF-8"?>
<nlmSearchResult><term>alpha</term><count>7</count><retmax>2</retmax>
<list num="1" start="0" per="1">
<document url="https://medlineplus.gov/alpha.html" rank="0">
<content name="title">The <span class="qt0">Alpha</span> Condition</content>
<content name="FullSummary">&lt;p&gt;Alpha is a test condition.&lt;/p&gt;&lt;div class="NLMattribution"&gt;&lt;p&gt;Source: Test Institute&lt;/p&gt;&lt;/div&gt;</content>
</document></list></nlmSearchResult>"""


def test_topics_xml_parses_title_summary_credit_and_total():
    from him_workbench.medlineplus import parse_topics
    pages, total = parse_topics(XML)
    assert total == 7 and pages[0]["title"] == "The Alpha Condition"
    assert pages[0]["summary"] == "Alpha is a test condition." and pages[0]["credit"] == "Source: Test Institute"


def test_topics_refuses_entities_and_unknown_xml():
    from him_workbench.medlineplus import parse_topics
    with pytest.raises(MedlinePlusError):
        parse_topics('<!DOCTYPE x [<!ENTITY a "b">]><nlmSearchResult/>')
    with pytest.raises(MedlinePlusError):
        parse_topics("<other/>")
    with pytest.raises(MedlinePlusError):
        parse_topics("not xml")


def test_a_name_is_searched_and_a_code_goes_to_connect(tmp_path):
    urls = []
    c = MedlinePlusClient(fetch=lambda u: urls.append(("json", u)) or FEED,
                          fetch_text=lambda u: urls.append(("xml", u)) or XML, cache_path=tmp_path / "m.json")
    r = c.education("alpha condition")
    assert r["term"] == "alpha condition" and "Only 1 of 7" in r["note"] and urls[0][0] == "xml"
    assert "healthTopics" in urls[0][1] and "tool=him-workbench" in urls[0][1]
    assert c.education("E11.9")["code"] == "E11.9" and urls[1][0] == "json"


REAL_SHAPE = """<?xml version="1.0" encoding="UTF-8"?>
<nlmSearchResult><term>pernicious anemia</term><count>4</count><retmax>2</retmax>
<list num="4" start="0" per="2">
<document rank="0" url="https://medlineplus.gov/anemia.html"><content name="healthTopic">
<health-topic title="Anemia" url="https://medlineplus.gov/anemia.html" id="139" language="English">
<also-called>Iron poor blood</also-called>
<full-summary>&lt;p&gt;If you have anemia, your blood does not carry enough oxygen. Most common cause is not enough &lt;a href="https://medlineplus.gov/iron.html"&gt;iron&lt;/a&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Heavy periods&lt;/li&gt;&lt;/ul&gt;</full-summary>
</health-topic></content></document></list></nlmSearchResult>"""


def test_topics_real_shape_title_and_summary_inside_health_topic():
    from him_workbench.medlineplus import parse_topics
    pages, total = parse_topics(REAL_SHAPE)
    assert total == 4 and pages[0]["title"] == "Anemia" and pages[0]["url"] == "https://medlineplus.gov/anemia.html"
    assert pages[0]["summary"].startswith("If you have anemia, your blood does not carry enough oxygen.")
    assert "<" not in pages[0]["summary"] and "iron" in pages[0]["summary"]


def test_education_result_carries_the_code_meaning_and_a_parts_note(tmp_path):
    from him_workbench.icd10 import ICD10Index
    f = tmp_path / "icd10cm_codes_2099.txt"
    f.write_text("J1282   Test pneumonia due to test virus\n")
    two = {"feed": {"entry": [
        {"title": {"_value": "Virus"}, "link": [{"href": "https://medlineplus.gov/virus.html"}], "summary": {"_value": "<p>v</p>"}},
        {"title": {"_value": "Pneumonia"}, "link": [{"href": "https://medlineplus.gov/pneumonia.html"}], "summary": {"_value": "<p>p</p>"}}]}}
    call = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "disease_education", "arguments": {"query": "J12.82"}}}]}
    replies = iter([call, {"role": "assistant", "content": "ok"}])
    bot = Assistant(lambda m, t: next(replies), icd=ICD10Index.from_file(f), education=client(tmp_path, two))
    bot.ask("x")
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert msg["code_description"] == "Test pneumonia due to test virus"
    assert "cover its parts" in msg["note"] or "describe the parts" in msg["note"]
