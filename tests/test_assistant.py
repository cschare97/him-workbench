import json

import pytest

from him_workbench import audit
from him_workbench.assistant import Assistant
from him_workbench.icd10 import ICD10Index
from him_workbench.matching import Patient


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def icd(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("A000    Test condition alpha\nA001    Test condition beta\n")
    return ICD10Index.from_file(p)


def scripted(*replies):
    """Fake chat function that returns the given messages in order."""
    it = iter(replies)
    return lambda messages, tools: next(it)


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def say(text):
    return {"role": "assistant", "content": text}


def test_tool_call_then_grounded_answer(icd):
    bot = Assistant(scripted(call("search_icd10", query="alpha"), say("Code A00.0 is test condition alpha.")), icd=icd)
    out = bot.ask("find alpha")
    assert out == "Code A00.0 is test condition alpha."          # no warning: code came from a tool
    assert any(m["role"] == "tool" for m in bot.messages)


def test_made_up_code_is_flagged(icd):
    bot = Assistant(scripted(say("The code is Z99.89.")), icd=icd)
    out = bot.ask("what code?")
    assert "Not returned by a lookup" in out and "Z99.89" in out


def test_string_arguments_and_unknown_tool_do_not_crash(icd):
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "lookup_icd10", "arguments": json.dumps({"code": "A00.1"})}},
        {"function": {"name": "delete_everything", "arguments": {}}}]}
    bot = Assistant(scripted(msg, say("done")), icd=icd)
    assert bot.ask("x") == "done"
    tool_msgs = [json.loads(m["content"]) for m in bot.messages if m["role"] == "tool"]
    assert tool_msgs[0]["status"] == "valid" and "unknown tool" in tool_msgs[1]["error"]


def test_scan_and_compare_use_loaded_patients(icd):
    pts = [Patient("1", "Robert", "Alvarado", "1980-03-14", "M", "90210"),
           Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210")]
    bot = Assistant(scripted(call("scan_duplicates"), call("compare_patients", id_a="1", id_b="nope"), say("ok")),
                    icd=icd, patients=pts)
    bot.ask("scan")
    tool_msgs = [json.loads(m["content"]) for m in bot.messages if m["role"] == "tool"]
    assert tool_msgs[0]["pairs"][0]["label"] == "match"


def test_missing_data_is_reported_not_invented():
    bot = Assistant(scripted(call("search_icd10", query="x"), say("not available")))
    bot.ask("x")
    tool_msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert "error" in tool_msg


def test_step_limit(icd):
    bot = Assistant(lambda m, t: call("search_icd10", query="alpha"), icd=icd, max_steps=3)
    assert bot.ask("loop").startswith("Stopped")


def test_audit_log_has_tool_names_not_questions(icd, tmp_path):
    bot = Assistant(scripted(call("search_icd10", query="alpha"), say("A00.0")), icd=icd)
    bot.ask("my secret question text")
    log = (tmp_path / "audit.jsonl").read_text()
    assert "assistant_tool" in log and "secret" not in log


def test_ollama_transport_against_fake_server(icd):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from him_workbench.assistant import list_models, ollama_chat

    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"models": [{"name": "fake-model:1b"}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"message": {"role": "assistant", "content": "hello"}}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_port}"
    try:
        assert list_models(host) == ["fake-model:1b"]
        bot = Assistant(ollama_chat("fake-model:1b", host=host), icd=icd)
        assert bot.ask("hi") == "hello"
        assert seen["body"]["model"] == "fake-model:1b" and seen["body"]["stream"] is False
        assert seen["body"]["tools"] and seen["body"]["messages"][0]["role"] == "system"
    finally:
        srv.shutdown()


def test_on_result_receives_raw_tool_output(icd):
    got = []
    bot = Assistant(scripted(call("search_icd10", query="alpha"), say("ok")), icd=icd,
                    on_result=lambda n, r: got.append((n, r)))
    bot.ask("x")
    assert got[0][0] == "search_icd10" and got[0][1]["results"][0]["code"] == "A00.0"


def test_search_tool_reports_ignored_words(icd):
    bot = Assistant(scripted(call("search_icd10", query="alpha complications"), say("ok")), icd=icd)
    bot.ask("x")
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert msg["results"][0]["code"] == "A00.0"
    assert msg["ignored_words"] == ["complications"] and "ignored" in msg["note"]


def test_scan_result_states_its_own_counts(icd):
    pts = [Patient("1", "Robert", "Alvarado", "1980-03-14", "M", "90210"),
           Patient("2", "Bob", "Alvarado", "1980-03-14", "M", "90210"),
           Patient("3", "Jaden", "Lee", "2005-06-01", "M", "30301"),
           Patient("4", "Jayden", "Lee", "2005-06-01", "M", "30301")]
    bot = Assistant(scripted(call("scan_duplicates", max_pairs=1), say("ok")), icd=icd, patients=pts)
    bot.ask("scan")
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert msg["pairs_found"] == 2 and msg["pairs_shown"] == 1
    assert "2 possible duplicate pair(s) found" in msg["summary"] and "1 more not shown" in msg["summary"]


def test_partial_results_are_labeled_partial(tmp_path):
    p = tmp_path / "icd10cm_codes_2099.txt"
    p.write_text("".join(f"A{n:02d}0    Test condition item {n}\n" for n in range(12)))
    big = ICD10Index.from_file(p)
    bot = Assistant(scripted(call("search_icd10", query="test condition"), say("ok")), icd=big)
    bot.ask("x")
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert len(msg["results"]) == 10 and msg["total_matches"] == 12
    assert "10 of 12" in msg["note"] and "partial" in msg["note"]


def test_data_quality_tool_returns_counts_only(icd):
    pts = [Patient("1", "Robert", "Alvarado", "1980-13-45", "M", "9021"),
           Patient("2", "Maria", "Okafor", "1991-11-02", "F", "60614")]
    bot = Assistant(scripted(call("data_quality_report"), say("ok")), icd=icd, patients=pts)
    bot.ask("how clean is the data")
    msg = json.loads([m for m in bot.messages if m["role"] == "tool"][0]["content"])
    assert msg["records"] == 2 and msg["problems"]["dob_malformed"] == 1 and msg["problems"]["zip_malformed"] == 1
    assert "Alvarado" not in json.dumps(msg) and "2 records" in msg["summary"]
