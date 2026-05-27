# Cache facade: namespaced JSON helpers over Redis. Keys are prefixed by
# student_id so cross-student collisions are structurally impossible.
import json
from typing import Any

from app.config import get_settings
from app.src.cache.client import get_redis


class Cache:
    """Single Redis facade for the whole monolith. Other modules import only
    this class; they never touch get_redis() directly."""

    @staticmethod
    def _k(student_id: str, ns: str, ident: str = "") -> str:
        if ident:
            return f"{student_id}:{ns}:{ident}"
        return f"{student_id}:{ns}"

    @staticmethod
    def _global_k(ns: str, ident: str) -> str:
        # for caches that are NOT per-student (e.g. embedding text hash)
        return f"global:{ns}:{ident}"

    async def _get(self, key: str) -> Any | None:
        raw = await get_redis().get(key)
        return json.loads(raw) if raw is not None else None

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        await get_redis().set(
            key, json.dumps(value, default=str, sort_keys=True), ex=ttl
        )

    async def _delete(self, key: str) -> None:
        await get_redis().delete(key)

    # --- profile -------------------------------------------------------

    async def get_profile(self, student_id: str) -> Any | None:
        return await self._get(self._k(student_id, "profile"))

    async def set_profile(self, student_id: str, value: Any) -> None:
        await self._set(
            self._k(student_id, "profile"), value, get_settings().ttl_profile
        )

    async def invalidate_profile(self, student_id: str) -> None:
        await self._delete(self._k(student_id, "profile"))

    # --- performance ---------------------------------------------------

    async def get_performance(self, student_id: str) -> Any | None:
        return await self._get(self._k(student_id, "performance"))

    async def set_performance(self, student_id: str, value: Any) -> None:
        await self._set(
            self._k(student_id, "performance"), value, get_settings().ttl_performance
        )

    async def invalidate_performance(self, student_id: str) -> None:
        await self._delete(self._k(student_id, "performance"))

    # --- upcoming tests -----------------------------------------------

    async def get_tests(self, student_id: str) -> Any | None:
        return await self._get(self._k(student_id, "tests"))

    async def set_tests(self, student_id: str, value: Any) -> None:
        await self._set(
            self._k(student_id, "tests"), value, get_settings().ttl_tests
        )

    async def invalidate_tests(self, student_id: str) -> None:
        await self._delete(self._k(student_id, "tests"))

    # --- long-term memory (full list, sorted by confidence) -----------

    async def get_lt_memory(self, student_id: str) -> Any | None:
        return await self._get(self._k(student_id, "lt_memory"))

    async def set_lt_memory(self, student_id: str, value: Any) -> None:
        await self._set(
            self._k(student_id, "lt_memory"), value, get_settings().ttl_memory
        )

    async def invalidate_memory(self, student_id: str) -> None:
        await self._delete(self._k(student_id, "lt_memory"))

    # --- chat session turns (Redis LIST, newest-first, sliding TTL) ---
    # Cache of the last N (user, assistant) pairs per chat. Cold reads (chat
    # opened after the TTL expired) fall back to Mongo via Memory.session_recent.

    async def get_chat_session(
        self, student_id: str, chat_id: str, n: int = 10
    ) -> list[Any]:
        key = self._k(student_id, "chat_session", chat_id)
        raw = await get_redis().lrange(key, 0, n - 1)
        return [json.loads(r) for r in raw]

    async def push_chat_session(
        self, student_id: str, chat_id: str, turn: Any
    ) -> None:
        s = get_settings()
        key = self._k(student_id, "chat_session", chat_id)
        pipe = get_redis().pipeline()
        pipe.lpush(key, json.dumps(turn, default=str, sort_keys=True))
        pipe.ltrim(key, 0, s.max_turns_in_context - 1)
        pipe.expire(key, s.ttl_session)
        await pipe.execute()

    async def delete_chat_session(self, student_id: str, chat_id: str) -> None:
        await self._delete(self._k(student_id, "chat_session", chat_id))

    # --- embedding cache (global, keyed by sha256(text)) --------------

    async def get_embedding(self, sha: str) -> list[float] | None:
        return await self._get(self._global_k("embedding", sha))

    async def set_embedding(self, sha: str, vector: list[float]) -> None:
        await self._set(
            self._global_k("embedding", sha), vector, get_settings().ttl_embedding
        )
