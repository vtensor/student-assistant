# RateLimiter: per-user_uuid token bucket via Redis (10 req/sec default).
# Uses a small Lua script for atomic refill+consume; falls back to
# INCR+EXPIRE if Lua is unavailable.
import time
from dataclasses import dataclass

from app.config import get_settings
from app.src.cache.client import get_redis

# Lua script: classic token bucket.
#   KEYS[1] = bucket key
#   ARGV[1] = capacity, ARGV[2] = refill_per_sec, ARGV[3] = now_ms
# Returns: {allowed (0|1), retry_after_ms}
_LUA = """
local cap = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = cap
  ts = now
end
local elapsed_ms = math.max(0, now - ts)
tokens = math.min(cap, tokens + (elapsed_ms * refill / 1000.0))
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) * 1000.0 / refill)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(cap * 1000.0 / refill) + 1000)
return {allowed, retry}
"""


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    retry_after_ms: int


class RateLimiter:
    def __init__(self) -> None:
        self._sha: str | None = None

    async def _load(self) -> str:
        if self._sha is None:
            self._sha = await get_redis().script_load(_LUA)
        return self._sha

    async def check(self, user_uuid: str) -> LimitResult:
        s = get_settings()
        cap = s.rate_limit_burst
        refill = s.rate_limit_rps
        now_ms = int(time.time() * 1000)
        key = f"ratelimit:{user_uuid}"
        try:
            sha = await self._load()
            res = await get_redis().evalsha(sha, 1, key, cap, refill, now_ms)
        except Exception:
            # fallback: cheap INCR+EXPIRE bucket (1-second window)
            window_key = f"{key}:{int(time.time())}"
            count = await get_redis().incr(window_key)
            if count == 1:
                await get_redis().expire(window_key, 1)
            if count > refill:
                return LimitResult(allowed=False, retry_after_ms=1000)
            return LimitResult(allowed=True, retry_after_ms=0)
        allowed = bool(int(res[0]))
        retry_ms = int(res[1])
        return LimitResult(allowed=allowed, retry_after_ms=retry_ms)
