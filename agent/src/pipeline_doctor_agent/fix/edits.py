"""Apply search-and-replace edits and render the result as a unified diff."""

from __future__ import annotations

import difflib
from collections.abc import Mapping

from pipeline_doctor_agent.schemas import FixEdit


class EditError(ValueError):
    """An edit could not be applied exactly as written; the message goes back to the model."""


def apply_edits(originals: Mapping[str, str], edits: list[FixEdit]) -> dict[str, str]:
    """Return {path: new content} for the files that change. All-or-nothing: each ``old_text`` must
    match exactly once in the file as it is at that point, otherwise nothing is applied."""
    current = dict(originals)
    changed: set[str] = set()
    for i, e in enumerate(edits, start=1):
        if e.path not in current:
            raise EditError(f"edit {i}: file '{e.path}' was not provided")
        count = current[e.path].count(e.old_text)
        if count == 0:
            raise EditError(
                f"edit {i}: old_text was not found in '{e.path}' (copy it exactly, including indentation)"
            )
        if count > 1:
            raise EditError(
                f"edit {i}: old_text appears {count} times in '{e.path}'; include more surrounding lines so it is unique"
            )
        current[e.path] = current[e.path].replace(e.old_text, e.new_text, 1)
        changed.add(e.path)
    return {p: current[p] for p in sorted(changed) if current[p] != originals[p]}


def unified_diff(originals: Mapping[str, str], updated: Mapping[str, str]) -> str:
    out: list[str] = []
    for path in sorted(updated):
        out.extend(
            difflib.unified_diff(
                originals[path].splitlines(keepends=True),
                updated[path].splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return "".join(out)
