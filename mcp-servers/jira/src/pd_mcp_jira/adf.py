"""Convert the small wiki-markup subset our tickets use into Atlassian Document Format (ADF).

Jira Cloud's v3 API takes descriptions and comments as ADF, a JSON tree. The agent renders tickets
in a restricted subset of wiki markup (headings, bullets, links, one preformatted block, a rule,
paragraphs); this module turns that into ADF.

Why this is also a security feature: the output is built from *nodes*, never by pasting text into
markup. A log line containing ``{code}``, ``[~accountid]``, ``*bold*``, ``!image.png!`` or an HTML
tag therefore stays inert literal text. The only text that becomes a link is ``[label|url]`` with
an ``http(s)`` URL; anything else (``javascript:``, ``data:``) stays plain text.
"""

from __future__ import annotations

import re
from typing import Any

Node = dict[str, Any]

_HEADING = re.compile(r"^h([1-6])\.\s+(.*)$")
_BULLET = re.compile(r"^\*\s+(.*)$")
_RULE = re.compile(r"^-{4,}\s*$")
_LINK = re.compile(r"\[([^\[\]|]{1,200})\|(https?://[^\s|\[\]]{1,500})\]")
_NOFORMAT = "{noformat}"
MAX_TEXT_NODE = 30_000  # Jira rejects overlong fields; callers cap well below this


def _text(value: str, *, marks: list[Node] | None = None) -> Node:
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def inline(value: str) -> list[Node]:
    """Plain text with ``[label|url]`` links; never an empty text node (Jira refuses those)."""
    out: list[Node] = []
    pos = 0
    for m in _LINK.finditer(value):
        if m.start() > pos:
            out.append(_text(value[pos : m.start()]))
        out.append(_text(m.group(1), marks=[{"type": "link", "attrs": {"href": m.group(2)}}]))
        pos = m.end()
    if pos < len(value):
        out.append(_text(value[pos:]))
    return [n for n in out if n["text"]]


def _paragraph(lines: list[str]) -> Node | None:
    content: list[Node] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content += inline(line)
    return {"type": "paragraph", "content": content} if content else None


def to_adf(markup: str) -> Node:
    """The ADF document for ``markup``. Always valid; empty input gives an empty paragraph."""
    blocks: list[Node] = []
    para: list[str] = []
    bullets: list[str] = []
    lines = markup.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def flush_para() -> None:
        if para:
            if (p := _paragraph(para)) is not None:
                blocks.append(p)
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            items = [
                {"type": "listItem", "content": [{"type": "paragraph", "content": inline(b)}]}
                for b in bullets
                if inline(b)
            ]
            if items:
                blocks.append({"type": "bulletList", "content": items})
            bullets.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.strip() == _NOFORMAT:  # preformatted block, up to the next marker (or the end)
            flush_para()
            flush_bullets()
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != _NOFORMAT:
                body.append(lines[i])
                i += 1
            code = "\n".join(body)
            node: Node = {"type": "codeBlock"}
            if code.strip():
                node["content"] = [_text(code)]
            blocks.append(node)
        elif _RULE.match(line):
            flush_para()
            flush_bullets()
            blocks.append({"type": "rule"})
        elif m := _HEADING.match(line):
            flush_para()
            flush_bullets()
            content = inline(m.group(2))
            if content:
                blocks.append(
                    {"type": "heading", "attrs": {"level": int(m.group(1))}, "content": content}
                )
        elif m := _BULLET.match(line):
            flush_para()
            bullets.append(m.group(1))
        elif not line.strip():
            flush_para()
            flush_bullets()
        else:
            flush_bullets()
            para.append(line)
        i += 1
    flush_para()
    flush_bullets()
    if not blocks:
        blocks.append({"type": "paragraph", "content": []})
    return {"version": 1, "type": "doc", "content": blocks}


def adf_text(node: Any) -> str:
    """All text in an ADF tree, for finding an idempotency marker in an existing comment."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        parts = [adf_text(c) for c in node.get("content", [])]
        sep = "\n" if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock"} else ""
        return sep.join(parts) if sep else "".join(parts)
    if isinstance(node, list):
        return "\n".join(adf_text(c) for c in node)
    return ""
