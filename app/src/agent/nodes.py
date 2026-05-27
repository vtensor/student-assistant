# LangGraph nodes. Order: pii_in -> load_context -> agent <-> tools -> persist
# -> END. The Agent facade calls eval.record after the graph returns.
#
# Message flow (clean 3-section structure, rebuilt fresh every turn):
#   1. pii_in: redacts the raw user input, stores in state["pii_redacted_message"].
#      Does NOT touch state["messages"].
#   2. load_context: builds the LLM input from scratch:
#        [System (instructions)] + recent (last 10 user+assistant turns)
#        + [HumanMessage(redacted current input)]
#      `recent` comes from Memory.session_recent which is cache-first
#      (Redis chat_session list) and falls back to Mongo `messages` for cold
#      chats (TTL expired or process restart).
#   3. agent ⇄ tools: standard ReAct loop. add_messages reducer appends
#      AI tool_call messages and ToolMessage results within the turn.
#   4. persist: extracts response_text from the final AI message, writes
#      (user, final assistant) to Mongo `messages` (durable source of truth)
#      AND pushes them to Memory.session_push for the cache.
# Output PII redaction was removed; the corresponding pii_out node is gone.
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.src.agent.state import AgentState
from app.src.agent.tools import TOOLS
from app.src.auth import Identity
from app.src.llm import get_chat_model
from app.src.logs import log
from app.src.metrics import (
    CHAT_LATENCY_MS,
    PII_DETECTIONS_TOTAL,
    TOOL_CALLS_TOTAL,
)

_SYSTEM = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
_identity = Identity()

PREVIEW_MEMORIES = 5
MAX_PREVIEW_CHARS = 240
HISTORY_WINDOW = 10  # max history messages = 5 user+assistant pairs

# Strict "fail clean" error payload sent back to the LLM whenever any tool
# call fails (backend down, unknown tool name, bad args, expired JWT, etc.).
# The guidance text is fixed and very directive so the LLM cannot
# hallucinate, retry, or compose a partial answer from training data.
_TOOL_FAIL_GUIDANCE = (
    "Stop. Do NOT use other tools, do NOT compose an answer from training "
    "data or general knowledge, do NOT try to be helpful with partial "
    "information. Reply to the student with this exact sentence and "
    'nothing else: "Sorry, an internal issue happened. Please try again '
    'after some time."'
)


def _tool_error(tool_name: str, reason: str) -> dict:
    return {
        "error": f"{tool_name} is currently unavailable ({reason})",
        "guidance": _TOOL_FAIL_GUIDANCE,
    }


def _render_memories(memories: list, max_chars: int = 500) -> str:
    if not memories:
        return "(no prior memory)"
    lines = []
    used = 0
    for m in memories[:PREVIEW_MEMORIES]:
        line = f"- [{m.category}] {m.content}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else "(no prior memory)"


def _rehydrate(turn: dict[str, str]):
    role = turn.get("role")
    content = turn.get("content", "")
    if role == "user":
        return HumanMessage(content=content)
    if role == "assistant":
        return AIMessage(content=content)
    return HumanMessage(content=content)


