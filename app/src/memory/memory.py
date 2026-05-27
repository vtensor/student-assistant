# Memory: short-term chat history (Redis cache + Mongo fallback) +
# long-term extracted facts (Mongo). All persistence flows through Data so
# RLS lives in one place.
#
# Short-term per-chat history:
#   - session_push: append (user, assistant) turn to the Redis cache
#     (chat_session:{student_id}:{chat_id}, LIST, sliding TTL).
#   - session_recent: read the last N turns oldest-first. Cache-first; on
#     cache miss (cold chat opened after the TTL expired) falls back to
#     Mongo `messages` and warms the cache for next time.
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import SystemMessage

from app.config import get_settings
from app.src.cache import Cache
from app.src.database import Data
from app.src.database.models.memory import MemoryRecord
from app.src.llm import get_structured_model
from app.src.logs import log
from app.src.memory.models import MemoryWriterOutput

_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


class Memory:
    """Facade for short-term per-chat history (Redis cache + Mongo fallback)
    and long-term extracted facts (Mongo)."""

    def __init__(self, cache: Cache, data: Data) -> None:
        self._cache = cache
        self._data = data

    # --- short-term per-chat history ----------------------------------

    async def session_recent(
        self, student_id: str, chat_id: str, n: int = 10
    ) -> list[dict[str, str]]:
        """Last n (user, assistant) turns for this chat, oldest-first.
        Cache-first; on cache miss falls back to Mongo and warms the cache.
        Returns a list of {"role", "content"} dicts."""
        cached = await self._cache.get_chat_session(student_id, chat_id, n)
        if cached:
            # Cache stores newest-first (LPUSH); reverse for replay.
            return list(reversed(cached))

        # Cold cache: hydrate from Mongo. list_messages returns the last n
        # messages oldest-first within the batch (newest-first sort + reverse
        # inside the Data method), so they are already replay-ordered.
        msgs = await self._data.list_messages(student_id, chat_id, limit=n)
        turns = [
            {"role": m.role, "content": m.content}
            for m in msgs
            if m.role in ("user", "assistant")
        ]
        # Warm cache for next read. Push oldest-first so LPUSH leaves the
        # newest at index 0 (matches the live-write order from session_push).
        for t in turns:
            await self._cache.push_chat_session(student_id, chat_id, t)
        return turns

    async def session_push(
        self, student_id: str, chat_id: str, role: str, content: str
    ) -> None:
        await self._cache.push_chat_session(
            student_id, chat_id, {"role": role, "content": content}
        )

    # --- long-term (extracted facts) ----------------------------------

    async def lt_load(self, student_id: str) -> list[MemoryRecord]:
        """Read-through cache → Mongo for the student's full memory list,
        sorted by confidence desc."""
        cached = await self._cache.get_lt_memory(student_id)
        if cached is not None:
            return [MemoryRecord(**m) for m in cached]
        records = await self._data.list_memory(student_id)
        records.sort(key=lambda r: r.confidence, reverse=True)
        await self._cache.set_lt_memory(
            student_id, [r.model_dump(mode="json") for r in records]
        )
        return records

    async def extract_and_persist(
        self,
        student_id: str,
        *,
        user_message: str,
        assistant_message: str,
    ) -> list[MemoryRecord]:
        if not user_message or not assistant_message:
            return []
        prompt = _PROMPT.format(
            user_message=user_message, assistant_message=assistant_message
        )
        try:
            llm = get_structured_model().with_structured_output(MemoryWriterOutput)
            out: MemoryWriterOutput = await llm.ainvoke(
                [SystemMessage(content=prompt)]
            )
        except Exception as e:
            log.warning("memory_extract_llm_failed", err=str(e))
            return []

        threshold = get_settings().memory_confidence_threshold
        kept = [m for m in out.memories if m.confidence >= threshold]
        if not kept:
            return []

        now = datetime.now(timezone.utc)
        persisted: list[MemoryRecord] = []
        for c in kept:
            rec = MemoryRecord(
                memory_id=str(uuid4()),
                student_id=student_id,
                category=c.category,
                content=c.content,
                confidence=c.confidence,
                created_at=now,
                valid_until=c.valid_until,
                source_message_id=None,
            )
            try:
                persisted.append(await self._data.insert_memory(student_id, rec))
            except Exception as e:
                log.warning("memory_persist_failed", err=str(e))
        # Data.insert_memory invalidates the memory cache for us
        return persisted
