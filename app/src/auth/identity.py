# Identity: the only module that touches passwords / JWT / auth collection.
# Public API: signup, login, refresh, verify. There is NO Principal class.
# Identity is the JWT string; verify(jwt) returns the plain student_id (str).
from datetime import datetime, timezone
from uuid import uuid4

from app.exceptions import AlreadyExists, AuthCredentialsInvalid
from app.src.auth.jwt import decode as _decode
from app.src.auth.jwt import encode as _encode
from app.src.auth.models import (
    AuthRecord,
    LoginBody,
    SignupBody,
    TokenResponse,
)
from app.src.auth.password import hash_password, verify_password
from app.src.database.client import get_db

# Default subjects to seed for a fresh student profile by (board, grade).
# Editable later via PUT /profile.
_DEFAULT_SUBJECTS = {
    ("CBSE", 10): ["Mathematics", "Science", "English", "Social Science", "Hindi"],
    ("ICSE", 10): [
        "Mathematics", "Physics", "Chemistry", "Biology", "English",
        "History and Civics", "Geography", "Computer Applications",
    ],
}


class Identity:
    """Facade for student authentication. The only place JWTs get decoded
    and the only place passwords get hashed."""

    @property
    def _auth(self):
        return get_db().auth

    @property
    def _students(self):
        return get_db().students

    async def signup(self, body: SignupBody) -> TokenResponse:
        student_id = f"S{uuid4().hex[:10].upper()}"
        user_uuid = str(uuid4())
        now = datetime.now(timezone.utc)

        auth_doc = AuthRecord(
            student_id=student_id,
            email=body.email,
            password_hash=hash_password(body.password),
            created_at=now,
            updated_at=now,
            last_login_at=None,
        ).model_dump(mode="json")
        auth_doc["_id"] = user_uuid

        subjects = _DEFAULT_SUBJECTS.get((body.board, body.grade), [])
        student_doc = {
            "_id": user_uuid,
            "student_id": student_id,
            "name": body.name,
            "grade": body.grade,
            "board": body.board,
            "target_exam": body.target_exam,
            "daily_study_time_minutes": body.daily_study_time_minutes,
            "strong_topics": [],
            "weak_topics": [],
            "subjects": subjects,
        }

        try:
            await self._auth.insert_one(auth_doc)
        except Exception as e:
            if "duplicate key" in str(e):
                raise AlreadyExists("email already registered") from e
            raise

        try:
            await self._students.insert_one(student_doc)
        except Exception:
            # roll back the auth row if the student insert fails
            await self._auth.delete_one({"_id": user_uuid})
            raise

        token, expires_in = _encode(student_id=student_id, email=body.email)
        return TokenResponse(
            student_id=student_id, access_token=token, expires_in=expires_in
        )

    async def login(self, body: LoginBody) -> TokenResponse:
        doc = await self._auth.find_one({"email": body.email})
        if not doc:
            raise AuthCredentialsInvalid("invalid credentials")
        if not verify_password(body.password, doc["password_hash"]):
            raise AuthCredentialsInvalid("invalid credentials")

        now = datetime.now(timezone.utc)
        await self._auth.update_one(
            {"_id": doc["_id"]},
            {"$set": {"last_login_at": now, "updated_at": now}},
        )

        token, expires_in = _encode(
            student_id=doc["student_id"], email=doc["email"]
        )
        return TokenResponse(
            student_id=doc["student_id"],
            access_token=token,
            expires_in=expires_in,
        )

    async def refresh(self, jwt_token: str) -> TokenResponse:
        # decode current token (raises if expired); re-issue
        claims = _decode(jwt_token)
        token, expires_in = _encode(
            student_id=claims["sub"], email=claims["email"]
        )
        return TokenResponse(
            student_id=claims["sub"],
            access_token=token,
            expires_in=expires_in,
        )

    def verify(self, jwt_token: str) -> str:
        """Single decode point in the codebase. Returns the verified
        student_id as a plain string. Raises InvalidJWT on expiry/bad."""
        claims = _decode(jwt_token)
        return claims["sub"]
