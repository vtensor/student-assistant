# Per-turn message record (user / assistant / tool / system).
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0


class MessageRecord(BaseModel):
    message_id: str
    chat_id: str
    student_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int | None = None
    created_at: datetime
