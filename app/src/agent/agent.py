# Agent facade: the public entry point. Routes call Agent.run(jwt, message,
# chat_id?) — the JWT flows into graph state so tools can re-verify it; the
# LLM never sees this state field. After the graph returns, eval.record fires
# synchronously (cheap rules) and a sampled judge fires fire-and-forget.
import asyncio
import random
import time
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

from app.config import get_settings
from app.src.agent.graph import build_graph
from app.src.agent.models import ChatResponse, ToolCallTrace
from app.src.auth import Identity
from app.src.cache import Cache
from app.src.database import Data
from app.src.database.models.traces import (
    LLMCall,
    TokenUsage,
    ToolCall,
    TraceRecord,
)
from app.src.logs import log
from app.src.memory import Memory
from app.src.metrics import CHAT_LATENCY_MS, CHAT_REQUESTS_TOTAL
from app.src.pii import PII

_FALLBACK_TEXT = (
    "Sorry, an internal issue happened. Please try again after some time."
)


class Agent:
    def __init__(
        self, data: Data, memory: Memory, pii: PII, cache: Cache
    ) -> None:
        self._data = data
        self._memory = memory
        self._pii = pii
        self._cache = cache
        self._identity = Identity()
        self._eval = None  # lazy; avoids import cycle

    def _get_eval(self):
        if self._eval is None:
            from app.src.eval import Eval
            self._eval = Eval(self._data)
        return self._eval

    async def _setup_turn(
        self, jwt_token: str, chat_id: str | None
    ) -> tuple[str, str, str]:
        """Verify JWT, mint request_id, create-or-validate chat_id. Shared
        by /chat and /achat so both paths enforce the same RLS."""
        student_id = self._identity.verify(jwt_token)
        request_id = uuid4().hex
        if not chat_id:
            chat = await self._data.create_chat(student_id)
            chat_id = chat.chat_id
        else:
            # RLS: confirm the chat belongs to this student BEFORE any writes.
            await self._data.get_chat(student_id, chat_id)
        return student_id, request_id, chat_id

    async def _post_graph(
        self,
        *,
        student_id: str,
        request_id: str,
        chat_id: str,
        message: str,
        final: dict,
        error: str | None,
        latency_ms: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> tuple[str, list[dict]]:
        """Bookkeeping that runs AFTER the graph completes (trace, memory
        extraction, eval). Shared by /chat and /achat. Returns
        (response_text, tool_calls_trace) for the caller to surface."""
        CHAT_LATENCY_MS.labels(phase="total").observe(latency_ms)
        CHAT_REQUESTS_TOTAL.labels(outcome="ok" if not error else "error").inc()

        response_text = final.get("response_text", "")
        trace = final.get("tool_calls_trace") or []

        try:
            llm_hops = [
                LLMCall(
                    index=h["index"],
                    started_at=datetime.fromisoformat(h["started_at"]),
                    latency_ms=h["latency_ms"],
                    model=h["model"],
                    added_messages=h.get("added_messages", []),
                    output_content=h["output_content"],
                    output_tool_calls=h["output_tool_calls"],
                    token_usage=TokenUsage(**h["token_usage"]),
                )
                for h in (final.get("trace_llm_calls") or [])
            ]
            tool_hops = [
                ToolCall(
                    index=t["index"],
                    llm_call_index=t["llm_call_index"],
                    name=t["name"],
                    args=t["args"],
                    result=t["result"],
                    started_at=datetime.fromisoformat(t["started_at"]),
                    latency_ms=t["latency_ms"],
                    error=t.get("error"),
                )
                for t in (final.get("trace_tool_calls") or [])
            ]
            total_tokens = TokenUsage(
                input=sum(h.token_usage.input for h in llm_hops),
                output=sum(h.token_usage.output for h in llm_hops),
                reasoning=sum(h.token_usage.reasoning for h in llm_hops),
                total=sum(h.token_usage.total for h in llm_hops),
            )
            trace_doc = TraceRecord(
                request_id=request_id,
                student_id=student_id,
                chat_id=chat_id,
                user_message_id=final.get("user_message_id"),
                assistant_message_id=final.get("assistant_message_id"),
                started_at=started_at,
                finished_at=finished_at,
                total_latency_ms=latency_ms,
                model=get_settings().azure_openai_chat_model,
                user_input=message,
                final_output=response_text,
                seed_messages=final.get("trace_seed_messages") or [],
                llm_calls=llm_hops,
                tool_calls=tool_hops,
                total_token_usage=total_tokens,
                error=error,
            )
            asyncio.create_task(self._data.insert_trace(trace_doc))
        except Exception as e:
            log.warning("trace_assemble_failed", rid=request_id, err=str(e))

        try:
            asyncio.create_task(
                self._memory.extract_and_persist(
                    student_id,
                    user_message=message,
                    assistant_message=response_text,
                )
            )
        except Exception as e:
            log.warning("memory_extract_dispatch_failed", err=str(e))

        ev = self._get_eval()
        try:
            await ev.record(
                request_id=request_id,
                student_id=student_id,
                chat_id=chat_id,
                user_message=message,
                response=response_text,
                tool_trace=trace,
                latency_ms_total=latency_ms,
                latency_ms_llm=final.get("latency_ms_llm", 0),
                latency_ms_tools=final.get("latency_ms_tools", 0),
                latency_ms_pii=final.get("latency_ms_pii", 0),
                pii_input_categories=final.get("pii_input_categories", []),
                error=error,
            )
            if random.random() < get_settings().eval_judge_sample_rate:
                asyncio.create_task(
                    ev.judge_async(
                        request_id=request_id,
                        user_message=message,
                        response=response_text,
                        tool_trace=trace,
                    )
                )
        except Exception as e:
            log.warning("eval_emit_failed", err=str(e))

        return response_text, trace

    async def run(
        self, jwt_token: str, message: str, chat_id: str | None = None
    ) -> ChatResponse:
        student_id, request_id, chat_id = await self._setup_turn(jwt_token, chat_id)

        graph = build_graph()
        initial_state = {
            "jwt": jwt_token,
            "chat_id": chat_id,
            "request_id": request_id,
            "data": self._data,
            "memory": self._memory,
            "pii": self._pii,
            "raw_user_message": message,
        }

        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        error: str | None = None
        try:
            final = await graph.ainvoke(initial_state)
        except Exception as e:
            log.exception("agent_run_failed", rid=request_id)
            final = {"response_text": _FALLBACK_TEXT, "tool_calls_trace": []}
            error = str(e)

        latency_ms = int((time.perf_counter() - started) * 1000)
        finished_at = datetime.now(timezone.utc)

        response_text, trace = await self._post_graph(
            student_id=student_id, request_id=request_id, chat_id=chat_id,
            message=message, final=final, error=error,
            latency_ms=latency_ms, started_at=started_at, finished_at=finished_at,
        )

        return ChatResponse(
            response=response_text,
            chat_id=chat_id,
            request_id=request_id,
            tool_calls=[ToolCallTrace(**t) for t in trace],
            latency_ms=latency_ms,
        )

    async def run_stream(
        self, jwt_token: str, message: str, chat_id: str | None = None
    ) -> AsyncIterator[tuple[str, dict]]:
        """Same as `run` but yields ('token', {'text': ...}) tuples as the
        LLM streams chunks, then a final ('done', {...}) tuple. On error,
        yields a single ('error', {'message': fallback_text}) and ('done').
        The caller (the SSE route) formats each tuple as an event."""
        student_id, request_id, chat_id = await self._setup_turn(jwt_token, chat_id)

        graph = build_graph()
        queue: asyncio.Queue = asyncio.Queue()
        initial_state = {
            "jwt": jwt_token,
            "chat_id": chat_id,
            "request_id": request_id,
            "data": self._data,
            "memory": self._memory,
            "pii": self._pii,
            "raw_user_message": message,
            "stream_queue": queue,
        }

        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        # Run the graph in a background task so we can pull tokens off the
        # queue as agent_node produces them.
        graph_task = asyncio.create_task(graph.ainvoke(initial_state))

        # Drain tokens from the queue while the graph runs. Race between
        # the next queue token and graph completion.
        while True:
            get_task = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {graph_task, get_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                item = get_task.result()
                yield item  # ("token", {"text": "..."})
            else:
                get_task.cancel()
            if graph_task in done:
                # Drain any tokens that arrived between the last get and now.
                while not queue.empty():
                    yield queue.get_nowait()
                break

        # graph finished — fetch result or exception
        error: str | None = None
        if graph_task.exception() is not None:
            exc = graph_task.exception()
            log.exception("agent_run_failed", rid=request_id)
            error = str(exc)
            final = {"response_text": _FALLBACK_TEXT, "tool_calls_trace": []}
            # If we never streamed anything, push the canned message now
            # so the client sees something useful on screen.
            yield ("token", {"text": _FALLBACK_TEXT})
        else:
            final = graph_task.result()

        latency_ms = int((time.perf_counter() - started) * 1000)
        finished_at = datetime.now(timezone.utc)

        response_text, trace = await self._post_graph(
            student_id=student_id, request_id=request_id, chat_id=chat_id,
            message=message, final=final, error=error,
            latency_ms=latency_ms, started_at=started_at, finished_at=finished_at,
        )

        yield ("done", {
            "request_id": request_id,
            "chat_id": chat_id,
            "latency_ms": latency_ms,
            "full_text": response_text,
            "tool_calls": trace,
        })
