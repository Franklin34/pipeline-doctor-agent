# ADR 0005: Hybrid retrieval on Postgres + pgvector with reciprocal rank fusion

Status: accepted

## Context
Failure text mixes exact identifiers (exception types, test names, fingerprints) with fuzzy meaning (a paraphrased symptom). Pure vector search misses exact tokens; pure keyword search misses paraphrases. We also want one datastore that works locally and as Cloud SQL.

## Decision
- One PostgreSQL table with a `vector` column (HNSW, cosine) and a generated `tsvector` column (GIN). Both rankings come from the same table.
- Merge by reciprocal rank fusion (k = 60): rank-based, no score normalisation, no tuning weights.
- Cap results per document so one long runbook cannot crowd out the matching incident.
- Hide stores and embedders behind small interfaces. The in-memory store and the Postgres store run the same contract tests; the hash embedder keeps the suite offline and deterministic, Gemini is the real embedder.
- Treat everything retrieved as untrusted input (escaping, re-scrubbing, labelling, an agent with no other tools).

## Consequences
- No extra vector database to run; Cloud SQL supports pgvector.
- Two stores must keep identical semantics; the shared contract tests are the guard (and already caught a real issue: results dominated by one document).
- The hash embedder is not semantic, so tests prove the plumbing, not retrieval quality. Quality needs the real embedder and an eval set (Phase 9).
- Changing the embedding model requires re-ingestion into a new table.
