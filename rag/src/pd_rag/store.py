"""Chunk stores: an in-memory one (tests, demos) and Postgres + pgvector (real)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Protocol

from pd_rag.embeddings import cosine, tokenize
from pd_rag.models import Chunk, Kind


class ChunkStore(Protocol):
    async def replace_source(
        self, source: str, chunks: list[Chunk], embeddings: list[list[float]], embedder_id: str
    ) -> None:
        """Atomically make ``chunks`` the complete content of ``source`` (idempotent)."""
        ...

    async def prune(self, keep_sources: set[str]) -> int:
        """Delete every source not in ``keep_sources``; return the number of chunks removed."""
        ...

    async def vector_search(
        self, embedding: list[float], embedder_id: str, limit: int, kind: Kind | None
    ) -> list[Chunk]: ...

    async def keyword_search(
        self, terms: list[str], limit: int, kind: Kind | None
    ) -> list[Chunk]: ...

    async def count(self) -> int: ...


class MemoryStore:
    """Same observable behaviour as the Postgres store, with simple scoring."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[Chunk, list[float], str]] = {}

    async def replace_source(
        self, source: str, chunks: list[Chunk], embeddings: list[list[float]], embedder_id: str
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings differ in length")
        self._rows = {k: v for k, v in self._rows.items() if v[0].source != source}
        for c, e in zip(chunks, embeddings, strict=True):
            self._rows[c.id] = (c, e, embedder_id)

    async def prune(self, keep_sources: set[str]) -> int:
        doomed = [k for k, v in self._rows.items() if v[0].source not in keep_sources]
        for k in doomed:
            del self._rows[k]
        return len(doomed)

    async def vector_search(
        self, embedding: list[float], embedder_id: str, limit: int, kind: Kind | None
    ) -> list[Chunk]:
        rows = [
            (cosine(embedding, e), c)
            for c, e, eid in self._rows.values()
            if eid == embedder_id and (kind is None or c.kind == kind)
        ]
        return [c for _, c in sorted(rows, key=lambda r: (-r[0], r[1].id))[:limit]]

    async def keyword_search(self, terms: list[str], limit: int, kind: Kind | None) -> list[Chunk]:
        wanted = set(terms)
        docs = [
            (c, Counter(tokenize(f"{c.title} {c.section} {c.content}")))
            for c, _, _ in self._rows.values()
            if kind is None or c.kind == kind
        ]
        n = max(len(docs), 1)
        df = {t: sum(1 for _, tf in docs if t in tf) for t in wanted}
        scored = []
        for c, tf in docs:
            score = sum(
                (1 + math.log(tf[t])) * math.log(1 + n / df[t]) for t in wanted if tf.get(t)
            )
            if score > 0:
                scored.append((score, c))
        return [c for _, c in sorted(scored, key=lambda r: (-r[0], r[1].id))[:limit]]

    async def count(self) -> int:
        return len(self._rows)


_SAFE_TERM = re.compile(r"^[a-z0-9_]{2,40}$")


class PgStore:
    """PostgreSQL with pgvector. One connection per operation: ingestion and search are low-volume."""

    TABLE = "knowledge_chunks"

    def __init__(self, dsn: str, dim: int) -> None:
        if not 16 <= dim <= 2000:  # pgvector's HNSW index limit is 2000 dimensions
            raise ValueError("dim must be between 16 and 2000")
        self._dsn, self.dim = dsn, dim

    async def _connect(self, *, register: bool = True) -> Any:
        import psycopg
        from pgvector.psycopg import register_vector_async

        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=False)
        if register:
            await register_vector_async(conn)
        return conn

    async def migrate(self) -> None:
        conn = await self._connect(register=False)  # the extension may not exist yet
        async with conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.TABLE} (
                  id text PRIMARY KEY,
                  source text NOT NULL,
                  kind text NOT NULL CHECK (kind IN ('runbook', 'incident')),
                  title text NOT NULL,
                  section text NOT NULL,
                  doc_date date,
                  content text NOT NULL,
                  embedder text NOT NULL,
                  embedding vector({int(self.dim)}) NOT NULL,
                  tsv tsvector GENERATED ALWAYS AS
                    (to_tsvector('english', title || ' ' || section || ' ' || content)) STORED
                )"""
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.TABLE}_embedding_idx ON {self.TABLE} USING hnsw (embedding vector_cosine_ops)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.TABLE}_tsv_idx ON {self.TABLE} USING gin (tsv)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.TABLE}_source_idx ON {self.TABLE} (source)"
            )
            await conn.commit()

    async def replace_source(
        self, source: str, chunks: list[Chunk], embeddings: list[list[float]], embedder_id: str
    ) -> None:
        from pgvector import Vector

        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings differ in length")
        conn = await self._connect()
        async with conn:  # one transaction: readers never see a half-replaced document
            await conn.execute(f"DELETE FROM {self.TABLE} WHERE source = %s", (source,))
            for c, e in zip(chunks, embeddings, strict=True):
                await conn.execute(
                    f"""INSERT INTO {self.TABLE}
                        (id, source, kind, title, section, doc_date, content, embedder, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, embedder = EXCLUDED.embedder""",
                    (
                        c.id,
                        c.source,
                        c.kind,
                        c.title,
                        c.section,
                        c.doc_date,
                        c.content,
                        embedder_id,
                        Vector(e),
                    ),
                )
            await conn.commit()

    async def prune(self, keep_sources: set[str]) -> int:
        conn = await self._connect()
        async with conn:
            cur = await conn.execute(
                f"DELETE FROM {self.TABLE} WHERE NOT (source = ANY(%s))", (sorted(keep_sources),)
            )
            await conn.commit()
            return int(cur.rowcount)

    async def vector_search(
        self, embedding: list[float], embedder_id: str, limit: int, kind: Kind | None
    ) -> list[Chunk]:
        from pgvector import Vector

        conn = await self._connect()
        async with conn:
            cur = await conn.execute(
                f"""SELECT id, source, kind, title, section, doc_date, content FROM {self.TABLE}
                    WHERE embedder = %s AND (%s::text IS NULL OR kind = %s)
                    ORDER BY embedding <=> %s, id LIMIT %s""",
                (embedder_id, kind, kind, Vector(embedding), limit),
            )
            return [_row_to_chunk(r) for r in await cur.fetchall()]

    async def keyword_search(self, terms: list[str], limit: int, kind: Kind | None) -> list[Chunk]:
        safe = [t for t in terms if _SAFE_TERM.match(t)][
            :30
        ]  # only [a-z0-9_] ever reaches to_tsquery
        if not safe:
            return []
        conn = await self._connect()
        async with conn:
            cur = await conn.execute(
                f"""SELECT id, source, kind, title, section, doc_date, content FROM {self.TABLE},
                    to_tsquery('english', %s) AS q
                    WHERE tsv @@ q AND (%s::text IS NULL OR kind = %s)
                    ORDER BY ts_rank_cd(tsv, q) DESC, id LIMIT %s""",
                (" | ".join(safe), kind, kind, limit),
            )
            return [_row_to_chunk(r) for r in await cur.fetchall()]

    async def count(self) -> int:
        conn = await self._connect()
        async with conn:
            cur = await conn.execute(f"SELECT count(*) FROM {self.TABLE}")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def drop(self) -> None:
        """Test helper."""
        conn = await self._connect(register=False)
        async with conn:
            await conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
            await conn.commit()


def _row_to_chunk(r: tuple[Any, ...]) -> Chunk:
    return Chunk(
        id=r[0], source=r[1], kind=r[2], title=r[3], section=r[4], doc_date=r[5], content=r[6]
    )
