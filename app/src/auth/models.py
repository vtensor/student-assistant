# Public Pydantic schemas for the auth module.
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    grade: int = Field(ge=1, le=12)
    board: Literal["CBSE", "ICSE"]
    target_exam: str = Field(min_length=1, max_length=200)
    daily_study_time_minutes: int = Field(ge=10, le=600)


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    student_id: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthRecord(BaseModel):
    student_id: str
    email: EmailStr
    password_hash: str
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None
