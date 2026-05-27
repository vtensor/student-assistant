# Shared FastAPI dependencies.
# Two deps for the student API surface:
#   get_jwt        -> the raw bearer token string (used by /chat to thread it
#                     into the agent for per-tool re-verification)
#   get_student_id -> the verified student_id (used by every other route)
#
# Identity.verify(jwt) is the SINGLE decode point in the codebase.
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.exceptions import InvalidJWT
from app.src.auth import Identity

bearer_scheme = HTTPBearer(auto_error=False, description="JWT from /auth/login")
_identity = Identity()


async def get_jwt(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    if creds is None or not creds.credentials:
        raise InvalidJWT("missing bearer token")
    return creds.credentials


async def get_student_id(jwt_token: str = Depends(get_jwt)) -> str:
    return _identity.verify(jwt_token)
