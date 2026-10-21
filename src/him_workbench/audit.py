"""Append-only audit log. Every tool call goes through log_call()."""

import json
import time
from pathlib import Path

LOG_PATH = Path("logs/audit.jsonl")


def log_call(tool: str, args: dict, result_summary: str) -> None:
    """Record what a tool actually did. Never log patient identifiers."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "args": args,
        "result": result_summary,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
