"""A golden-dataset scenario: a failure, what a correct system must conclude, and (for offline
runs) the answers a scripted model gives. One directory per scenario under ``fixtures/``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    action: str  # "fix" or "ticket_only": what the policy gate must decide
    suspect: str | None = None
    suspect_scored: bool = True  # False when the model is *supposed* to be wrong about it
    injection_flagged: bool = False
    secrets_absent: list[str] = Field(default_factory=list)  # must never reach a model or an output
    min_blocked_calls: int = 0  # tool calls the policy layer must have refused


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    event: dict[str, Any]
    expected: Expected
    scripted_model: dict[str, list[dict[str, Any]]]


def load_scenarios(fixtures_dir: Path, only: set[str] | None = None) -> list[Scenario]:
    found = [
        Scenario.model_validate(json.loads(p.read_text()))
        for p in sorted(fixtures_dir.glob("*/scenario.json"))
    ]
    if only:
        missing = only - {s.id for s in found}
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(missing))}")
        found = [s for s in found if s.id in only]
    return found
