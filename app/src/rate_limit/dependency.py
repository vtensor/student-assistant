# FastAPI dependency that enforces per-student rate limit AFTER auth.
# Returns the verified student_id; raises RateLimited on overflow.
from fastapi import Depends

from app.dependencies import get_student_id
from app.exceptions import RateLimited
from app.src.metrics import RATE_LIMITED_TOTAL
from app.src.rate_limit.limiter import RateLimiter

_limiter = RateLimiter()


async def rate_limited(student_id: str = Depends(get_student_id)) -> str:
    result = await _limiter.check(student_id)
    if not result.allowed:
        RATE_LIMITED_TOTAL.labels(route="chat").inc()
        raise RateLimited(
            "rate limit exceeded",
            detail={"retry_after_ms": result.retry_after_ms},
        )
    return student_id
