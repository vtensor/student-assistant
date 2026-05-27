# Persisted long-term memory record (LLM-extracted candidate lives in agent module).
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

MemoryCategory = Literal["preference", "struggle", "goal", "schedule", "milestone"]


class MemoryRecord(BaseModel):
    memory_id: str
    student_id: str
    category: MemoryCategory
    content: str = Field(max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    valid_until: date | None = None
    source_message_id: str | None = None
