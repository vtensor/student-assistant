# Typed domain errors + a single global FastAPI handler that maps them to HTTP.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class AlreadyExists(DomainError):
    status_code = 409
    code = "already_exists"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"


class ProfileNotFound(NotFound):
    pass


class AuthCredentialsInvalid(Unauthorized):
    pass


class InvalidJWT(Unauthorized):
    pass


class VectorSearchFailed(DomainError):
    status_code = 502
    code = "vector_search_failed"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _handle(_: Request, exc: DomainError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            retry_ms = exc.detail.get("retry_after_ms")
            if retry_ms is not None:
                headers["Retry-After"] = str(max(1, int(retry_ms / 1000)))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
            headers=headers,
        )
