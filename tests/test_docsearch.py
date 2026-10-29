import re

import pytest

from him_workbench import audit
from him_workbench.docsearch import NoteIndex
from him_workbench.embeddings import EmbeddingError


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


def bow_embed(text: str) -> list[float]:
    """A fake embedding: a bag-of-words count vector over a fixed small vocabulary. Deterministic,
    no network, and close enough to real embeddings' behavior (more shared words -> higher cosine)
    to test ranking without needing a running Ollama."""
    vocab = ["cardiology", "palpitations", "podiatry", "foot", "pain", "diabetes", "insulin"]
    words = re.findall(r"[a-z]+", text.lower())
    return [float(words.count(w)) for w in vocab]


def failing_embed(text: str) -> list[float]:
    raise EmbeddingError("Could not reach Ollama for embeddings (ConnectionRefusedError). Is Ollama running?")


def test_search_ranks_closer_matches_first():
    idx = NoteIndex(embed_fn=bow_embed)
    idx.build([
        ("p1", "Referred to cardiology for palpitations."),
        ("p2", "Referred to podiatry for foot pain."),
        ("p3", "Diabetes managed with insulin."),
    ])
    assert len(idx) == 3
    hits, error = idx.search("heart racing, cardiology concern")
    assert not error
    assert hits[0].patient_id == "p1" and hits[0].score > hits[1].score


def test_search_before_build_says_nothing_indexed():
    idx = NoteIndex(embed_fn=bow_embed)
    hits, error = idx.search("anything")
    assert hits == [] and "No notes indexed" in error


def test_build_records_error_when_nothing_at_all_can_be_embedded():
    idx = NoteIndex(embed_fn=failing_embed)
    idx.build([("p1", "note one"), ("p2", "note two")])
    assert len(idx) == 0 and "Ollama" in idx.build_error and idx.skipped_count == 2
    hits, error = idx.search("anything")
    assert hits == [] and error == idx.build_error


def test_build_skips_one_bad_note_instead_of_aborting_the_rest():
    # One note (e.g. too long for the embedding model's context window) should not take down the
    # whole index — the other, perfectly good notes still get indexed and stay searchable.
    def flaky(text):
        if text == "too long":
            raise EmbeddingError("the input length exceeds the context length")
        return bow_embed(text)

    idx = NoteIndex(embed_fn=flaky)
    idx.build([("p1", "Referred to cardiology for palpitations."), ("p2", "too long"),
               ("p3", "Referred to podiatry for foot pain.")])
    assert len(idx) == 2 and idx.skipped_count == 1 and not idx.build_error
    hits, error = idx.search("cardiology")
    assert not error and hits[0].patient_id == "p1"


def test_search_itself_failing_is_reported_not_raised():
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] > 2:   # the build succeeds (2 notes), the query embed call then fails
            raise EmbeddingError("Could not reach Ollama for embeddings (TimeoutError). Is Ollama running?")
        return bow_embed(text)

    idx = NoteIndex(embed_fn=flaky)
    idx.build([("p1", "cardiology palpitations"), ("p2", "podiatry foot pain")])
    hits, error = idx.search("cardiology")
    assert hits == [] and "Ollama" in error


def test_audit_log_never_holds_note_text(tmp_path):
    idx = NoteIndex(embed_fn=bow_embed)
    idx.build([("secret-id-123", "very private palpitations note")])
    idx.search("palpitations")
    log = (tmp_path / "audit.jsonl").read_text()
    assert "docsearch_build" in log and "secret-id-123" not in log and "private" not in log
