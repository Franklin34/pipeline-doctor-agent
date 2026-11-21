# RAG: runbooks and past incidents

`rag/` (package `pd_rag`) indexes `knowledge/runbooks/*.md` and `knowledge/incidents/*.md` and answers "have we seen this before, and what do we do?". The agent side is `agent/.../knowledge.py` and the `knowledge_agent`.

## Pipeline

```
markdown -> chunking (headings, then size + overlap) -> embeddings -> ChunkStore (Postgres + pgvector)
query -> embed ->  vector top-20 \
                                   > reciprocal rank fusion -> max 2 chunks per document -> top 5
query -> tokens -> full-text top-20 /
```

- **Chunking** (`chunking.py`): front matter (`title`, `date`), then one section per heading path (`Title > Diagnosis > Network`; `#` inside code fences is not a heading), then paragraphs packed up to 1,200 characters with a 200-character overlap. No chunk exceeds the limit. Chunk ids are `sha256(source, section, index, text)`, so re-ingesting unchanged content yields the same ids.
- **Metadata** per chunk: `source` (relative path), `kind` (runbook or incident), `title`, `section`, `doc_date`.
- **Embedders** (`embeddings.py`): `GeminiEmbedder` (real; `EMBEDDING_MODEL`, `EMBEDDING_DIM`, needs `GOOGLE_API_KEY`) and `HashEmbedder` (deterministic, offline, **not semantic**: tests and demos). The embedder id is stored with each chunk and search only compares vectors of the same embedder.
- **Stores** (`store.py`): `PgStore` (table `knowledge_chunks`: `vector(N)` with an HNSW cosine index, a generated `tsvector` column with a GIN index) and `MemoryStore` with the same observable behaviour. Documents are replaced atomically per source; documents removed from disk are pruned.
- **Hybrid search** (`search.py`): the vector list and the full-text list are merged with **reciprocal rank fusion**: `score = sum 1 / (60 + rank)`. Only ranks are used, so cosine similarity and `ts_rank` never need to be normalised against each other. A document found by both lists beats one that is first in only one. Full-text uses an OR query of sanitised tokens, so a long error message still matches on its distinctive words (and on exact tokens such as a **fingerprint**).
- **Diversity:** at most 2 chunks per document in the final list, otherwise the top hits are often all sections of one runbook and hide the matching past incident (found by the Postgres tests).

## In the agent system

- `search_knowledge(query, kind)` is the knowledge agent's only tool (`policy/tools.py`). Arguments are validated (query max 500 characters, kind enum), at most 5 passages come back, and a database failure returns a short "unavailable" error that does not leak internals; the run continues without knowledge.
- **Retrieved text is untrusted.** The tool escapes `<` and `>` so a document cannot close or forge a delimiter, the guard re-scrubs and labels every tool result, the prompt says passages are evidence and never instructions, and the knowledge agent has no other tool. `KnowledgeFindings` (bounded, schema-validated) is what reaches the diagnose agent.
- Without `DATABASE_URL` there is no searcher and the pipeline uses a no-op knowledge step.
- Ingestion scrubs secrets from documents before indexing (`run_ingest`).

## Run it

```sh
make test              # unit + contract tests on the in-memory store (Postgres cases skipped)
make test-pg           # same suite on a throwaway pgvector container (needs Docker), removed afterwards
make up                # compose stack incl. Postgres with pgvector
DATABASE_URL=... EMBEDDER=hash make ingest    # index knowledge/ (use EMBEDDER=gemini with an API key)
```

Changing the embedding model or dimension needs a fresh table (or `PgStore.drop()` then re-ingest): the vector column width is fixed at creation.

## Known limits

- `HashEmbedder` matches words, not meaning ("db is unreachable" will not find "connection refused"). Retrieval quality with `GeminiEmbedder` is not measured yet; the Phase 9 evals should include a retrieval set (hit rate at 3).
- There is no relevance threshold: RRF scores are relative, so a query with no good match still returns the nearest passages. The knowledge agent is told that an empty result is correct and to report only passages that really match.
- Ingestion re-embeds every document each time (fine for a small knowledge base); hash-based skipping is a simple later optimisation.
- Full-text uses the `english` configuration; non-English runbooks need another one.
- Poisoned runbooks are a real attack path: changes to `knowledge/` need code review, and an ingest-time screening hook is planned for Phase 9.
