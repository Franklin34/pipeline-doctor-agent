from __future__ import annotations

import itertools
from datetime import date

import pytest
from pd_rag.chunking import chunk_document, parse_front_matter, split_sections, split_text

DOC = """---
title: Postgres down
date: 2026-08-20
---
# Postgres down

Intro paragraph.

## Diagnosis

Check the service.

### Network

Check the alias.

## Fix

Restart it.
"""


def test_front_matter() -> None:
    meta, body = parse_front_matter(DOC)
    assert meta == {"title": "Postgres down", "date": "2026-08-20"} and body.lstrip().startswith(
        "# Postgres down"
    )
    assert parse_front_matter("no front matter") == ({}, "no front matter")
    assert parse_front_matter("---\ntitle: x\nnever closed")[0] == {}


def test_sections_follow_heading_hierarchy() -> None:
    chunks = chunk_document("runbooks/pg.md", "runbook", DOC)
    assert [c.section for c in chunks] == [
        "Postgres down",
        "Postgres down > Diagnosis",
        "Postgres down > Diagnosis > Network",
        "Postgres down > Fix",
    ]
    assert all(
        c.title == "Postgres down" and c.doc_date == date(2026, 8, 20) and c.kind == "runbook"
        for c in chunks
    )
    assert chunks[3].content == "Restart it."


def test_hash_inside_code_fence_is_not_a_heading() -> None:
    body = "# T\n\n```sh\n# a comment, not a heading\necho hi\n```\n"
    sections = split_sections(body, "T")
    assert len(sections) == 1 and "# a comment" in sections[0][1]


def test_title_falls_back_to_h1_then_filename() -> None:
    assert chunk_document("a/b.md", "incident", "# Heading\n\ntext")[0].title == "Heading"
    assert chunk_document("a/my-note.md", "incident", "just text")[0].title == "my-note"
    assert (
        chunk_document("a/b.md", "incident", "---\ndate: not-a-date\n---\ntext")[0].doc_date is None
    )


def test_empty_and_heading_only_documents_have_no_chunks() -> None:
    assert chunk_document("a.md", "runbook", "") == []
    assert chunk_document("a.md", "runbook", "# Only a heading\n\n## Another\n") == []


def test_ids_are_stable_and_content_sensitive() -> None:
    a = chunk_document("a.md", "runbook", DOC)
    assert [c.id for c in a] == [c.id for c in chunk_document("a.md", "runbook", DOC)]
    assert [c.id for c in a] != [c.id for c in chunk_document("b.md", "runbook", DOC)]
    assert len({c.id for c in a}) == len(a)
    assert (
        a[3].id != chunk_document("a.md", "runbook", DOC.replace("Restart it.", "Reboot it."))[3].id
    )


def test_long_sections_split_with_overlap_and_never_exceed_max() -> None:
    paragraphs = [f"Paragraph {i} " + ("word " * 60).strip() + "." for i in range(30)]
    pieces = split_text("\n\n".join(paragraphs), max_chars=1000, overlap=150)
    assert len(pieces) > 3 and all(len(p) <= 1000 for p in pieces)
    for prev, nxt in itertools.pairwise(pieces):
        tail = prev[-120:].split(" ", 1)[
            -1
        ]  # the end of the previous piece reappears at the start of the next
        assert tail[:40] in nxt[:400]
    covered = "\n\n".join(pieces)
    assert all(f"Paragraph {i} " in covered for i in range(30))  # nothing lost


def test_single_giant_paragraph_is_cut() -> None:
    pieces = split_text("x" * 5000, max_chars=1000, overlap=100)
    assert all(len(p) <= 1000 for p in pieces) and sum(len(p) for p in pieces) >= 5000


def test_invalid_sizes() -> None:
    with pytest.raises(ValueError):
        split_text("a" * 5000, max_chars=100, overlap=100)
