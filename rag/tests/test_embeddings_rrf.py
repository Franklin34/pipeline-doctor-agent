from __future__ import annotations

import math

import pytest
from pd_rag import HashEmbedder, rrf
from pd_rag.embeddings import cosine, tokenize


async def test_hash_embedder_is_deterministic_unit_length_and_sized() -> None:
    e = HashEmbedder(128)
    [a1, b1] = await e.embed(["Postgres connection refused", "unrelated cooking recipe"])
    [a2] = await e.embed(["Postgres connection refused"])
    assert a1 == a2 and len(a1) == 128 == e.dim and e.id == "hash-128"
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0, rel_tol=1e-9)
    assert math.isclose(cosine(a1, a1), 1.0, rel_tol=1e-9)
    assert cosine(a1, b1) < 0.5


async def test_similar_text_is_closer_than_dissimilar() -> None:
    e = HashEmbedder()
    [q, near, far] = await e.embed(
        [
            "postgres connection refused in integration tests",
            "integration tests: postgres connection refused error",
            "assert discount formula inverted",
        ],
        query=True,
    )
    assert cosine(q, near) > cosine(q, far)


async def test_empty_text_gives_a_zero_vector_not_a_crash() -> None:
    [v] = await HashEmbedder().embed([""])
    assert not any(v)


def test_tokenize_drops_stop_words_and_case() -> None:
    assert tokenize("The Postgres IS down, to be fixed!") == ["postgres", "down", "fixed"]


def test_hash_embedder_validates_dim() -> None:
    with pytest.raises(ValueError):
        HashEmbedder(4)


def test_rrf_scores_and_ordering() -> None:
    scores = rrf([["a", "b", "c"], ["c", "a", "d"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62) and scores["d"] == pytest.approx(1 / 63)
    assert max(scores, key=lambda k: scores[k]) == "a"


def test_rrf_rewards_agreement_between_lists() -> None:
    both = rrf([["x", "y"], ["z", "x"]])
    assert both["x"] > both["y"] and both["x"] > both["z"]  # found by both beats rank 1 in one only


def test_rrf_edge_cases() -> None:
    assert rrf([]) == {} and rrf([[], []]) == {}
    assert rrf([["only"]]) == {"only": pytest.approx(1 / 61)}
