"""Input screening: spot text that tries to give instructions to an AI.

Build logs, commit messages and documents are written by people (or by attackers who control a
dependency, a branch or a test name). This module looks for the signs of a prompt injection in that
text. It is a *tripwire, not a wall*: a clever attacker can phrase around any list of patterns.
What makes the system safe is that a model has no write tools, the policy layer refuses
out-of-scope calls, and the gate sends everything unusual to a human. Screening adds two things on
top: a flagged run can never be auto-fixed (the gate blocks it), and the ticket tells the reviewer
why, which is also a detection signal for the security team.

A false positive is cheap (the failure still gets a ticket, just no automatic fix); a missed
injection is caught by the layers behind. So the rules favour recall.

Safe by construction: input is size-capped, matching runs on normalised text, and every quantifier
is bounded, so crafted input cannot make the scan slow (the scrubber once suffered from this).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MAX_SCAN_CHARS = 200_000

# Characters that hide text from a human reviewer but not from a model.
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")

_WRITE_TOOLS = (
    "create_branch|commit_files|open_draft_pull_request|create_issue|add_comment|"
    "update_issue|rebuild|trigger_build|merge"
)

# kind -> pattern, applied to lower-cased NFKC-normalised text with whitespace collapsed.
RULES: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,30}\b(?:instructions?|polic(?:y|ies)|rules?|guardrails?|prompts?|constraints?|restrictions?)\b"
    ),
    "addressed_to_ai": re.compile(
        r"\b(?:notice|note|message|instructions?|attention|important)\b.{0,20}\b(?:to|for)\b.{0,12}\b(?:ai|llm|assistant|agent|model|bot|gemini|claude|gpt)\b"
        r"|\bas an? (?:ai|language model|assistant)\b|\bdear (?:ai|assistant|agent|model)\b"
    ),
    "role_hijack": re.compile(
        r"\byou are now\b|\bnew (?:system )?(?:instructions?|prompt|role)\b|\bsystem prompt\b|\bact as\b.{0,20}\b(?:admin|root|developer|maintainer)\b|\bfrom now on\b.{0,20}\byou\b"
    ),
    "approval_demand": re.compile(
        r"\b(?:approve|accept|merge|ship|deploy)\b.{0,20}\b(?:all|every|this|the)\b.{0,20}\b(?:pull requests?|prs?|changes?|commits?|builds?)\b|\bauto[- ]?merge\b|\bskip (?:the )?(?:review|tests?|checks?)\b"
    ),
    "tool_invocation": re.compile(
        rf"\b(?:call|run|invoke|use|execute|trigger)\b.{{0,25}}\b(?:{_WRITE_TOOLS})\b|\b(?:{_WRITE_TOOLS})\s*\("
    ),
    "exfiltration": re.compile(
        r"\b(?:send|post|upload|email|print|reveal|leak|exfiltrate|include)\b.{0,30}\b(?:secrets?|tokens?|passwords?|credentials?|api[_ -]?keys?|env(?:ironment)? var\w*)\b"
    ),
    "delimiter_forgery": re.compile(
        r"<<<\s*(?:begin|end)_|</?\s*(?:system|assistant|failure_event|pre_analysis)\b|^\s*role:\s*\w+|\[/?(?:inst|system)\]",
        re.MULTILINE,
    ),
}
HIDDEN_KIND = "hidden_text"
KINDS = frozenset(RULES) | {HIDDEN_KIND}


@dataclass(frozen=True)
class Flag:
    kind: str  # one of KINDS
    where: str  # the source: a tool name or "build_output"

    def __str__(self) -> str:
        return f"{self.kind.replace('_', ' ')} in {self.where}"


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def screen(text: str, where: str) -> list[Flag]:
    """Flags found in ``text``; one per kind. Never raises and never echoes the text."""
    text = text[:MAX_SCAN_CHARS]
    flags: list[Flag] = []
    if _HIDDEN.search(text):
        flags.append(Flag(HIDDEN_KIND, where))
    # Hidden characters are removed before matching, so splitting a phrase with zero-width
    # characters ("ig​nore") does not evade the rules.
    clean = _normalise(_HIDDEN.sub("", text))
    for kind, pattern in RULES.items():
        if pattern.search(clean):
            flags.append(Flag(kind, where))
    return flags


def strings_in(obj: Any, *, limit: int = MAX_SCAN_CHARS) -> str:
    """All string values of a nested structure, joined for scanning (bounded)."""
    parts: list[str] = []
    size = 0
    stack: list[Any] = [obj]
    while stack and size < limit:
        cur = stack.pop()
        if isinstance(cur, str):
            parts.append(cur)
            size += len(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list | tuple):
            stack.extend(cur)
    return "\n".join(parts)


def dedupe(flags: Iterable[Flag]) -> tuple[Flag, ...]:
    return tuple(dict.fromkeys(flags))
