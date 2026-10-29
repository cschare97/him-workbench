"""Local text embeddings via Ollama's /api/embeddings endpoint. Nothing leaves this machine: the
request goes to the Ollama host set by OLLAMA_HOST (default localhost), same as the chat model.

Used by docsearch.py for semantic search over the generated clinical notes. Needs an embedding
model pulled first, e.g. `ollama pull nomic-embed-text`. Set HIM_EMBED_MODEL to use a different one.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

from .assistant import _host

DEFAULT_MODEL = "nomic-embed-text"


class EmbeddingError(RuntimeError):
    """Ollama could not be reached, or did not return a usable embedding. The message says which."""


def default_model() -> str:
    return os.environ.get("HIM_EMBED_MODEL", "").strip() or DEFAULT_MODEL


def embed(text: str, model: str | None = None, host: str | None = None, timeout: int = 60) -> list[float]:
    model = model or default_model()
    url = _host(host) + "/api/embeddings"
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception as exc:
        raise EmbeddingError(f"Could not reach Ollama for embeddings ({type(exc).__name__}). "
                             "Is Ollama running?") from exc
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise EmbeddingError(f"Ollama did not return an embedding from '{model}'. "
                             f"Is it pulled? Try: ollama pull {model}")
    return [float(x) for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
