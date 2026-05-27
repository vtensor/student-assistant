# Student profile schemas. `subjects` is the canonical list of subjects the
# student is enrolled in; used by tool4 (get_performance_history) to validate
# the agent's subject arg.
from typing import Literal

from pydantic import BaseModel, Field


class StudentProfile(BaseModel):
    student_id: str
    name: str
    grade: int = Field(ge=1, le=12)
    board: Literal["CBSE", "ICSE"]
    target_exam: str
    daily_study_time_minutes: int
    strong_topics: list[str] = Field(default_factory=list)
    weak_topics: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


class StudentProfileUpdate(BaseModel):
    name: str | None = None
    grade: int | None = Field(default=None, ge=1, le=12)
    board: Literal["CBSE", "ICSE"] | None = None
    target_exam: str | None = None
    daily_study_time_minutes: int | None = None
    strong_topics: list[str] | None = None
    weak_topics: list[str] | None = None
    subjects: list[str] | None = None
