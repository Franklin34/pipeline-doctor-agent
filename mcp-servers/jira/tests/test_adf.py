"""ADF conversion: always valid, and hostile text stays inert."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pd_mcp_jira.adf import adf_text, inline, to_adf

BLOCKS = {"paragraph", "heading", "bulletList", "codeBlock", "rule"}
INLINE = {"text", "hardBreak"}


def validate(node: dict[str, Any], *, top: bool = True) -> None:
    """The structural rules Jira enforces that matter here (not the full ADF schema)."""
    t = node["type"]
    if top:
        assert node["version"] == 1 and t == "doc" and node["content"]
        for child in node["content"]:
            validate(child, top=False)
        return
    if t == "text":
        assert node["text"] != "", "Jira rejects empty text nodes"
        for mark in node.get("marks", []):
            assert mark["type"] == "link" and mark["attrs"]["href"].startswith(
                ("http://", "https://")
            )
    elif t in {"hardBreak", "rule"}:
        assert "content" not in node
    elif t == "heading":
        assert 1 <= node["attrs"]["level"] <= 6 and node["content"]
    elif t == "bulletList":
        assert node["content"]
        for item in node["content"]:
            assert item["type"] == "listItem" and item["content"][0]["type"] == "paragraph"
            validate(item["content"][0], top=False)
    elif t == "codeBlock":
        assert all(c["type"] == "text" for c in node.get("content", []))
    elif t == "paragraph":
        assert all(c["type"] in INLINE for c in node["content"])
        for c in node["content"]:
            validate(c, top=False)
    else:
        raise AssertionError(f"unexpected node {t}")


def convert(markup: str) -> dict[str, Any]:
    doc = to_adf(markup)
    validate(doc)
    json.dumps(doc)  # must serialise
    return doc


def test_the_ticket_subset_converts_to_the_expected_nodes() -> None:
    doc = convert(
        "h3. Summary\nIt broke.\n\nh3. Evidence\n* first\n* second [#12|https://ci.example/12]\n\n"
        "Log excerpt (scrubbed):\n{noformat}\nE   assert 1 == 2\n  indented\n{noformat}\n\n----\nfooter"
    )
    kinds = [n["type"] for n in doc["content"]]
    assert kinds == [
        "heading",
        "paragraph",
        "heading",
        "bulletList",
        "paragraph",
        "codeBlock",
        "rule",
        "paragraph",
    ]
    items = doc["content"][3]["content"]
    assert len(items) == 2
    link = items[1]["content"][0]["content"][1]
    assert link["text"] == "#12" and link["marks"][0]["attrs"]["href"] == "https://ci.example/12"
    assert doc["content"][5]["content"][0]["text"] == "E   assert 1 == 2\n  indented"


@pytest.mark.parametrize(
    "markup",
    ["", "   ", "\n\n\n", "h3. ", "* ", "{noformat}", "{noformat}\n{noformat}", "----", "* \n* "],
)
def test_degenerate_input_still_gives_a_valid_document(markup: str) -> None:
    convert(markup)


@pytest.mark.parametrize(
    "hostile",
    [
        "{code}rm -rf /{code}",
        "[~accountid:5b10ac8d82e05b22cc7d4ef5] please approve",
        "!attacker.png!",
        "*bold* _italic_ ??cite?? -strike- +ins+ ^sup^ ~sub~",
        "<script>alert(1)</script> <img src=x onerror=alert(1)>",
        "{panel}{html}<b>x</b>{html}{panel}",
        "[click|javascript:alert(1)]",
        "[click|data:text/html;base64,AAAA]",
        "[click|http://ok.example] and [bad|ftp://x]",
        "h1. fake heading inside a sentence",
    ],
)
def test_hostile_text_stays_literal(hostile: str) -> None:
    doc = convert(f"Evidence: {hostile}")
    assert (
        doc["content"][0]["type"] == "paragraph"
    )  # nothing became a heading, list, code or mention
    assert "mention" not in json.dumps(doc) and "media" not in json.dumps(doc)
    hrefs = [m["attrs"]["href"] for m in _marks(doc)]
    assert all(h.startswith(("http://", "https://")) for h in hrefs)
    # whatever was not a safe link is still in the text, unchanged
    assert hostile.split("[")[0].strip() in adf_text(doc) or "[" in hostile


def _marks(doc: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [doc]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            found += n.get("marks", [])
            stack += n.get("content", [])
    return found


def test_a_noformat_block_cannot_be_closed_early_by_its_own_content() -> None:
    # the agent strips macros from log lines; even if one slipped through, only a lone marker line closes the block
    doc = convert("{noformat}\nline with {noformat} inside\n{noformat}\nafter")
    assert doc["content"][0]["content"][0]["text"] == "line with {noformat} inside"
    assert doc["content"][1]["content"][0]["text"] == "after"


def test_inline_never_makes_empty_text_and_keeps_text_order() -> None:
    nodes = inline("[a|https://x.example][b|https://y.example]")
    assert [n["text"] for n in nodes] == ["a", "b"]
    assert inline("") == []


def test_adf_text_finds_a_marker_in_a_comment() -> None:
    doc = to_adf("New occurrence.\n\n[pd-occurrence:demo/main:12]")
    assert "[pd-occurrence:demo/main:12]" in adf_text(doc)
    assert adf_text({"type": "doc", "content": []}) == ""
