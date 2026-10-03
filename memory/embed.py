"""Embedder interface for future semantic search. v1 ships no embeddings; retrieval never calls this."""
from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """Turns texts into vectors. Implement this (e.g. with a free embedding API) and pass it to retrieval later."""

    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NullEmbedder:
    """The v1 default: produces nothing, so retrieval stays keyword + graph based."""

    dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]
