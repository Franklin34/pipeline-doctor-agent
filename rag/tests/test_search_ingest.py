"""Contract tests: identical behaviour on the in-memory store and on Postgres + pgvector."""

from __future__ import annotations

from pathlib import Path

from pd_rag import ChunkStore, HashEmbedder, HybridSearcher, chunk_document, ingest

FINGERPRINT = "fdacb5a1cf4e5ebe"


async def loaded(store: ChunkStore, embedder: HashEmbedder, kb: Path) -> HybridSearcher:
    report = await ingest(kb, store, embedder)
    assert report.documents == 6 and report.chunks >= 6 and not report.skipped
    return HybridSearcher(store, embedder)


async def test_connection_refused_finds_runbook_and_incident(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    hits = await s.search(
        "psycopg2.OperationalError connection to server at postgres port 5432 failed: Connection refused"
    )
    sources = [h.chunk.source for h in hits]
    assert sources[0] in {
        "runbooks/postgres-connection-refused-in-ci.md",
        "incidents/2026-09-14-orders-db-unreachable.md",
    }
    assert "runbooks/postgres-connection-refused-in-ci.md" in sources[:3]
    assert "incidents/2026-09-14-orders-db-unreachable.md" in sources[:3]


async def test_exact_fingerprint_token_finds_the_incident(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    top = (await s.search(f"fingerprint {FINGERPRINT}"))[0]
    assert (
        top.chunk.source == "incidents/2026-09-14-orders-db-unreachable.md"
        and top.keyword_rank == 1
    )


async def test_assertion_failure_finds_refactor_knowledge(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    hits = await s.search("AssertionError assert 10.0 == 90.0 test_apply_discount")
    sources = [h.chunk.source for h in hits]
    assert {
        "incidents/2026-10-02-discount-rounding-regression.md",
        "runbooks/pytest-assertion-failure-after-refactor.md",
    } <= set(sources[:3])


async def test_kind_filter(store: ChunkStore, embedder: HashEmbedder, kb: Path) -> None:
    s = await loaded(store, embedder, kb)
    assert {
        h.chunk.kind
        for h in await s.search("postgres connection refused", kind="incident", limit=10)
    } == {"incident"}
    assert {
        h.chunk.kind
        for h in await s.search("postgres connection refused", kind="runbook", limit=10)
    } == {"runbook"}


async def test_hits_carry_source_metadata_and_rank_explanations(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    hit = (await s.search("flaky test intermittent failure rerun passes"))[0]
    assert hit.chunk.source == "runbooks/flaky-tests.md" and hit.chunk.doc_date is not None
    assert hit.chunk.title and hit.chunk.section and hit.score > 0
    assert hit.vector_rank is not None


async def test_limit_and_blank_queries(store: ChunkStore, embedder: HashEmbedder, kb: Path) -> None:
    s = await loaded(store, embedder, kb)
    assert len(await s.search("postgres", limit=2)) == 2
    assert await s.search("   ") == [] and await s.search("postgres", limit=0) == []


async def test_nonsense_query_has_no_keyword_matches(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    assert all(h.keyword_rank is None for h in await s.search("zzzzqqqq xxxyyyy"))


async def test_hostile_query_text_is_harmless(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    before = await store.count()
    for q in [
        "foo'; DROP TABLE knowledge_chunks; --",
        "a & | ! ( ) : * <-> b",
        "\x00 null byte",
        "x" * 50_000,
        "' OR 1=1 --",
    ]:
        await s.search(q)  # must not raise
    assert await store.count() == before


async def test_ingest_is_idempotent(store: ChunkStore, embedder: HashEmbedder, kb: Path) -> None:
    await ingest(kb, store, embedder)
    n = await store.count()
    again = await ingest(kb, store, embedder)
    assert await store.count() == n and again.pruned_chunks == 0


async def test_changed_document_is_replaced_and_removed_one_pruned(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    s = await loaded(store, embedder, kb)
    (kb / "runbooks" / "flaky-tests.md").write_text(
        "# Handling flaky tests\n\nQuarantine is forbidden; use the wombat protocol.\n"
    )
    (kb / "incidents" / "2026-10-02-discount-rounding-regression.md").unlink()
    report = await ingest(kb, store, embedder)
    assert report.documents == 5 and report.pruned_chunks >= 1
    assert "wombat" in (await s.search("wombat protocol"))[0].chunk.content
    sources = {
        h.chunk.source for h in await s.search("discount rounding regression refactor", limit=20)
    }
    assert "incidents/2026-10-02-discount-rounding-regression.md" not in sources
    assert not any(
        "intermittent" in h.chunk.content
        for h in await s.search("intermittent flaky rerun passes", limit=20)
    )


async def test_vectors_from_another_embedder_are_never_compared(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    await ingest(kb, store, embedder)

    class Other(HashEmbedder):
        def __init__(self) -> None:
            super().__init__(256)
            self.id = "some-other-model"

    hits = await HybridSearcher(store, Other()).search(f"fingerprint {FINGERPRINT}")
    assert hits and all(
        h.vector_rank is None for h in hits
    )  # keyword side still works; vector side is empty


async def test_sanitize_hook_runs_before_indexing(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    await ingest(kb, store, embedder, sanitize=lambda t: t.replace(FINGERPRINT, "[REDACTED]"))
    hits = await HybridSearcher(store, embedder).search(FINGERPRINT)
    assert not any(FINGERPRINT in h.chunk.content for h in hits)


async def test_symlinks_and_oversized_files_are_skipped(
    store: ChunkStore, embedder: HashEmbedder, kb: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside.md"
    secret.write_text("# outside\n\nshould never be indexed")
    (kb / "runbooks" / "link.md").symlink_to(secret)
    (kb / "runbooks" / "huge.md").write_text("x" * 1_100_000)
    report = await ingest(kb, store, embedder)
    assert len(report.skipped) == 2 and report.documents == 6
    assert not any(
        "never be indexed" in h.chunk.content
        for h in await HybridSearcher(store, embedder).search("should never be indexed")
    )


async def test_chunk_count_matches_chunker(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    await ingest(kb, store, embedder)
    expected = sum(
        len(chunk_document(f"{d}/{p.name}", k, p.read_text()))
        for d, k in (("runbooks", "runbook"), ("incidents", "incident"))
        for p in (kb / d).glob("*.md")
    )
    assert await store.count() == expected


async def test_results_are_diversified_across_sources(
    store: ChunkStore, embedder: HashEmbedder, kb: Path
) -> None:
    await ingest(kb, store, embedder)
    hits = await HybridSearcher(store, embedder, max_per_source=1).search(
        "postgres connection refused", limit=10
    )
    sources = [h.chunk.source for h in hits]
    assert len(sources) == len(set(sources))
    capped = await HybridSearcher(store, embedder, max_per_source=2).search(
        "postgres connection refused", limit=10
    )
    from collections import Counter

    assert max(Counter(h.chunk.source for h in capped).values()) <= 2
