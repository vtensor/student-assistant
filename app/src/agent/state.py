# LangGraph state. Carries the raw JWT (not student_id) so tools can re-verify
# on every call (defense in depth). The LLM never sees `jwt` — only `messages`
# go to the model.
#
# No graph checkpointer is used. Short-term chat history is owned by
# `Memory.session_recent` (Redis cache, populated by `Memory.session_push`,
# falls back to Mongo `messages` for cold chats). load_context reads it at
# the start of every turn and rebuilds `messages` from scratch — clean order,
# no duplication.
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def _replace(old, new):
    """Reducer that replaces the value wholesale instead of merging.
    Used on trace arrays (load_context resets them each turn)."""
    return new if new is not None else old


class AgentState(TypedDict, total=False):
    # rebuilt fresh every turn by load_context; agent/tool nodes append via add_messages
    messages: Annotated[list[BaseMessage], add_messages]

    # identity / services (per-turn; never persisted across runs)
    jwt: str
    chat_id: str
    request_id: str
    data: Any
    memory: Any
    pii: Any

    # PII pipeline scratch (input only; output PII redaction is disabled)
    raw_user_message: str
    pii_redacted_message: str
    pii_input_categories: list[str]

    # graph outputs
    response_text: str
    tool_calls_trace: list[dict[str, Any]]

    # latency aggregates (one int each, summed in-place by nodes)
    latency_ms_llm: int
    latency_ms_tools: int
    latency_ms_pii: int

    # developer-facing trace data (load_context resets via _replace each turn)
    trace_seed_messages: Annotated[list[dict[str, Any]], _replace]
    trace_llm_calls: Annotated[list[dict[str, Any]], _replace]
    trace_tool_calls: Annotated[list[dict[str, Any]], _replace]

    # ids minted by `persist`, surfaced to Agent.run for trace correlation
    user_message_id: str
    assistant_message_id: str

    # Optional asyncio.Queue used by /achat streaming. agent_node pushes
    # ("token", {"text": ...}) tuples into it as the LLM streams chunks.
    # Absent for the non-streaming /chat path (queue is None → no-op).
    stream_queue: Any
