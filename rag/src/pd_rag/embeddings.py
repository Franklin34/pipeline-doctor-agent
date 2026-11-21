"""Embedders. ``HashEmbedder`` is deterministic and offline (tests, demos); ``GeminiEmbedder`` is the real one."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


class Embedder(Protocol):
    id: str  # stored with each chunk: vectors from different models are never compared
    dim: int

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return vec if norm == 0 else [v / norm for v in vec]


class HashEmbedder:
    """Feature hashing of unigrams and bigrams into a fixed-size unit vector.

    Not semantic (no synonyms), but deterministic and similar texts land close together, which is
    enough to test the retrieval plumbing without a network or a model.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim < 16:
            raise ValueError("dim must be at least 16")
        self.dim = dim
        self.id = f"hash-{dim}"

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        vec = [0.0] * self.dim
        for gram in [*tokens, *(f"{a} {b}" for a, b in itertools.pairwise(tokens))]:
            h = hashlib.sha1(gram.encode(), usedforsecurity=False).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            vec[idx] += 1.0 if h[4] % 2 == 0 else -1.0
        return _normalize([math.copysign(math.log1p(abs(v)), v) for v in vec])


class GeminiEmbedder:
    """Gemini embeddings through ``google-genai``. Needs GOOGLE_API_KEY. Not exercised by the
    test-suite (no network in tests); the interface contract is covered with the fake."""

    BATCH = 64

    def __init__(self, model: str = "gemini-embedding-001", dim: int = 768) -> None:
        from google import genai

        self._client = genai.Client()
        self._model, self.dim = model, dim
        self.id = f"{model}-{dim}"

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        from google.genai import types

        config = types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
            output_dimensionality=self.dim,
        )
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            resp = await self._client.aio.models.embed_content(
                model=self._model, contents=list(texts[i : i + self.BATCH]), config=config
            )
            for emb in resp.embeddings or []:
                out.append(_normalize(list(emb.values or [])))  # truncated dims are not unit length
        if len(out) != len(texts):
            raise RuntimeError(
                f"embedding service returned {len(out)} vectors for {len(texts)} texts"
            )
        return out


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # inputs are unit vectors
