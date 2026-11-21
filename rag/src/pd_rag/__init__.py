"""RAG over runbooks and past incidents: chunking, embeddings, stores, hybrid search."""

from pd_rag.chunking import chunk_document
from pd_rag.embeddings import Embedder, GeminiEmbedder, HashEmbedder
from pd_rag.ingest import IngestReport, ingest
from pd_rag.models import Chunk, Kind, SearchHit
from pd_rag.search import HybridSearcher, rrf
from pd_rag.store import ChunkStore, MemoryStore, PgStore

__all__ = [
    "Chunk",
    "ChunkStore",
    "Embedder",
    "GeminiEmbedder",
    "HashEmbedder",
    "HybridSearcher",
    "IngestReport",
    "Kind",
    "MemoryStore",
    "PgStore",
    "SearchHit",
    "chunk_document",
    "ingest",
    "rrf",
]
