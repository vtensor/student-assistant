# JWT encode/decode primitives. Single decode point lives in Identity.verify().
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from app.config import get_settings
from app.exceptions import InvalidJWT


def encode(*, student_id: str, email: str) -> tuple[str, int]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=s.jwt_expiration_minutes)
    payload = {
        "sub": student_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = pyjwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)
    return token, s.jwt_expiration_minutes * 60


def decode(token: str) -> dict:
    s = get_settings()
    try:
        return pyjwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except pyjwt.ExpiredSignatureError as e:
        raise InvalidJWT("token expired") from e
    except pyjwt.InvalidTokenError as e:
        raise InvalidJWT("invalid token") from e
