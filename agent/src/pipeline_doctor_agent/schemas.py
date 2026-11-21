"""Pydantic contracts. Agent outputs are validated against these; invalid output is rejected."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA_PATTERN = r"^[0-9a-fA-F]{7,40}$"
Sha = Annotated[str, Field(pattern=SHA_PATTERN)]
ShortText = Annotated[str, Field(max_length=500)]


class Strict(BaseModel):
    """Unknown fields are errors: a model that invents fields is a model that drifted."""

    model_config = ConfigDict(extra="forbid")


class Category(StrEnum):
    CODE = "code"
    TEST = "test"
    DEPENDENCY = "dependency"
    INFRASTRUCTURE = "infrastructure"
    FLAKY = "flaky"
    CONFIGURATION = "configuration"


class PipelineFailedEvent(BaseModel):
    """The queue message published by ingress (docs/ingress.md). Extra fields are ignored so
    ingress can add fields without breaking consumers."""

    idempotency_key: str = Field(min_length=3, max_length=300)
    job_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,199}$")
    build_number: int = Field(ge=1, le=10_000_000)
    build_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://[^\s|\]\[]+$")
    repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,40}$")
    received_at: datetime | None = None
    traceparent: str | None = None


# ---- investigator outputs -------------------------------------------------------------


class LogFindings(Strict):
    failed_stage: str | None = Field(default=None, max_length=200)
    error_summary: ShortText
    key_error_lines: Annotated[list[Annotated[str, Field(max_length=300)]], Field(max_length=10)]
    likely_area: Annotated[str, Field(max_length=200)] = ""


class CommitRef(Strict):
    sha: Sha
    reason: ShortText


class CodeFindings(Strict):
    candidate_commits: Annotated[list[CommitRef], Field(max_length=20)]
    suspect_commit: Sha | None = None
    suspect_reason: ShortText = ""
    files_of_interest: Annotated[
        list[Annotated[str, Field(max_length=300)]], Field(max_length=20)
    ] = []

    @field_validator("suspect_commit")
    @classmethod
    def _in_candidates(cls, v: str | None, info: object) -> str | None:
        # Cross-field check lives in the gate (it needs the candidate list); here only format.
        return v.lower() if v else v


class SimilarIncident(Strict):
    source: str = Field(max_length=300)
    summary: ShortText


class RunbookStep(Strict):
    source: str = Field(max_length=300)
    step: str = Field(max_length=300)


class KnowledgeFindings(Strict):
    similar_incidents: Annotated[list[SimilarIncident], Field(max_length=5)] = []
    runbook_steps: Annotated[list[RunbookStep], Field(max_length=10)] = []


class Diagnosis(Strict):
    category: Category
    root_cause: Annotated[str, Field(min_length=10, max_length=1000)]
    evidence: Annotated[
        list[Annotated[str, Field(min_length=3, max_length=500)]],
        Field(min_length=1, max_length=10),
    ]
    suspect_commit: Sha | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    fixable: bool

    @field_validator("suspect_commit")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("evidence")
    @classmethod
    def _no_blank(cls, v: list[str]) -> list[str]:
        if any(not e.strip() for e in v):
            raise ValueError("evidence entries must not be blank")
        return v


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_model_json[T: BaseModel](model: type[T], raw: object) -> T:
    """Validate agent output that ADK stored in session state (dict, or JSON text)."""
    if isinstance(raw, model):
        return raw
    if isinstance(raw, str):
        return model.model_validate_json(_FENCE.sub("", raw.strip()))
    return model.model_validate(raw)


# ---- fix proposals ---------------------------------------------------------------------------------


class FixEdit(Strict):
    """Replace ``old_text`` with ``new_text`` in an existing file. ``old_text`` must occur exactly once."""

    path: Annotated[str, Field(min_length=1, max_length=300)]
    old_text: Annotated[str, Field(min_length=1, max_length=4000)]
    new_text: Annotated[str, Field(max_length=4000)]


class FixProposal(Strict):
    explanation: Annotated[str, Field(min_length=5, max_length=600)]
    edits: Annotated[list[FixEdit], Field(min_length=1, max_length=5)]
