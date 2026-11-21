"""Hybrid search: vector similarity + full-text, merged with reciprocal rank fusion."""

from __future__ import annotations

from pd_rag.embeddings import Embedder, tokenize
from pd_rag.models import Chunk, Kind, SearchHit
from pd_rag.store import ChunkStore

MAX_QUERY_CHARS = 2000
RRF_K = 60  # the constant from the original RRF paper; damps the influence of top ranks


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank_in_list).

    Only ranks matter, so lists with incomparable scores (cosine vs ts_rank) merge fairly.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


class HybridSearcher:
    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        *,
        candidates: int = 20,
        rrf_k: int = RRF_K,
        max_per_source: int = 2,
    ) -> None:
        if max_per_source < 1:
            raise ValueError("max_per_source must be >= 1")
        self.store, self.embedder, self.candidates, self.rrf_k = store, embedder, candidates, rrf_k
        # Without a cap, all top hits can be sections of one document and hide other sources
        # (for example the matching past incident behind its runbook).
        self.max_per_source = max_per_source

    async def search(
        self, query: str, *, kind: Kind | None = None, limit: int = 5
    ) -> list[SearchHit]:
        query = query.strip()[:MAX_QUERY_CHARS]
        if not query or limit < 1:
            return []
        [qvec] = await self.embedder.embed([query], query=True)
        by_vector = await self.store.vector_search(qvec, self.embedder.id, self.candidates, kind)
        by_keyword = await self.store.keyword_search(
            list(dict.fromkeys(tokenize(query))), self.candidates, kind
        )

        scores = rrf([[c.id for c in by_vector], [c.id for c in by_keyword]], self.rrf_k)
        chunks: dict[str, Chunk] = {c.id: c for c in [*by_vector, *by_keyword]}
        vrank = {c.id: i for i, c in enumerate(by_vector, start=1)}
        krank = {c.id: i for i, c in enumerate(by_keyword, start=1)}
        ordered: list[str] = []
        per_source: dict[str, int] = {}
        for chunk_id in sorted(scores, key=lambda i: (-scores[i], i)):
            source = chunks[chunk_id].source
            if per_source.get(source, 0) >= self.max_per_source:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            ordered.append(chunk_id)
            if len(ordered) == limit:
                break
        return [
            SearchHit(
                chunk=chunks[i],
                score=scores[i],
                vector_rank=vrank.get(i),
                keyword_rank=krank.get(i),
            )
            for i in ordered
        ]
