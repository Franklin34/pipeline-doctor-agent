from __future__ import annotations

import os

import pytest
from pd_rag import PgStore

DSN = os.environ.get("PD_TEST_DATABASE_URL")


def test_dim_is_validated() -> None:
    for bad in (4, 5000):
        with pytest.raises(ValueError):
            PgStore("postgresql://x", bad)


@pytest.mark.skipif(not DSN, reason="set PD_TEST_DATABASE_URL")
async def test_migrate_is_idempotent_and_creates_indexes() -> None:
    assert DSN
    pg = PgStore(DSN, 256)
    await pg.drop()
    await pg.migrate()
    await pg.migrate()
    conn = await pg._connect()
    async with conn:
        cur = await conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'knowledge_chunks'"
        )
        names = {r[0] for r in await cur.fetchall()}
    assert {
        "knowledge_chunks_embedding_idx",
        "knowledge_chunks_tsv_idx",
        "knowledge_chunks_source_idx",
    } <= names
    await pg.drop()