def _serialize_message(m: Any) -> dict[str, Any]:
    role = getattr(m, "type", None) or "unknown"
    out: dict[str, Any] = {
        "role": role,
        "content": str(getattr(m, "content", "") or ""),
    }
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        out["tool_calls"] = [
            {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args", {})}
            for tc in tcs
        ]
    tcid = getattr(m, "tool_call_id", None)
    if tcid:
        out["tool_call_id"] = tcid
    return out


# --- 1. pii_in --------------------------------------------------------
# Redacts the raw user input. Does NOT touch state["messages"] — load_context
# owns message construction (clean order, no duplication).

async def pii_in(state: AgentState) -> dict:
    pii = state["pii"]
    raw = state["raw_user_message"]
    started = time.perf_counter()
    redacted, hits = pii.redact(raw)
    elapsed = int((time.perf_counter() - started) * 1000)
    cats = pii.categories(hits)
    for c in cats:
        PII_DETECTIONS_TOTAL.labels(category=c, surface="input").inc()
    return {
        "pii_redacted_message": redacted,
        "pii_input_categories": cats,
        "latency_ms_pii": state.get("latency_ms_pii", 0) + elapsed,
    }


# --- 2. load_context --------------------------------------------------
# Rebuilds the full messages list every turn from cached history
# (Memory.session_recent: cache-first, Mongo fallback for cold chats):
#     [System, user_0, ai_0, user_1, ai_1, ..., user_N (current, redacted)]

async def load_context(state: AgentState) -> dict:
    student_id = _identity.verify(state["jwt"])
    data = state["data"]
    memory = state["memory"]
    chat_id = state["chat_id"]

    profile = await data.get_profile(student_id)
    memories = await memory.lt_load(student_id)
    # Short-term history (last 5 user+assistant pairs = 10 messages).
    # Cache-first; on cache miss Memory falls back to Mongo and warms cache.
    recent = await memory.session_recent(student_id, chat_id, n=HISTORY_WINDOW)

    today = date.today()
    # Use the first token of the stored name as the conversational name.
    # Calling the student by full name on every turn ("Arjun Sharma, ...") feels
    # formal and robotic; first name only ("Arjun, ...") reads like a teacher.
    first_name = (profile.name or "").strip().split(" ", 1)[0] or profile.name
    substitutions = {
        "{student_name}": first_name,
        "{grade}": str(profile.grade),
        "{board}": profile.board,
        "{target_exam}": profile.target_exam,
        "{current_date}": today.isoformat(),
        "{current_weekday}": today.strftime("%A"),
        "{subjects_list}": (
            ", ".join(profile.subjects) if profile.subjects else "(not set)"
        ),
        "{memories_preview}": _render_memories(memories),
    }
    system_text = _SYSTEM
    for placeholder, value in substitutions.items():
        system_text = system_text.replace(placeholder, value)

    # Build the LLM input — 3 logical sections:
    #   1. System instructions   (rendered fresh for today's date + profile)
    #   2. Chat history          (last 10 user+assistant turns, oldest-first)
    #   3. Current user prompt   (PII-redacted)
    # Section 4 (ongoing AI tool_calls + tool results) is appended later by
    # the agent ⇄ tools loop via the add_messages reducer.
    messages: list = [SystemMessage(content=system_text)]
    for turn in recent[-HISTORY_WINDOW:]:
        messages.append(_rehydrate(turn))
    messages.append(HumanMessage(content=state["pii_redacted_message"]))

    # capture the seed for the developer trace (stored ONCE per request)
    seed = [_serialize_message(m) for m in messages]

    return {
        "messages": messages,
        "tool_calls_trace": [],
        "latency_ms_llm": 0,
        "latency_ms_tools": 0,
        "trace_seed_messages": seed,
        "trace_llm_calls": [],
        "trace_tool_calls": [],
    }


# --- 3. agent + 4. tools ---------------------------------------------

def _noop(**kwargs):
    return kwargs


def _to_langchain_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name=name, description=desc, args_schema=schema, func=_noop
        )
        for name, (schema, _impl, desc) in TOOLS.items()
    ]


_BOUND = None


def _bound_model():
    global _BOUND
    if _BOUND is None:
        _BOUND = get_chat_model().bind_tools(_to_langchain_tools())
    return _BOUND


