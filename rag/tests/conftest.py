from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pd_rag import ChunkStore, HashEmbedder, MemoryStore, PgStore

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge"
DSN = os.environ.get("PD_TEST_DATABASE_URL")
DIM = 256


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder(DIM)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ChunkStore]:
    """Every contract test runs against both stores (Postgres only if PD_TEST_DATABASE_URL is set)."""
    if request.param == "memory":
        yield MemoryStore()
        return
    if not DSN:
        pytest.skip("set PD_TEST_DATABASE_URL to a pgvector-enabled Postgres to run these")
    pg = PgStore(DSN, DIM)
    await pg.drop()
    await pg.migrate()
    yield pg
    await pg.drop()


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """A private copy of the real knowledge base that tests may modify."""
    dest = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE, dest)
    return dest
