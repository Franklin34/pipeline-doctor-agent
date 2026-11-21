"""Ingest ``knowledge/runbooks/*.md`` and ``knowledge/incidents/*.md`` into a store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pd_rag.chunking import chunk_document
from pd_rag.embeddings import Embedder
from pd_rag.models import Kind
from pd_rag.store import ChunkStore

log = logging.getLogger("pd_rag.ingest")
MAX_FILE_BYTES = 1_000_000
KINDS: dict[str, Kind] = {"runbooks": "runbook", "incidents": "incident"}


@dataclass(frozen=True)
class IngestReport:
    documents: int
    chunks: int
    pruned_chunks: int
    skipped: tuple[str, ...]


async def ingest(
    knowledge_dir: Path,
    store: ChunkStore,
    embedder: Embedder,
    *,
    sanitize: Callable[[str], str] | None = None,
) -> IngestReport:
    """Re-ingest everything. Idempotent: each document is replaced atomically, documents that no
    longer exist are pruned. ``sanitize`` (for example a secret scrubber) runs on the raw text."""
    documents = chunks_total = 0
    seen: set[str] = set()
    skipped: list[str] = []
    for dirname, kind in KINDS.items():
        for path in sorted((knowledge_dir / dirname).glob("*.md")):
            source = f"{dirname}/{path.name}"
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                skipped.append(f"{source} (symlink or larger than {MAX_FILE_BYTES} bytes)")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks = chunk_document(source, kind, sanitize(text) if sanitize else text)
            if not chunks:
                skipped.append(f"{source} (no content)")
                continue
            vectors = await embedder.embed([c.embed_text for c in chunks])
            await store.replace_source(source, chunks, vectors, embedder.id)
            seen.add(source)
            documents += 1
            chunks_total += len(chunks)
    pruned = await store.prune(seen)
    log.info(
        "ingest finished", extra={"documents": documents, "chunks": chunks_total, "pruned": pruned}
    )
    return IngestReport(documents, chunks_total, pruned, tuple(skipped))