async def agent_node(state: AgentState) -> dict:
    """One LLM hop. Captures the trace delta (only messages new since the
    previous hop) instead of re-storing the full prompt every time.

    Streams the LLM call via `astream` and accumulates the chunks. If
    `state["stream_queue"]` is present (set by /achat), each chunk's text
    is pushed there so the streaming route can forward it as an SSE token.
    For /chat (non-streaming) the queue is None and we just accumulate."""
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    queue = state.get("stream_queue")

    response = None
    async for chunk in _bound_model().astream(state["messages"]):
        # Accumulate via AIMessageChunk.__add__ (preserves tool_calls + usage)
        response = chunk if response is None else response + chunk
        # Forward visible content tokens (skip tool-call-only chunks that
        # carry empty content) to the streaming consumer if any.
        if queue is not None:
            text = getattr(chunk, "content", "")
            if isinstance(text, str) and text:
                await queue.put(("token", {"text": text}))

    if response is None:
        # Edge case: empty stream (shouldn't happen with Azure OpenAI but
        # guard against it). Build an empty AIMessage so downstream code
        # doesn't crash on getattr lookups.
        from langchain_core.messages import AIMessage as _AIMessage
        response = _AIMessage(content="")

    elapsed = int((time.perf_counter() - started) * 1000)
    CHAT_LATENCY_MS.labels(phase="llm").observe(elapsed)

    usage = getattr(response, "usage_metadata", None) or {}
    out_tool_calls = getattr(response, "tool_calls", None) or []

    # delta = messages new since last hop. For hop 0 that's [] (the seed is
    # already stored at trace root). For subsequent hops it's typically the
    # ToolMessage(s) that arrived after the previous tool_node ran.
    prior_hops = state.get("trace_llm_calls") or []
    seed_size = len(state.get("trace_seed_messages") or [])
    # count of messages each prior hop output added (its own AI msg + the tool
    # messages from tool_node = number of tool_calls it issued)
    consumed = seed_size
    for h in prior_hops:
        consumed += 1 + len(h.get("output_tool_calls") or [])
    delta = [_serialize_message(m) for m in state["messages"][consumed:]]

    llm_hop = {
        "index": len(prior_hops),
        "started_at": started_at.isoformat(),
        "latency_ms": elapsed,
        "model": get_settings().azure_openai_chat_model,
        "added_messages": delta,
        "output_content": str(getattr(response, "content", "") or ""),
        "output_tool_calls": [
            {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args", {})}
            for tc in out_tool_calls
        ],
        "token_usage": {
            "input": int(usage.get("input_tokens", 0)) if usage else 0,
            "output": int(usage.get("output_tokens", 0)) if usage else 0,
            "reasoning": int(
                (usage.get("output_token_details") or {}).get("reasoning", 0)
            ) if usage else 0,
            "total": int(usage.get("total_tokens", 0)) if usage else 0,
        },
    }

    return {
        "messages": [response],
        "latency_ms_llm": state.get("latency_ms_llm", 0) + elapsed,
        "trace_llm_calls": prior_hops + [llm_hop],
    }


def has_tool_calls(state: AgentState) -> str:
    msgs = state["messages"]
    if not msgs:
        return "end"
    last = msgs[-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "end"


async def tool_node(state: AgentState) -> dict:
    started = time.perf_counter()
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", []) or []
    data = state["data"]
    trace = list(state.get("tool_calls_trace") or [])
    full_trace = list(state.get("trace_tool_calls") or [])
    llm_hop_index = max(len(state.get("trace_llm_calls") or []) - 1, 0)
    out_msgs: list[ToolMessage] = []

    try:
        student_id = _identity.verify(state["jwt"])
    except Exception as e:
        log.warning("tool_node_jwt_invalid", err=str(e))
        for call in tool_calls:
            payload = _tool_error(call.get("name", "tool"), "session token expired")
            out_msgs.append(
                ToolMessage(
                    content=json.dumps(payload),
                    tool_call_id=call.get("id", ""),
                )
            )
        return {
            "messages": out_msgs,
            "tool_calls_trace": trace,
            "trace_tool_calls": full_trace,
        }

    for call in tool_calls:
        name = call["name"]
        args = call.get("args", {}) or {}
        call_id = call.get("id", "")
        call_started_at = datetime.now(timezone.utc)
        call_started = time.perf_counter()

        entry = TOOLS.get(name)
        if entry is None:
            payload = _tool_error(name, "unknown tool")
            out_msgs.append(ToolMessage(content=json.dumps(payload), tool_call_id=call_id))
            trace.append({"name": name, "args": args, "result_preview": "unknown tool"})
            full_trace.append({
                "index": len(full_trace),
                "llm_call_index": llm_hop_index,
                "name": name,
                "args": args,
                "result": payload,
                "started_at": call_started_at.isoformat(),
                "latency_ms": int((time.perf_counter() - call_started) * 1000),
                "error": "unknown_tool",
            })
            continue

        schema, impl, _desc = entry
        try:
            parsed = schema(**args)
        except Exception as e:
            payload = _tool_error(name, f"invalid arguments: {e}")
            out_msgs.append(ToolMessage(content=json.dumps(payload), tool_call_id=call_id))
            trace.append({"name": name, "args": args, "result_preview": "invalid args"})
            full_trace.append({
                "index": len(full_trace),
                "llm_call_index": llm_hop_index,
                "name": name,
                "args": args,
                "result": payload,
                "started_at": call_started_at.isoformat(),
                "latency_ms": int((time.perf_counter() - call_started) * 1000),
                "error": f"invalid_args: {e}",
            })
            continue

        call_error: str | None = None
        try:
            result = await impl(parsed, student_id, data)
        except Exception as e:
            log.exception("tool_execution_failed", tool=name)
            # Short, neutral backend reason so the user-facing message stays
            # generic. The full traceback is in the structured log line.
            result = _tool_error(name, "backend error")
            call_error = str(e)

        call_latency = int((time.perf_counter() - call_started) * 1000)
        TOOL_CALLS_TOTAL.labels(tool=name).inc()
        serialized = json.dumps(result, default=str)
        trace.append(
            {"name": name, "args": args, "result_preview": serialized[:MAX_PREVIEW_CHARS]}
        )
        full_trace.append({
            "index": len(full_trace),
            "llm_call_index": llm_hop_index,
            "name": name,
            "args": args,
            "result": result,
            "started_at": call_started_at.isoformat(),
            "latency_ms": call_latency,
            "error": call_error,
        })
        out_msgs.append(ToolMessage(content=serialized, tool_call_id=call_id))

    elapsed = int((time.perf_counter() - started) * 1000)
    CHAT_LATENCY_MS.labels(phase="tools").observe(elapsed)
    return {
        "messages": out_msgs,
        "tool_calls_trace": trace,
        "trace_tool_calls": full_trace,
        "latency_ms_tools": state.get("latency_ms_tools", 0) + elapsed,
    }


# --- 5. persist -------------------------------------------------------
# Mongo: insert user + final assistant rows.
# Cache: push (user_raw, response_text) via Memory.session_push so the next
# turn's load_context can read them without hitting Mongo.
# Also extracts response_text from the final AI message (this used to be
# pii_out's job, now folded in since output PII is disabled).

async def persist(state: AgentState) -> dict:
    from uuid import uuid4

    from app.src.database.models.messages import MessageRecord

    data = state["data"]
    memory = state["memory"]
    chat_id = state["chat_id"]
    raw_user = state["raw_user_message"]
    trace = state.get("tool_calls_trace") or []

    # final AI text comes from the last AIMessage in state.messages.
    final_msg = next(
        (m for m in reversed(state["messages"])
         if isinstance(m, AIMessage) and m.content),
        None,
    )
    response_text = str(final_msg.content) if final_msg else ""

    try:
        student_id = _identity.verify(state["jwt"])
    except Exception as e:
        log.warning("persist_jwt_invalid", err=str(e))
        return {"messages": []}

    now = datetime.now(timezone.utc)
    user_message_id = str(uuid4())
    asst_message_id = str(uuid4())

    user_record = MessageRecord(
        message_id=user_message_id,
        chat_id=chat_id,
        student_id=student_id,
        role="user",
        content=raw_user,
        created_at=now,
    )
    asst_record = MessageRecord(
        message_id=asst_message_id,
        chat_id=chat_id,
        student_id=student_id,
        role="assistant",
        content=response_text,
        tool_calls=trace or None,
        created_at=now,
    )

    try:
        await data.insert_message(student_id, user_record)
        await data.insert_message(student_id, asst_record)
        await data.touch_chat(student_id, chat_id)
        await data.set_chat_title_if_empty(student_id, chat_id, raw_user)
    except Exception as e:
        log.warning("persist_messages_failed", err=str(e))

    # Push to short-term cache so the next turn's load_context can read it
    # cheaply (Cache.push_chat_session LTRIMs to max_turns_in_context).
    try:
        await memory.session_push(student_id, chat_id, "user", raw_user)
        await memory.session_push(student_id, chat_id, "assistant", response_text)
    except Exception as e:
        log.warning("session_push_failed", err=str(e))

    return {
        "messages": [],
        "user_message_id": user_message_id,
        "assistant_message_id": asst_message_id,
        # surface response_text for Agent.run / Agent.run_stream consumers
        "response_text": response_text,
    }
