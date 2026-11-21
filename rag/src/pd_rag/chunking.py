"""Markdown -> chunks: split by headings, then by size with overlap."""

from __future__ import annotations

import hashlib
import re
from datetime import date

from pd_rag.models import Chunk, Kind

MAX_CHARS = 1200
OVERLAP_CHARS = 200
_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Minimal ``key: value`` front matter between ``---`` lines (no YAML dependency)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return {}, text  # unterminated front matter: treat the whole file as body


def split_sections(body: str, doc_title: str) -> list[tuple[str, str]]:
    """Return (heading path, text) pairs. ``#`` inside code fences is not a heading."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            path = " > ".join(h for _, h in stack) or doc_title
            sections.append((path, text))
        buf.clear()

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
        else:
            buf.append(line)
    flush()
    return sections


def split_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Pack paragraphs into pieces of at most ``max_chars``; each piece after the first starts
    with the tail of the previous one (``overlap`` chars, cut at a word boundary)."""
    if overlap < 0 or max_chars < 2 * overlap + 50:
        raise ValueError("max_chars must be at least 2 * overlap + 50")
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    # Paragraphs leave room for the overlap tail, so no piece ever exceeds max_chars.
    for para in _paragraphs(text, max_chars - overlap - 2):
        if current and len(current) + len(para) + 2 > max_chars:
            pieces.append(current)
            current = _tail(current, overlap)
        current = f"{current}\n\n{para}" if current else para
    if current.strip():
        pieces.append(current)
    return pieces


def _paragraphs(text: str, max_chars: int) -> list[str]:
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > max_chars:  # a single huge paragraph: cut at the last space
            cut = para.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            out.append(para)
    return out


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail


def chunk_document(source: str, kind: Kind, text: str) -> list[Chunk]:
    """Turn one markdown file into chunks with stable ids (same input -> same ids)."""
    meta, body = parse_front_matter(text)
    first_h1 = next(
        (
            m.group(2)
            for line in body.splitlines()
            if (m := _HEADING.match(line)) and len(m.group(1)) == 1
        ),
        None,
    )
    title = meta.get("title") or first_h1 or source.rsplit("/", 1)[-1].removesuffix(".md")
    doc_date: date | None = None
    if meta.get("date"):
        try:
            doc_date = date.fromisoformat(meta["date"])
        except ValueError:
            doc_date = None

    chunks: list[Chunk] = []
    for section, section_text in split_sections(body, title):
        for index, piece in enumerate(split_text(section_text)):
            digest = hashlib.sha256(f"{source}\0{section}\0{index}\0{piece}".encode()).hexdigest()[
                :32
            ]
            chunks.append(
                Chunk(
                    id=digest,
                    source=source,
                    kind=kind,
                    title=title,
                    section=section,
                    doc_date=doc_date,
                    content=piece,
                )
            )
    return chunks
