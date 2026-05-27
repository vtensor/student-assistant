# Public API: RateLimiter + the FastAPI dep used by /chat.
from app.src.rate_limit.dependency import rate_limited
from app.src.rate_limit.limiter import LimitResult, RateLimiter

__all__ = ["RateLimiter", "LimitResult", "rate_limited"]
