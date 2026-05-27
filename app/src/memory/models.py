# LLM-side memory schemas (extraction candidates, before persistence).
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

MemoryCategory = Literal["preference", "struggle", "goal", "schedule", "milestone"]


class MemoryCandidate(BaseModel):
    category: MemoryCategory
    content: str = Field(max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_until: date | None = None


class MemoryWriterOutput(BaseModel):
    memories: list[MemoryCandidate] = []
