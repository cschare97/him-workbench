"""In-memory semantic index over the generated clinical notes (notes.py), ranked by cosine
similarity between local Ollama embeddings. Not persisted to disk — rebuilt once per app run,
which is cheap at the size of a synthetic patient set (one short note per patient).

The embedding call is swappable (embed_fn) so this can be tested without a running Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import audit
from .embeddings import EmbeddingError, cosine, embed as _default_embed

EmbedFn = Callable[[str], list[float]]


@dataclass
class Hit:
    patient_id: str
    text: str
    score: float


class NoteIndex:
    def __init__(self, embed_fn: EmbedFn | None = None):
        self.embed_fn = embed_fn or _default_embed
        self._notes: list[tuple[str, str]] = []
        self._vecs: list[list[float]] = []
        self.build_error = ""
        self.skipped_count = 0

    def __len__(self) -> int:
        return len(self._notes)

    def build(self, notes) -> None:
        """notes: iterable of (patient_id, text). A note whose embedding call fails is skipped, not
        treated as fatal for the whole build — the commonest cause isn't Ollama being unreachable, it's
        one patient's note being too long for the embedding model's context window (many chronic/staged
        conditions can add up), and that should cost you one patient, not the entire index.
        build_error is only set when nothing at all could be embedded (genuinely unreachable Ollama,
        wrong model, etc); otherwise it stays empty and skipped_count says how many notes were left out."""
        self._notes, self._vecs, self.build_error = [], [], ""
        self.skipped_count = 0
        first_error = ""
        for pid, text in notes:
            try:
                vec = self.embed_fn(text)
            except EmbeddingError as exc:
                self.skipped_count += 1
                first_error = first_error or str(exc)
                continue
            self._notes.append((pid, text))
            self._vecs.append(vec)
        if not self._notes and first_error:
            self.build_error = first_error
        audit.log_call("docsearch_build", {}, f"{len(self._notes)} notes indexed"
                       + (f", {self.skipped_count} skipped" if self.skipped_count else ""))

    def search(self, query: str, limit: int = 10) -> tuple[list[Hit], str]:
        if not self._notes:
            return [], self.build_error or "No notes indexed."
        try:
            qvec = self.embed_fn(query)
        except EmbeddingError as exc:
            return [], str(exc)
        scored = sorted((Hit(pid, text, cosine(qvec, v)) for (pid, text), v in zip(self._notes, self._vecs)),
                        key=lambda h: h.score, reverse=True)
        audit.log_call("docsearch_search", {"query_chars": len(query)}, f"{min(limit, len(scored))} of {len(scored)} results")
        return scored[:limit], ""
