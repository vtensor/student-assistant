# Developer-facing trace: full trajectory of one /chat request.
# One doc per request_id. Mirrors LangSmith-style structure without duplicating
# the seed prompt across every hop:
#
#   trace.seed_messages    = [System, ...chat_history, HumanMessage(current)]
#                            — stored ONCE; this is the input to hop 0.
#   trace.llm_calls[k]     = the AI message produced by hop k + token usage +
#                            latency. Plus `added_messages` = the new messages
#                            that arrived since hop k-1 (typically the tool
#                            result from the prior hop). Hop 0 has added=[].
#   trace.tool_calls[i]    = each tool invocation with full args + full result.
#
# To reconstruct hop N's full input messages:
#   seed_messages + [hop_0.ai_message, hop_1.added_messages..., hop_1.ai_message,
#                    hop_2.added_messages..., ..., hop_N.added_messages]
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0


class LLMCall(BaseModel):
    index: int                              # 0-based hop number within this request
    started_at: datetime
    latency_ms: int
    model: str
    added_messages: list[dict[str, Any]]    # messages NEW since prior hop (usually [tool_msg])
    output_content: str                     # raw assistant text the LLM produced
    output_tool_calls: list[dict[str, Any]] # tool_calls the LLM issued in this hop
    token_usage: TokenUsage
    error: str | None = None


class ToolCall(BaseModel):
    index: int                              # 0-based across all tool calls in the request
    llm_call_index: int                     # which LLM hop issued this call
    name: str
    args: dict[str, Any]
    result: Any                             # FULL result, untruncated
    started_at: datetime
    latency_ms: int
    error: str | None = None


class TraceRecord(BaseModel):
    request_id: str
    student_id: str
    chat_id: str
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    started_at: datetime
    finished_at: datetime
    total_latency_ms: int
    model: str
    user_input: str                         # original (pre-PII) user prompt
    final_output: str                       # final (post-PII) assistant reply
    seed_messages: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[LLMCall] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    total_token_usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None
