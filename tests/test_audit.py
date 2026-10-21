import json

from him_workbench import audit


def test_log_call_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")
    audit.log_call("demo", {"a": 1}, "ok")
    audit.log_call("demo", {"a": 2}, "ok")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "demo"
