"""Deterministic extraction from a (scrubbed) Jenkins console log."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STAGE_START = "[Pipeline] { ("
STAGE_END = "[Pipeline] }"
MAX_LINE = 400
MAX_ERROR_LINES = 50
MAX_TRACES = 5
MAX_TRACE_LINES = 40

_ERROR_LINE = re.compile(
    r"(?i)\b(error|errors|fail|failed|failure|exception|traceback|assert\w*|fatal|refused|"
    r"denied|timed? ?out|not found)\b|^E\s"
)
_EXIT_MARKER = re.compile(r"^ERROR:|script returned exit code")
# Exception type: dotted name ending in Error/Exception (or well-known assertion).
# Bounded: an unbounded `[\w.]*` here is quadratic on inputs like "a.a.a.a...".
_TYPE = r"[A-Za-z_][\w.]{0,100}?(?:Error|Exception|Failure|Timeout)"
_PYTEST_LOC = re.compile(rf"^(?P<file>[\w./-]+\.py):(?P<line>\d+): (?P<type>{_TYPE})\b")
_E_LINE = re.compile(rf"^E\s+(?P<type>{_TYPE})\s*(?::\s*(?P<msg>.*))?$")
_FAILED_LINE = re.compile(r"^FAILED (?P<id>[\w./-]+\.py::[\w:.\[\]-]+)(?: - (?P<rest>.*))?$")
_GENERIC = re.compile(rf"\b(?P<type>{_TYPE})\s*:\s*(?P<msg>.+)")
_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')
_JAVA_HEAD = re.compile(
    r"^(?:Exception in thread \"[^\"]+\" )?(?P<type>[\w.$]+(?:Exception|Error))(?::\s*(?P<msg>.*))?$"
)
_JAVA_FRAME = re.compile(r"^\s+at [\w.$<>/]+\(.*\)$")
_LIBRARY_PATH = re.compile(r"site-packages|/lib/python|dist-packages|<frozen|/usr/lib/|\.venv/")


def clip(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + "...(clipped)"


def stage_lines(lines: list[str], stage: str) -> list[str] | None:
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside and line == f"{STAGE_START}{stage})":
            inside = True
        elif inside and line == STAGE_END:
            return out
        elif inside:
            out.append(line)
    return out if inside else None


def infer_failed_stage(lines: list[str]) -> str | None:
    """The stage containing the first exit-code/ERROR marker, else None."""
    current: str | None = None
    for line in lines:
        if line.startswith(STAGE_START) and line.endswith(")"):
            current = line[len(STAGE_START) : -1]
        elif line == STAGE_END:
            current = None
        elif current and _EXIT_MARKER.search(line):
            return current
    return None


@dataclass
class LogFacts:
    error_lines: list[str] = field(default_factory=list)
    stack_traces: list[str] = field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    top_frame: str | None = None  # "path:function" of the innermost project frame
    failed_test_ids: list[str] = field(default_factory=list)


def extract_error_lines(lines: list[str]) -> list[str]:
    keep = [False] * len(lines)
    for i, line in enumerate(lines):
        if _ERROR_LINE.search(line):
            keep[i] = True
            if i:
                keep[i - 1] = True
    picked = [clip(line) for i, line in enumerate(lines) if keep[i] and line.strip()]
    if len(picked) > MAX_ERROR_LINES:
        head = (MAX_ERROR_LINES - 1) // 2
        tail = MAX_ERROR_LINES - 1 - head
        picked = [
            *picked[:head],
            f"... ({len(picked) - head - tail} lines omitted)",
            *picked[-tail:],
        ]
    return picked


def _trim(block: list[str]) -> str:
    if len(block) > MAX_TRACE_LINES:
        half = MAX_TRACE_LINES // 2
        block = [
            *block[:half],
            f"... ({len(block) - MAX_TRACE_LINES} lines omitted)",
            *block[-half:],
        ]
    return "\n".join(clip(b) for b in block)


def extract_stack_traces(lines: list[str]) -> tuple[list[str], str | None, str | None, str | None]:
    """Return (traces, type, message, top_frame) from Python and Java traces."""
    traces: list[str] = []
    etype = emsg = frame = None
    i = 0
    while i < len(lines) and len(traces) < MAX_TRACES:
        line = lines[i]
        if line.startswith("Traceback (most recent call last):"):
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "\t"))):
                j += 1
            end = min(j + 1, len(lines))  # include the final "Type: message" line
            block = lines[i:end]
            traces.append(_trim(block))
            last = lines[j] if j < len(lines) else ""
            m = _GENERIC.search(last)
            if m and etype is None:
                etype, emsg = m["type"], m["msg"].strip()
            for b in block:
                f = _PY_FRAME.match(b)
                if f and not _LIBRARY_PATH.search(f["file"]):
                    frame = (
                        f"{normalize_path(f['file'])}:{f['func']}"  # innermost project frame wins
                    )
            i = end
            continue
        h = _JAVA_HEAD.match(line)
        if h and i + 1 < len(lines) and _JAVA_FRAME.match(lines[i + 1]):
            j = i + 1
            while j < len(lines) and (
                _JAVA_FRAME.match(lines[j]) or lines[j].startswith("Caused by:")
            ):
                j += 1
            traces.append(_trim(lines[i:j]))
            if etype is None:
                etype, emsg = h["type"], (h["msg"] or "").strip()
            i = j
            continue
        i += 1
    return traces, etype, emsg, frame


def normalize_path(path: str) -> str:
    """Make paths repo-relative and machine-independent."""
    if "/workspace/" in path:
        path = path.split("/workspace/", 1)[1].split("/", 1)[-1]
    if "site-packages/" in path:
        path = "site-packages/" + path.split("site-packages/", 1)[1]
    return path.lstrip("./") if path.startswith("./") else path


def extract_facts(lines: list[str]) -> LogFacts:
    facts = LogFacts(error_lines=extract_error_lines(lines))
    traces, etype, emsg, frame = extract_stack_traces(lines)
    facts.stack_traces, facts.error_type, facts.error_message, facts.top_frame = (
        traces,
        etype,
        emsg,
        frame,
    )

    for line in lines:
        if m := _FAILED_LINE.match(line):
            facts.failed_test_ids.append(m["id"])
            if facts.error_type is None and m["rest"]:
                t = re.match(rf"({_TYPE})", m["rest"])
                if t:
                    facts.error_type = t[1]
    if facts.error_type is None:
        for line in lines:
            if (m := _PYTEST_LOC.match(line)) is not None:
                facts.error_type = m["type"]
                break
    for line in lines:
        if (m := _E_LINE.match(line)) is not None:
            if facts.error_type is None:
                facts.error_type = m["type"]
            if facts.error_message is None and m["type"] == facts.error_type:
                facts.error_message = (m["msg"] or "").strip() or None
            break
    if facts.error_type is None:
        for line in lines:
            if (m := _GENERIC.search(line)) is not None:
                facts.error_type, facts.error_message = m["type"], m["msg"].strip()
                break
    if facts.error_message is None:
        # pytest prints the failing expression on "E   <expr>" lines.
        first_e = next((line[1:].strip() for line in lines if line.startswith("E ")), None)
        facts.error_message = first_e or (facts.error_lines[0] if facts.error_lines else None)
    if facts.error_message:
        facts.error_message = clip(facts.error_message)
    return facts
