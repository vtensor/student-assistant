# I/O schemas for the /chat and /chat/{id}/end routes.
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    chat_id: str | None = None    # null => start a new chat


class ToolCallTrace(BaseModel):
    name: str
    args: dict
    result_preview: str | None = None


class ChatResponse(BaseModel):
    response: str
    chat_id: str
    request_id: str
    tool_calls: list[ToolCallTrace] = []
    latency_ms: int


class EndChatResponse(BaseModel):
    ok: bool = True
    chat_id: str
    status: str = "ended"
