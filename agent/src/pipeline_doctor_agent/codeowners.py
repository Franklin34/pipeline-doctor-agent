"""CODEOWNERS matching (GitHub semantics, the useful subset) and assignee selection."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]


def _regex(pattern: str) -> re.Pattern[str]:
    """Translate a CODEOWNERS (gitignore-style) pattern into a regex over repo-relative paths.

    * ``/x`` or a slash in the middle anchors to the repository root; otherwise it matches at any depth.
    * ``*`` stays inside one path segment, ``**`` crosses segments.
    * A pattern also covers everything below it (``/docs/`` or ``docs`` owns ``docs/a/b.md``).
    """
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    return re.compile(f"{prefix}{''.join(out)}(?:/.*)?$")


def owners_for(path: str, rules: Iterable[Rule]) -> tuple[str, ...]:
    """The owners of ``path``: the LAST matching rule wins (as on GitHub)."""
    path = path.strip().lstrip("/")
    result: tuple[str, ...] = ()
    for rule in rules:
        if _regex(rule.pattern).match(path):
            result = rule.owners
    return result


def is_individual(owner: str) -> bool:
    """A user handle or an email; teams (``@org/team``) cannot be Jira assignees."""
    return "/" not in owner


def choose_assignee(paths: Iterable[str], rules: list[Rule]) -> tuple[str | None, list[str]]:
    """Return (assignee, all owners involved). The assignee is the individual owning the most
    changed files (ties: the one listed first); None when only teams own them."""
    counts: Counter[str] = Counter()
    order: dict[str, int] = {}
    everyone: list[str] = []
    for path in paths:
        for owner in owners_for(path, rules):
            if owner not in everyone:
                everyone.append(owner)
            if is_individual(owner):
                counts[owner] += 1
                order.setdefault(owner, len(order))
    if not counts:
        return None, everyone
    best = min(counts, key=lambda o: (-counts[o], order[o]))
    return best.lstrip("@"), everyone
