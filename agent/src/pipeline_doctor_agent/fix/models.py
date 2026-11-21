"""What a fix attempt reports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FixStatus = Literal["pr_opened", "reused_pr", "skipped", "failed", "error"]


class FixOutcome(BaseModel):
    """The result of trying to fix a failure. Anything but ``pr_opened``/``reused_pr`` means no
    pull request exists and a human handles it from the ticket."""

    status: FixStatus
    reason: str = Field(default="", max_length=800)
    attempts: int = 0
    pr_url: str | None = None
    branch: str | None = None
    verification: str = Field(default="", max_length=600, description="What the sandbox showed")
