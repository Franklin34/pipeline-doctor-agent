"""Deterministic rules every model-proposed patch must pass BEFORE it is applied or tested.

The model proposes; this module decides. Violations are fed back to the model as the reason for
the failed attempt (it still counts as one of the three attempts).

These are heuristics layered on stronger controls: the sandbox (no network, no secrets), the judge
(tests must really pass, none removed), the GitHub MCP server's own protected-path and branch rules,
and a human reviewing the draft pull request.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from pd_mcp_github.policy import is_protected

from pipeline_doctor_agent.schemas import Category, FixEdit

MAX_EDITS = 5
MAX_CHANGED_LINES = 200
MAX_FILE_CHARS = 200_000

# Added code that neuters tests or reaches outside the process. A legitimate bug fix rarely needs
# these; a false positive only costs one attempt and ends in a ticket, never in a bad change.
_SUSPICIOUS = [
    (
        re.compile(
            r"pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|unittest\.skip|\bassert\s+True\b"
        ),
        "skips or neuters tests",
    ),
    (re.compile(r"\b(eval|exec|__import__)\s*\("), "dynamic code execution"),
    (
        re.compile(
            r"\bos\.(system|popen|remove|unlink|rmdir|rename)\b|\bshutil\.rmtree\b|\bsubprocess\b"
        ),
        "runs commands or deletes files",
    ),
    (
        re.compile(
            r"\bsocket\b|\burllib\b|\brequests\.|\bhttpx\b|\bhttp\.client\b|\bftplib\b|\bsmtplib\b"
        ),
        "network access",
    ),
    (re.compile(r"\bpickle\b|\bmarshal\b|\bctypes\b"), "unsafe deserialisation or native calls"),
]
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(p in {"tests", "test", "__tests__"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))
    )


def _requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-") and (m := _REQ_NAME.match(line)):
            names.add(m.group(1).lower().replace("_", "-"))
    return names


def _safe_relative(path: str) -> bool:
    return bool(
        path
        and len(path) <= 300
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and not any(p in ("", ".", "..") for p in path.split("/"))
    )


def check_edits(
    edits: list[FixEdit], category: Category, read: Callable[[str], str | None]
) -> list[str]:
    """Return every violation (empty list = the patch may be applied)."""
    problems: list[str] = []
    if not 1 <= len(edits) <= MAX_EDITS:
        problems.append(f"a fix must make between 1 and {MAX_EDITS} edits")
    if (
        sum(len(e.old_text.splitlines()) + len(e.new_text.splitlines()) for e in edits)
        > MAX_CHANGED_LINES
    ):
        problems.append(
            f"the change is larger than {MAX_CHANGED_LINES} lines; make a smaller, targeted fix"
        )

    for e in edits:
        where = f"{e.path}:"
        if not _safe_relative(e.path):
            problems.append(f"{where} not a safe repository-relative path")
            continue
        content = read(e.path)
        if content is None:
            problems.append(f"{where} the file does not exist (creating files is not allowed)")
            continue
        if len(content) > MAX_FILE_CHARS:
            problems.append(f"{where} the file is too large to edit automatically")
        if is_protected(e.path):
            problems.append(
                f"{where} protected path (pipeline, infrastructure, build or test configuration, ownership, credentials)"
            )
        elif is_test_path(e.path) and category is not Category.TEST:
            problems.append(
                f"{where} tests may not be edited for a '{category.value}' failure; fix the code under test"
            )
        elif (
            not is_test_path(e.path) and category is Category.TEST and not _is_requirements(e.path)
        ):
            problems.append(f"{where} for a 'test' failure only test files may be edited")
        if e.old_text == e.new_text:
            problems.append(f"{where} old_text and new_text are identical")
        if _is_requirements(e.path):
            if category is not Category.DEPENDENCY:
                problems.append(
                    f"{where} dependency files may only change for a 'dependency' failure"
                )
            else:
                new_content = content.replace(e.old_text, e.new_text, 1)
                if _requirement_names(content) != _requirement_names(new_content):
                    problems.append(
                        f"{where} packages may not be added or removed; only existing versions may change"
                    )
        for pattern, label in _SUSPICIOUS:
            if len(pattern.findall(e.new_text)) > len(pattern.findall(e.old_text)):
                problems.append(f"{where} the new code {label}")
    return problems


def _is_requirements(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.startswith("requirements") and name.endswith(".txt")


def read_from(files: Mapping[str, str]) -> Callable[[str], str | None]:
    return lambda path: files.get(path)
