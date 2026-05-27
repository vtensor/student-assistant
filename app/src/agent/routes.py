# Chat endpoints:
#   POST /chat         - blocking, returns full ChatResponse JSON
#   POST /achat        - streaming SSE (text/event-stream); emits token/done/error
#   POST /chat/{id}/end
#   DELETE /chat/{id}
# /chat/history + /chat/{id}/messages live in database/routes.py.
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.dependencies import get_jwt, get_student_id
from app.src.agent import Agent
from app.src.agent.models import ChatRequest, ChatResponse, EndChatResponse
from app.src.cache import Cache
from app.src.database import Data
from app.src.logs import log
from app.src.memory import Memory
from app.src.pii import PII
from app.src.rate_limit import rate_limited

router = APIRouter(tags=["chat"])

# Build the agent once at import. All services are stateless w.r.t. requests.
_cache = Cache()
_data = Data(_cache)
_memory = Memory(_cache, _data)
_pii = PII()
_agent = Agent(_data, _memory, _pii, _cache)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    jwt_token: str = Depends(get_jwt),
    _student_id: str = Depends(rate_limited),
) -> ChatResponse:
    return await _agent.run(jwt_token, body.message, body.chat_id)


def _sse(event: str, data: dict) -> bytes:
    """Encode one Server-Sent Event frame. Each frame is `event: NAME\\n
    data: JSON\\n\\n` per the SSE spec; browsers and httpx-stream both
    parse this natively."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@router.post("/achat")
async def achat_stream(
    body: ChatRequest,
    jwt_token: str = Depends(get_jwt),
    _student_id: str = Depends(rate_limited),
) -> StreamingResponse:
    """Streaming counterpart to /chat. Yields SSE events as the LLM
    produces tokens; closes with a `done` event carrying request_id,
    chat_id, latency_ms, full_text."""

    async def event_source():
        try:
            async for event_name, payload in _agent.run_stream(
                jwt_token, body.message, body.chat_id
            ):
                yield _sse(event_name, payload)
        except Exception as e:
            # Any failure that escapes run_stream (e.g. JWT verify fails
            # in _setup_turn) becomes a single error+done pair so the
            # client always gets a clean terminal signal.
            log.warning("achat_stream_failed", err=str(e))
            yield _sse("error", {
                "message": "Sorry, an internal issue happened. "
                           "Please try again after some time.",
            })
            yield _sse("done", {})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "x-accel-buffering": "no",   # disable nginx buffering if proxied
        },
    )


@router.post("/chat/{chat_id}/end", response_model=EndChatResponse)
async def end_chat(
    chat_id: str, student_id: str = Depends(get_student_id)
) -> EndChatResponse:
    await _data.end_chat(student_id, chat_id)
    return EndChatResponse(chat_id=chat_id)


@router.delete("/chat/{chat_id}")
async def delete_chat(
    chat_id: str, student_id: str = Depends(get_student_id)
) -> dict:
    counts = await _data.delete_chat(student_id, chat_id)
    return {"chat_id": chat_id, **counts}
