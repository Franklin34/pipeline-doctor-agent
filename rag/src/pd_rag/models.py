"""Data types shared by the chunker, stores and search."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["runbook", "incident"]


class Chunk(BaseModel):
    id: str
    source: str = Field(description="Path relative to the knowledge directory")
    kind: Kind
    title: str
    section: str = Field(description="Heading path, for example 'Postgres refused > Fix'")
    doc_date: date | None = None
    content: str

    @property
    def embed_text(self) -> str:
        """What gets embedded: the heading context makes short chunks self-explanatory."""
        return f"{self.title} - {self.section}\n{self.content}"


class SearchHit(BaseModel):
    chunk: Chunk
    score: float = Field(description="Reciprocal rank fusion score (higher is better)")
    vector_rank: int | None = Field(
        default=None, description="1-based rank in the vector list, if present"
    )
    keyword_rank: int | None = Field(
        default=None, description="1-based rank in the full-text list, if present"
    )
